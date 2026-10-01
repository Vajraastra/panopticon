"""Handles Windows: verificar bytes y eliminar el archivo abierto, nunca por ruta.

CreateFile comparte sólo lectura para el keeper; la víctima queda exclusiva.
FILE_DISPOSITION_INFO marca la eliminación al cerrar, sólo tras aprobación.
https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle
"""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import stat
from core.curation_apply import _check_cancel, _identity
from .file_guards import plain_chain


class PinnedFile:
    def __init__(self, path, *, delete=False):
        self.path = Path(path)
        self.deletable = delete
        self.stream = None
        self.api = None
        self.marked = False

    def __enter__(self):
        if os.name != 'nt':
            raise ValueError('duplicate_purge_requires_windows')
        import msvcrt
        plain_chain(self.path)
        name = str(self.path)
        # Sin UNC/dispositivos/ADS; no inferir semántica de borrado de un servidor.
        if (not self.path.is_absolute() or not self.path.drive or name.startswith('\\\\') or
                ':' in name[2:] or len(name) > 4096):
            raise ValueError('duplicate_purge_local_path_required')
        self.actual = self.path.resolve(strict=True)
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        api.GetDriveTypeW.restype = wintypes.UINT
        if api.GetDriveTypeW(self.path.anchor) not in (2, 3):
            raise ValueError('duplicate_purge_local_path_required')
        api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        api.CreateFileW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        api.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        api.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.SetFileInformationByHandle.restype = wintypes.BOOL
        handle = api.CreateFileW(name, 0x80000000 | (0x10000 if self.deletable else 0),
            0 if self.deletable else 1, None, 3, 0x00200000 | 0x08000000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        fd = None
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
            self.stream = os.fdopen(fd, 'rb', buffering=0)
        except Exception:
            if fd is None:
                api.CloseHandle(handle)
            else:
                os.close(fd)
            raise
        self.api = api
        self.handle = handle
        try:
            self.check()
        except Exception:
            self.close()
            raise
        return self

    def check(self):
        plain_chain(self.path)
        buffer = ctypes.create_unicode_buffer(32768)
        count = self.api.GetFinalPathNameByHandleW(self.handle, buffer, len(buffer), 0)
        if not 0 < count < len(buffer):
            raise ValueError('duplicate_purge_handle_path_unavailable')
        final = buffer.value
        if final.startswith('\\\\?\\'):
            final = final[4:]
        if os.path.normcase(final) != os.path.normcase(str(self.actual)):
            raise ValueError('duplicate_purge_handle_path_changed')
        info = os.fstat(self.stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400 or
                (self.deletable and info.st_nlink != 1) or _identity(self.path.stat()) != _identity(info)):
            raise ValueError('duplicate_purge_handle_identity_changed')
        return _identity(info)

    def verify(self, digest, identity, event=None):
        before = self.check()
        if before != tuple(identity):
            raise ValueError('duplicate_purge_bytes_changed')
        self.stream.seek(0)
        hasher = hashlib.sha256()
        while True:
            _check_cancel(event)
            block = self.stream.read(1024 * 1024)
            if not block:
                break
            hasher.update(block)
        if self.check() != before or hasher.hexdigest() != digest:
            raise ValueError('duplicate_purge_bytes_changed')
        return before

    def mark_delete(self):
        if not self.deletable or self.marked:
            raise ValueError('invalid_duplicate_purge_handle')
        self.check()
        # Win32 BOOLEAN es BYTE, no BOOL de cuatro bytes.
        flag = ctypes.c_ubyte(1)
        if not self.api.SetFileInformationByHandle(self.handle, 4, ctypes.byref(flag), ctypes.sizeof(flag)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.marked = True

    def close(self):
        if self.stream is not None:
            stream, self.stream = self.stream, None
            stream.close()

    def __exit__(self, *args):
        self.close()
