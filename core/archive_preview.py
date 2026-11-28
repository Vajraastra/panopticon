"""Primera imagen de un ZIP/RAR, leída en memoria y sin extraer nada al disco.

Lo usa la vista previa de Pickler (y puede usarla cualquier visor). Solo lee:
lista las entradas, elige una imagen y devuelve sus bytes con un tope de tamaño.

Qué imagen se elige:
- ZIP y RAR normal: la primera por nombre en orden natural (``2.png`` antes de ``10.png``).
- RAR sólido: la primera imagen en orden físico. En un RAR sólido llegar a un archivo
  obliga a descomprimir todo lo anterior, así que pedir «la primera por nombre» podría
  descomprimir el paquete entero solo para una vista previa.

Lo que no se puede mostrar (cifrado, dividido en volúmenes, sin imágenes, imagen
demasiado grande) no es un error: vuelve como ``problem`` para que la interfaz lo
diga y el archivo se pueda seguir marcando. Un paquete dañado sí lanza ``ValueError``.

UnRAR viene dentro del wheel de ``unrar2-cffi``; no se usan ejecutables del sistema.
"""
from __future__ import annotations

import re
import stat
import zipfile
from pathlib import PurePosixPath, PureWindowsPath

ARCHIVE_EXTENSIONS = frozenset({'.zip', '.cbz', '.rar', '.cbr'})
IMAGE_EXTENSIONS = frozenset({'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tiff', '.tif', '.avif'})
MAX_ENTRIES = 100_000
MAX_MEMBER_BYTES = 128 * 1024 * 1024
_CHUNK = 1024 * 1024

# RARHeaderDataEx.Flags (UnRAR dll.hpp)
_RHDF_SPLITBEFORE = 0x01
_RHDF_SPLITAFTER = 0x02
_RHDF_ENCRYPTED = 0x04
_RHDF_SOLID = 0x10
_RHDF_DIRECTORY = 0x20
_ERAR_END_ARCHIVE = 10
_ERAR_MISSING_PASSWORD = 22
_ERAR_BAD_PASSWORD = 24


class Cancelled(Exception):
    """La vista previa se canceló (el usuario pasó a otro archivo)."""


def is_archive(path) -> bool:
    return PureWindowsPath(str(path)).suffix.lower() in ARCHIVE_EXTENSIONS


def archive_format(path) -> str:
    suffix = PureWindowsPath(str(path)).suffix.lower()
    return 'zip' if suffix in ('.zip', '.cbz') else 'rar'


def natural_key(name: str):
    """Orden natural, sin distinguir mayúsculas: «img2» antes de «img10»."""
    return [(0, int(part), '') if part.isdigit() else (1, 0, part)
            for part in re.split(r'(\d+)', name.replace('\\', '/').lower())]


def _is_image(name: str) -> bool:
    return PurePosixPath(name.replace('\\', '/')).suffix.lower() in IMAGE_EXTENSIONS


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def _result(fmt, *, images=0, entries=0, unpacked=0, first=None, data=None, solid=False, problem=None):
    return dict(format=fmt, image_count=images, entry_count=entries, unpacked_bytes=unpacked,
                first_name=first, data=data, solid=solid, problem=problem)


def inspect_archive(path, cancel=None) -> dict:
    """Cuenta las imágenes del paquete y devuelve los bytes de la primera.

    Devuelve ``format``, ``image_count``, ``entry_count``, ``unpacked_bytes`` (suma de
    las imágenes sin comprimir), ``first_name``, ``data`` (bytes o None), ``solid`` y
    ``problem`` (None, 'encrypted', 'multivolume', 'no_images' o 'too_large').
    """
    _check(cancel)
    if archive_format(path) == 'zip':
        return _inspect_zip(path, cancel)
    return _inspect_rar(path, cancel)


# ── ZIP ───────────────────────────────────────────────────────────────
def _inspect_zip(path, cancel):
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ValueError('archive_damaged') from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ValueError('archive_entry_limit')
        entries = [info for info in infos if not info.is_dir()]
        images = [info for info in entries
                  if _is_image(info.filename) and not stat.S_ISLNK(info.external_attr >> 16)]
        unpacked = sum(info.file_size for info in images)
        base = dict(images=len(images), entries=len(entries), unpacked=unpacked)
        if not images:
            return _result('zip', problem='no_images', **base)
        first = min(images, key=lambda info: natural_key(info.filename))
        if first.flag_bits & 0x1:
            return _result('zip', first=first.filename, problem='encrypted', **base)
        if first.file_size > MAX_MEMBER_BYTES:
            return _result('zip', first=first.filename, problem='too_large', **base)
        data = bytearray()
        try:
            with archive.open(first) as stream:
                while chunk := stream.read(_CHUNK):
                    _check(cancel)
                    data += chunk
                    if len(data) > first.file_size:
                        raise ValueError('archive_damaged')
        except (zipfile.BadZipFile, EOFError, OSError, NotImplementedError, RuntimeError) as exc:
            raise ValueError('archive_damaged') from exc
        if len(data) != first.file_size:
            raise ValueError('archive_damaged')
        return _result('zip', first=first.filename, data=bytes(data), **base)


