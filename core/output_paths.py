"""Rutas locales/publicación fuera de Cherry y aliases Windows acotados; sin SQL."""
import ctypes
import itertools
import os
from pathlib import Path

def safe_path(path, *, exists=True, portable=False):
    path = Path(path)
    if not path.is_absolute() or len(str(path)) > 4096 or path.drive.startswith('\\') or any(':' in part for part in path.parts[1:]):
        raise ValueError('metadata_requires_local_path')
    if os.name == 'nt':
        drive = ctypes.WinDLL('kernel32', use_last_error=True).GetDriveTypeW
        drive.argtypes, drive.restype = (ctypes.c_wchar_p,), ctypes.c_uint32
        if drive(path.anchor) not in (2, 3): raise ValueError('metadata_requires_local_path')
    check = path if exists else path.parent
    for parent in (check, *check.parents):
        info = parent.stat(follow_symlinks=False)
        if parent.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('metadata_link_requires_review')
    path = path.resolve(strict=exists)
    if portable:
        for parent in (path.parent, *path.parents):
            if os.path.lexists(parent / 'catalog.db') or os.path.lexists(parent / 'panopticon.curation.db'):
                raise ValueError('metadata_cherry_original_protected')
    return path


def path_variants(output, entered):
    values = {str(output), str(Path(entered).absolute())}
    if os.name == 'nt':
        api = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
        api.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        api.restype = ctypes.c_uint32
        size = api(str(output.parent), None, 0)
        if not size or size > 32768: raise ValueError('metadata_alias_requires_review')
        buffer = ctypes.create_unicode_buffer(size)
        count = api(str(output.parent), buffer, size)
        if not count or count >= size: raise ValueError('metadata_alias_requires_review')
        short = Path(buffer.value)
        if len(short.parts) != len(output.parent.parts): raise ValueError('metadata_alias_requires_review')
        options, count = [], 1
        for long_part, short_part in zip(output.parent.parts, short.parts):
            parts = [long_part] if long_part.casefold() == short_part.casefold() else [long_part, short_part]
            count *= len(parts)
            if count > 30: raise ValueError('metadata_alias_requires_review')
            options.append(parts)
        for parts in itertools.product(*options): values.add(str(Path(*parts) / output.name))
    if len(values) > 32: raise ValueError('metadata_alias_requires_review')
    return sorted(values)
