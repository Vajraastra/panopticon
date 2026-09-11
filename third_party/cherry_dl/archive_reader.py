"""Extractor aislado ZIP/RAR. Sólo escribe nombres internos generados, nunca rutas del paquete.

Se ejecuta con el Python del venv; cancelar el proceso cierra también la biblioteca RAR.
unrar2-cffi incluye UnRAR en su wheel Windows: no usa ejecutables del sistema.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import stat
import sys
import zipfile
import zlib
from pathlib import Path

MAX_ENTRIES = 20000
MAX_MEMBER = 16 * 1024**3
MAX_TOTAL = 100 * 1024**3
RESERVE = 256 * 1024**2


def _rar_headers(rar, archive):
    # UnRAR dll.hpp: ERAR_END_ARCHIVE=10. El iterador del wrapper silencia
    # errores de cabecera; aquí sólo ese código significa fin correcto.
    while True:
        raw = rar.RARHeaderDataEx()
        result = rar.RARReadHeaderEx(archive.handle, raw)
        if result == 10:
            return
        if result != rar.C_ERAR_SUCCESS:
            raise ValueError(f"Cabecera RAR dañada (código {result})")
        yield rar.RarHeader(archive, raw)


class Sink:
    def __init__(self, target: Path, expected: int, budget: list[int]):
        if expected < 0 or expected > MAX_MEMBER:
            raise ValueError("Entrada demasiado grande")
        if budget[0] + expected > MAX_TOTAL:
            raise ValueError("Paquete demasiado grande")
        if shutil.disk_usage(target.parent).free < expected + RESERVE:
            raise OSError("Espacio insuficiente para descomprimir")
        self.file = target.open("wb")
        self.expected, self.budget = expected, budget
        self.size = self.crc = 0
        self.sha = hashlib.sha256()

    def write(self, chunk: bytes):
        self.size += len(chunk)
        self.budget[0] += len(chunk)
        if self.size > self.expected or self.budget[0] > MAX_TOTAL:
            raise ValueError("Tamaño descomprimido fuera de límites")
        self.file.write(chunk)
        self.sha.update(chunk)
        self.crc = zlib.crc32(chunk, self.crc)

    def finish(self, name: str, crc: int | None):
        if self.size != self.expected or (crc is not None and self.crc != crc):
            raise ValueError("Contenido incompleto o CRC incorrecto")
        return {"name": name, "hash": self.sha.hexdigest(), "size": self.size}


def extract(source: Path, stage: Path) -> list[dict]:
    entries: list[dict] = []
    budget = [0]

    def sink_for(size):
        if len(entries) >= MAX_ENTRIES:
            raise ValueError("Demasiadas entradas")
        return Sink(stage / f"{len(entries)}.bin", size, budget)

    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            if len(archive.infolist()) > MAX_ENTRIES:
                raise ValueError("Demasiadas entradas")
            for info in archive.infolist():
                if info.is_dir():
                    continue
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or info.flag_bits & 1:
                    raise ValueError("Enlace o entrada protegida con contraseña")
                sink = sink_for(info.file_size)
                try:
                    with archive.open(info) as stream:
                        while chunk := stream.read(1024 * 1024):
                            sink.write(chunk)
                    entries.append(sink.finish(info.filename, info.CRC))
                finally:
                    sink.file.close()
    elif source.suffix.lower() == ".rar":
        from unrar.cffi import unrarlib as rar

        with rar.RarArchive.open_for_processing(source) as archive:
            for index, header in enumerate(_rar_headers(rar, archive)):
                if index >= MAX_ENTRIES:
                    raise ValueError("Demasiadas entradas")
                raw = header.headerDataEx
                # No reconstruir volúmenes, enlaces ni archivos cifrados automáticamente.
                if (header.Flags & 7 or raw.RedirType
                        or (header.HostOS == 3 and stat.S_ISLNK(raw.FileAttr))):
                    raise ValueError("RAR dividido, cifrado o con enlaces")
                if header.Flags & rar.FLAGS_RHDF_DIRECTORY:
                    header.skip()
                    continue
                sink = sink_for(header.UnpSize + (header.UnpSizeHigh << 32))
                errors = []

                def callback(msg, p1, p2):
                    try:
                        if msg != rar.UCM_PROCESSDATA:
                            return -1  # Nunca pedir contraseña o buscar otro volumen.
                        sink.write(bytes(rar.ffi.buffer(rar.ffi.cast("char *", p1), p2)))
                        return 1
                    except Exception as exc:
                        errors.append(exc)
                        return -1

                handle = rar.ffi.new_handle(callback)
                try:
                    rar.RARSetCallbackPtr(header.handle, rar.PyUNRARCALLBACKStub, handle)
                    result = rar.RARProcessFileW(
                        header.handle, rar.C_RAR_TEST, rar.ffi.NULL, rar.ffi.NULL,
                    )
                    if errors:
                        raise errors[0]
                    if result != rar.C_ERAR_SUCCESS:
                        raise ValueError(f"No se pudo verificar RAR (código {result})")
                    # UnRAR ya verificó CRC/BLAKE2. RAR5 puede no incluir CRC32.
                    entries.append(sink.finish(header.FileNameW, None))
                finally:
                    rar.RARSetCallbackPtr(header.handle, rar.ffi.NULL, rar.ffi.NULL)
                    sink.file.close()
    else:
        raise ValueError("Formato no soportado: sólo ZIP y RAR")
    return entries


if __name__ == "__main__":
    try:
        rows = extract(Path(sys.argv[1]), Path(sys.argv[2]))
        (Path(sys.argv[2]) / "entries.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8",
        )
    except Exception as exc:
        print(str(exc).encode("ascii", "backslashreplace").decode(), file=sys.stderr)
        sys.exit(1)
