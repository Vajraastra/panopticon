"""Organización manual por proyecto, con recibos persistentes y recuperación idempotente.

Los comprimidos procesados permanecen en files como historial de descarga (igual que
los archivos purgados). El sidecar conserva miembros y estado sin alterar el schema.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import catalog
from .downloads import build_filename
from .file_routing import LAYOUT_VERSION, category, route, routed_name

ARCHIVES = {".zip", ".rar"}
STATE_DIR = ".cherry-archives"


@dataclass
class ArchiveResult:
    processed: int = 0
    added: int = 0
    duplicates: int = 0
    organized: int = 0
    audited: int = 0
    duplicates_removed: int = 0
    compacted: int = 0
    registered: int = 0
    errors: list[str] = field(default_factory=list)


def file_category(path: Path) -> str:
    """Conserva en la raíz imágenes sin extensión o con extensión desconocida."""
    group = category(path.name)
    if group != "Otros":
        return group
    with path.open("rb") as stream:
        head = stream.read(32)
    if (head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a",
                         b"BM", b"II*\x00", b"MM\x00*", b"\xff\x0a",
                         b"\x00\x00\x00\x0cJXL \r\n\x87\n"))
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
            or (head[4:8] == b"ftyp" and head[8:12] in {b"avif", b"avis"})):
        return ""
    return group


def _write(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    tmp.replace(path)


def _inside(folder: Path, relative: str) -> Path:
    path = folder / relative
    if not path.resolve().is_relative_to(folder.resolve()) or path.is_symlink():
        raise ValueError(f"Ruta fuera del proyecto: {relative}")
    return path


async def _hash(path: Path) -> str:
    stop = threading.Event()

    def calculate():
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while not stop.is_set() and (chunk := stream.read(1024 * 1024)):
                digest.update(chunk)
        return digest.hexdigest()

    task = asyncio.create_task(asyncio.to_thread(calculate))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        await task  # Cerrar el handle antes de liberar el proyecto a las descargas.
        raise


async def _move(folder: Path, state: Path, row: dict, relative: str):
    source = _inside(folder, row["filename"])
    target = _inside(folder, relative)
    if source == target:
        return
    journal = state / "move.json"
    _write(journal, {"hash": row["hash"], "old": row["filename"], "new": relative})
    await _recover_move(folder, journal)
    row["filename"] = relative


async def _recover_move(folder: Path, journal: Path):
    if not journal.exists():
        return
    item = json.loads(journal.read_text(encoding="utf-8"))
    source, target = _inside(folder, item["old"]), _inside(folder, item["new"])
    if source.exists():
        if target.exists():
            raise FileExistsError(f"Destino ocupado: {target.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
    elif not target.is_file() or await _hash(target) != item["hash"]:
        raise ValueError("No se pudo recuperar el movimiento pendiente")
    await catalog.relocate_file(folder, item["hash"], item["new"])
    journal.unlink()


async def _extract(source: Path, stage: Path):
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).with_name("archive_reader.py")),
        str(source), str(stage), stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE, **kwargs,
    )
    try:
        _, error = await proc.communicate()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode:
        raise ValueError(error.decode("utf-8", "replace").strip()[-600:])
    return json.loads((stage / "entries.json").read_text(encoding="utf-8"))


async def _process(folder, artist, row, known, state, result, emit):
    source = _inside(folder, row["filename"])
    receipt = state / (row["hash"] + ".json")
    saved = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else {}
    if not source.exists():
        return
    if await _hash(source) != row["hash"]:
        raise ValueError("El comprimido cambió desde su descarga; se conserva")
    emit(source.name)
    if not saved:
        saved = {"archive": row, "state": "pending", "entries": []}
        _write(receipt, saved)
    # El temporal se ubica en el mismo volumen; las incorporaciones son renames.
    with tempfile.TemporaryDirectory(prefix="extract-", dir=state) as tmp:
        stage = Path(tmp)
        entries = await _extract(source, stage)
        previous = saved.get("entries", [])
        for i, entry in enumerate(entries):
            if i < len(previous) and previous[i].get("hash") == entry["hash"]:
                entry.update({k: previous[i][k] for k in ("target", "counter") if k in previous[i]})
        saved = {"archive": row, "state": "processing", "entries": entries}
        _write(receipt, saved)
        for i, entry in enumerate(entries):
            file_hash = entry["hash"]
            existing = known.get(file_hash)
            if existing:
                path = _inside(folder, existing["filename"])
                if not path.is_file() or await _hash(path) != file_hash:
                    raise ValueError(f"Copia catalogada ausente o modificada: {existing['filename']}")
                entry["target"] = existing["filename"]
                result.duplicates += 1
            else:
                if "target" not in entry:
                    layout_v2 = (
                        await catalog.get_meta_int(folder, "layout_version") or 0
                    ) >= LAYOUT_VERSION
                    if layout_v2:
                        spec = route(entry["name"])
                        counter = await catalog.next_counter(folder, spec.key)
                        name = routed_name(artist, counter, entry["name"])
                    else:
                        counter = await catalog.next_counter(folder)
                        name = build_filename(artist, counter, entry["name"])
                    entry.update(target=(Path(category(name)) / name).as_posix(), counter=counter)
                    _write(receipt, saved)  # plan durable ANTES de mover
                target = _inside(folder, entry["target"])
                if target.exists():
                    if await _hash(target) != file_hash:
                        raise FileExistsError(f"Destino ocupado: {target.name}")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    (stage / f"{i}.bin").rename(target)
                await catalog.add_file(
                    folder, file_hash, entry["target"],
                    url_source=row.get("url_source"),
                    file_size=entry["size"], counter=entry["counter"],
                    content_service=row.get("content_service", ""),
                    post_id=row.get("post_id", ""), content_role="archive_member",
                )
                known[file_hash] = {"hash": file_hash, "filename": entry["target"]}
                result.added += 1
            _write(receipt, saved)
        # Verificación final: una fila de DB sola nunca autoriza borrar el ZIP/RAR.
        for entry in entries:
            if await _hash(_inside(folder, entry["target"])) != entry["hash"]:
                raise ValueError("Falló la verificación final")
        if await _hash(source) != row["hash"]:
            raise ValueError("El comprimido cambió durante el procesamiento")
        saved["state"] = "verified"
        _write(receipt, saved)
        source.unlink()
        saved["state"] = "completed"
        _write(receipt, saved)
        result.processed += 1


async def organize_project(folder: Path):
    """Organiza archivos catalogados sin extraer ni eliminar comprimidos.