# ── RAR ───────────────────────────────────────────────────────────────
def _open_rar(rar, path, mode):
    try:
        return rar.RarArchive(path, mode)
    except rar.BadRarFile as exc:
        if getattr(exc, 'code', None) in (_ERAR_MISSING_PASSWORD, _ERAR_BAD_PASSWORD):
            return None  # cabeceras cifradas: ni siquiera se pueden listar
        raise ValueError('archive_damaged') from exc


def _rar_headers(rar, archive):
    # El iterador del wrapper calla los errores de cabecera; aquí solo
    # ERAR_END_ARCHIVE significa fin correcto.
    while True:
        raw = rar.RARHeaderDataEx()
        result = rar.RARReadHeaderEx(archive.handle, raw)
        if result == _ERAR_END_ARCHIVE:
            return
        if result in (_ERAR_MISSING_PASSWORD, _ERAR_BAD_PASSWORD):
            raise PermissionError('archive_encrypted')
        if result != rar.C_ERAR_SUCCESS:
            raise ValueError('archive_damaged')
        yield rar.RarHeader(archive, raw)


def _skip(rar, header):
    result = rar.RARProcessFileW(header.handle, rar.C_RAR_SKIP, rar.ffi.NULL, rar.ffi.NULL)
    if result != rar.C_ERAR_SUCCESS:
        raise ValueError('archive_damaged')


def _inspect_rar(path, cancel):
    from unrar.cffi import unrarlib as rar

    # Paso 1: listar (modo LIST: saltar entradas no descomprime nada, ni en sólidos).
    listing = []
    archive = _open_rar(rar, path, rar.C_RAR_OM_LIST_INCSPLIT)
    if archive is None:
        return _result('rar', problem='encrypted')
    try:
        with archive:
            for index, header in enumerate(_rar_headers(rar, archive)):
                _check(cancel)
                if index >= MAX_ENTRIES:
                    raise ValueError('archive_entry_limit')
                flags = header.Flags
                listing.append(dict(
                    index=index, name=header.FileNameW, flags=flags,
                    size=header.UnpSize + (header.UnpSizeHigh << 32),
                    directory=bool(flags & _RHDF_DIRECTORY),
                ))
                _skip(rar, header)
    except PermissionError:
        return _result('rar', problem='encrypted')

    files = [entry for entry in listing if not entry['directory']]
    images = [entry for entry in files if _is_image(entry['name'])]
    solid = any(entry['flags'] & _RHDF_SOLID for entry in listing)
    base = dict(images=len(images), entries=len(files),
                unpacked=sum(entry['size'] for entry in images), solid=solid)
    if any(entry['flags'] & (_RHDF_SPLITBEFORE | _RHDF_SPLITAFTER) for entry in listing):
        return _result('rar', problem='multivolume', **base)
    if not images:
        return _result('rar', problem='no_images', **base)
    first = images[0] if solid else min(images, key=lambda entry: natural_key(entry['name']))
    if first['flags'] & _RHDF_ENCRYPTED:
        return _result('rar', first=first['name'], problem='encrypted', **base)
    if first['size'] > MAX_MEMBER_BYTES:
        return _result('rar', first=first['name'], problem='too_large', **base)

    # Paso 2: recorrer hasta la imagen elegida y probarla (TEST) volcando a memoria.
    data = bytearray()
    errors = []

    def callback(msg, p1, p2):
        try:
            if msg != rar.UCM_PROCESSDATA:
                return -1  # nunca pedir contraseña ni buscar otro volumen
            if cancel is not None and cancel.is_set():
                errors.append(Cancelled())
                return -1
            data.extend(bytes(rar.ffi.buffer(rar.ffi.cast('char *', p1), p2)))
            if len(data) > first['size']:
                errors.append(ValueError('archive_damaged'))
                return -1
            return 1
        except Exception as exc:  # el callback no puede propagar excepciones a C
            errors.append(exc)
            return -1

    archive = _open_rar(rar, path, rar.C_RAR_OM_EXTRACT)
    if archive is None:
        return _result('rar', first=first['name'], problem='encrypted', **base)
    try:
        with archive:
            for index, header in enumerate(_rar_headers(rar, archive)):
                _check(cancel)
                if index < first['index']:
                    _skip(rar, header)
                    continue
                handle = rar.ffi.new_handle(callback)
                try:
                    rar.RARSetCallbackPtr(header.handle, rar.PyUNRARCALLBACKStub, handle)
                    result = rar.RARProcessFileW(header.handle, rar.C_RAR_TEST, rar.ffi.NULL, rar.ffi.NULL)
                finally:
                    rar.RARSetCallbackPtr(header.handle, rar.ffi.NULL, rar.ffi.NULL)
                if errors:
                    raise errors[0]
                if result in (_ERAR_MISSING_PASSWORD, _ERAR_BAD_PASSWORD):
                    return _result('rar', first=first['name'], problem='encrypted', **base)
                if result != rar.C_ERAR_SUCCESS or len(data) != first['size']:
                    raise ValueError('archive_damaged')
                break
    except PermissionError:
        return _result('rar', first=first['name'], problem='encrypted', **base)
    return _result('rar', first=first['name'], data=bytes(data), **base)
