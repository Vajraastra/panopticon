"""Worker-only observations of Windows names, never a deletion authorization.

Resolve existing local names through Windows instead of Python case folding.
Hard links retain the name used to open them. No directory flags are changed.
No caching: an observation may expire as soon as the handle closes. Consumers
must keep DB identity/curation and revalidate before publishing a migration.
"""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import ntpath
import os
from pathlib import Path
import stat

MAX_PATH_CHARS = 4096


@dataclass(frozen=True, slots=True)
class PathIdentity:
    path: str
    status: str
    canonical: str | None = None
    kind: str | None = None
    physical: tuple | None = None
    reason: str | None = None


def local_path(value):
    """Pure validation; reject ambiguous Win32 spelling before any disk call."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS or '\0' in value:
        raise ValueError('invalid_identity_path')
    drive, tail = ntpath.splitdrive(value)
    if (len(drive) != 2 or drive[1] != ':' or not drive[0].isascii() or
            not drive[0].isalpha() or not tail.startswith(('\\', '/')) or ':' in tail):
        raise ValueError('identity_local_absolute_path_required')
    # Win32 strips trailing dots/spaces; do not silently merge such records.
    parts = tail.replace('\\', '/').split('/')
    if any(part and (part in ('.', '..') or part[-1] in ('.', ' ') or
                    any(ch in part for ch in '*?<>|"')) for part in parts):
        raise ValueError('identity_ambiguous_spelling')
    return ntpath.normpath(value).replace('\\', '/')


def _chain(path):
    values = []
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        if getattr(info, 'st_file_attributes', 0) & 0x400 or stat.S_ISLNK(info.st_mode):
            raise ValueError('identity_reparse_path')
        # Directory content changes are irrelevant to its name/identity.
        values.append((info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)))
    return tuple(values)


class _Info(ctypes.Structure):
    _fields_ = [('attributes', wintypes.DWORD), ('creation', wintypes.FILETIME),
                ('access', wintypes.FILETIME), ('write', wintypes.FILETIME),
                ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
                ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]


def _windows_observe(path):
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                           wintypes.DWORD, wintypes.DWORD]
    api.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    api.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Info)]
    api.GetFileInformationByHandle.restype = wintypes.BOOL
    api.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    api.GetDriveTypeW.restype = wintypes.UINT
    target = Path(path)
    if api.GetDriveTypeW(target.anchor) not in (2, 3):
        raise ValueError('identity_local_drive_required')
    before = _chain(target)
    # Attributes only, share reads/writes/deletes: no image decoding, exclusive
    # pin, or interference with Cherry downloads. Open directories as well.
    handle = api.CreateFileW(str(target), 0x80, 7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        def information():
            value = _Info()
            if not api.GetFileInformationByHandle(handle, ctypes.byref(value)):
                raise ctypes.WinError(ctypes.get_last_error())
            if value.attributes & 0x400:
                raise ValueError('identity_reparse_path')
            return (value.volume, value.index_high, value.index_low,
                    value.size_high, value.size_low,
                    value.write.dwHighDateTime, value.write.dwLowDateTime), value.attributes
        physical, attrs = information()
        buffer = ctypes.create_unicode_buffer(MAX_PATH_CHARS + 8)
        count = api.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not 0 < count < len(buffer):
            raise ValueError('identity_final_name_unavailable')
        final = buffer.value
        if final.startswith('\\\\?\\'):
            final = final[4:]
        canonical = local_path(final)
        # Observe path bindings again, including the actual spelling. This is
        # race detection, not an atomic filesystem/SQLite transaction.
        if before != _chain(target) or before != _chain(Path(canonical)) or physical != information()[0]:
            raise ValueError('identity_path_changed')
        return canonical, 'directory' if attrs & 0x10 else 'file', physical
    finally:
        api.CloseHandle(handle)


def observe_path(value, *, cancel_event=None):
    """Blocking filesystem work belongs outside Qt GUI and SQL transactions.

    Cancellation is cooperative between calls; Windows I/O has no hard deadline.
    Unknown/offline/reparse names never acquire a guessed canonical key.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise ValueError('identity_cancelled')
    path = local_path(value)
    if os.name != 'nt':
        return PathIdentity(path, 'unsupported', reason='identity_requires_windows')
    try:
        canonical, kind, physical = _windows_observe(path)
    except OSError as exc:
        result = PathIdentity(path, 'unavailable', reason='winerror_' + str(getattr(exc, 'winerror', None)))
    except ValueError as exc:
        result = PathIdentity(path, 'changed' if str(exc) == 'identity_path_changed' else 'unsupported', reason=str(exc))
    else:
        result = PathIdentity(path, 'resolved', canonical, kind, physical)
    if cancel_event is not None and cancel_event.is_set():
        raise ValueError('identity_cancelled')
    return result