El llamador debe excluir descargas/fusiones durante toda la operación.
No recorre otros proyectos ni descarga nada.
"""
    folder = folder.resolve()
    if not (folder / catalog.CATALOG_NAME).is_file():
        raise ValueError("El proyecto no tiene catálogo")
    state = _inside(folder, STATE_DIR)
    state.mkdir(exist_ok=True)
    result = ArchiveResult()
    await catalog.init_catalog(folder)
    await _recover_move(folder, state / "move.json")
    rows = await catalog.get_all_files(folder)
    for row in rows:
        path = _inside(folder, row["filename"])
        group = category(row["filename"])
        if path.parent == folder and group and path.is_file():
            group = file_category(path)
            if not group:
                continue
            await _move(folder, state, row, f"{group}/{path.name}")
            result.organized += 1
    return result


async def process_project(
    folder: Path,
    artist: str,
    emit: Callable[[str], None] = lambda _: None,
    *,
    force_audit: bool = False,
):
    """Organiza, extrae, deduplica por hash y compacta un perfil.

    El llamador debe excluir descargas/fusiones durante toda la operación.
    """
    folder = folder.resolve()
    result = await organize_project(folder)
    state = _inside(folder, STATE_DIR)
    rows = await catalog.get_all_files(folder)
    known = {row["hash"]: row for row in rows}
    compressed = _inside(folder, "Comprimidos")
    # Se admiten también paquetes que el usuario colocó manualmente aquí.
    by_name = {row["filename"]: row for row in rows}
    compressed_files = sorted(compressed.iterdir()) if compressed.exists() else []
    for path in compressed_files:
        if not path.is_file() or path.suffix.lower() not in ARCHIVES:
            continue
        row = None
        try:
            relative = path.relative_to(folder).as_posix()
            _inside(folder, relative)
            row = by_name.get(relative)
            if row is None:
                file_hash = await _hash(path)
                if file_hash in known:
                    row = dict(known[file_hash], filename=relative)
                else:
                    await catalog.add_file(folder, file_hash, relative, file_size=path.stat().st_size)
                    row = {"hash": file_hash, "filename": relative, "file_size": path.stat().st_size}
                    known[file_hash] = row
            await _process(folder, artist, row, known, state, result, emit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result.errors.append(f"{path.name}: {exc}")
            if row is not None:
                receipt = state / (row["hash"] + ".json")
                if receipt.exists():
                    saved = json.loads(receipt.read_text(encoding="utf-8"))
                    saved.update(state="error", error=str(exc))
                    _write(receipt, saved)
    try:
        from .project_maintenance import maintain_project
        maintenance, _ = await maintain_project(
            folder, artist, force_audit=force_audit, emit=emit,
        )
        result.audited += maintenance.audited
        result.duplicates_removed += maintenance.duplicates_removed
        result.compacted += maintenance.compacted
        result.registered += maintenance.registered
        result.errors.extend(maintenance.errors)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        result.errors.append(f"Mantenimiento: {exc}")
    return result


async def migrate_history(source: Path, destination: Path) -> None:
    """La fusión conserva recibos sólo si todos sus miembros viven en el destino."""
    state = source / STATE_DIR
    if not state.is_dir():
        return
    known = {row["hash"]: row for row in await catalog.get_all_files(destination)}
    dest_state = _inside(destination, STATE_DIR)
    dest_state.mkdir(exist_ok=True)
    for path in state.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("state") not in {"completed", "verified"}:
            continue
        row = data["archive"]
        members = data["entries"]
        for member in members:
            existing = known.get(member["hash"])
            if existing is None or not _inside(destination, existing["filename"]).is_file():
                break
            member["target"] = existing["filename"]
        else:
            if row["hash"] not in known:
                await catalog.add_file(
                    destination, row["hash"], "_purged_" + row["hash"],
                    url_source=row.get("url_source"), file_size=row.get("file_size"),
                    content_service=row.get("content_service", ""),
                    post_id=row.get("post_id", ""),
                    content_role=row.get("content_role", ""),
                    content_position=row.get("content_position"),
                )
            await catalog.copy_content_aliases(source, destination, row["hash"])
            _write(dest_state / path.name, data)
