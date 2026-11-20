"""Auditoría y compactación recuperable de un proyecto Cherry.

La primera ejecución reconcilia todos los archivos numerados con ``catalog.db``.
Reutiliza SHA-256 ya verificados y sólo calcula archivos no registrados o cuyo
tamaño cambió; ``force_audit`` vuelve a leer cada byte. En ambos casos conserva
la copia exacta con contador menor y compacta la numeración.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .maintenance_progress import ProgressReporter
from . import catalog
from .downloads import build_filename
from .file_routing import COUNTER_NAMESPACES, LAYOUT_VERSION, ROUTES, route, routed_name

STATE_DIR = ".cherry-maintenance"
PLAN_NAME = "plan.json"
AUDIT_VERSION = 1
AUDIT_META_KEY = "maintenance_hash_audit"
_NUMBER = re.compile(r"([_-])(\d{5,})(?=\.[^.]+$)")


@dataclass
class MaintenanceResult:
    audited: int = 0
    duplicates_removed: int = 0
    compacted: int = 0
    registered: int = 0
    errors: list[str] = field(default_factory=list)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _inside(folder: Path, relative: str) -> Path:
    path = folder / relative
    if not path.resolve().is_relative_to(folder.resolve()) or path.is_symlink():
        raise ValueError(f"Ruta fuera del proyecto: {relative}")
    return path


def _number(relative: str) -> int | None:
    match = _NUMBER.search(Path(relative).name)
    return int(match.group(2)) if match else None


def _renumber(relative: str, counter: int) -> str:
    path = Path(relative)
    name = _NUMBER.sub(
        lambda match: f"{match.group(1)}{counter:05d}", path.name, count=1,
    )
    return (path.parent / name).as_posix()


async def _physical_files(folder: Path, progress: ProgressReporter) -> list[Path]:
    files = []
    visited = 0
    progress("inventory")
    def fail(error):
        raise error
    managed = {spec.folder for spec in ROUTES if spec.folder}
    for root, dirs, names in os.walk(folder, followlinks=False, onerror=fail):
        # Pickler/curation and manually managed subtrees are not Cherry output.
        dirs[:] = ([name for name in dirs if name in managed
                    and not (Path(root) / name).is_symlink()
                    and not (Path(root) / name).is_junction()]
                   if Path(root) == folder else [])
        for name in names:
            path = Path(root) / name
            visited += 1
            if not path.is_symlink() and _number(name) is not None and path.is_file():
                files.append(path)
            progress("inventory", visited)
            if visited % 128 == 0:
                await asyncio.sleep(0)
        await asyncio.sleep(0)
    progress("inventory", visited, visited)
    return sorted(files, key=lambda item: item.relative_to(folder).as_posix().casefold())


async def _reserved_numbers(
    folder: Path, artist: str, layout_v2: bool, progress: ProgressReporter,
) -> dict[str, set[int]]:
    """Excluded named copies keep their numbers, without becoming dedup inputs."""
    reserved: dict[str, set[int]] = defaultdict(set)
    managed = {spec.folder for spec in ROUTES if spec.folder}
    visited = 0
    def fail(error):
        raise error
    for root, dirs, names in os.walk(folder, followlinks=False, onerror=fail):
        dirs[:] = [name for name in dirs if not name.startswith('.cherry-')
                   and not (Path(root) / name).is_symlink()
                   and not (Path(root) / name).is_junction()]
        relative = Path(root).relative_to(folder)
        if relative == Path('.') or (len(relative.parts) == 1 and relative.name in managed):
            continue
        for name in names:
            visited += 1
            number = _number(name)
            if number and name.casefold() in {
                build_filename(artist, number, name).casefold(),
                routed_name(artist, number, name).casefold(),
            }:
                path = Path(root) / name
                if not path.is_symlink() and path.is_file():
                    reserved[route(name).key if layout_v2 else 'legacy'].add(number)
            if visited % 128 == 0:
                progress('inventory', visited)
                await asyncio.sleep(0)
        await asyncio.sleep(0)
    return reserved


async def _hash(path: Path) -> str:
    stop = threading.Event()

    def calculate() -> str:
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
        await task
        raise


def _backup_db(folder: Path, state: Path) -> Path:
    backup = state / "catalog-before-maintenance.db"
    if backup.exists():
        return backup
    source = (folder / catalog.CATALOG_NAME).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(source, uri=True)) as src:
        with closing(sqlite3.connect(backup)) as dst:
            src.backup(dst)
    return backup


def _update_receipts(folder: Path, state: Path, files: list[dict]) -> None:
    mapping = {item["hash"]: item for item in files}
    receipts = folder / ".cherry-archives"
    if not receipts.is_dir():
        return
    backup_dir = state / "receipt-backups"
    for path in receipts.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        archive = data.get("archive") or {}
        if archive.get("hash") in mapping:
            item = mapping[archive["hash"]]
            if (archive.get("filename"), archive.get("counter")) != (item["new"], item["counter"]):
                archive.update(filename=item["new"], counter=item["counter"])
                changed = True
        for entry in data.get("entries") or []:
            if entry.get("hash") in mapping:
                item = mapping[entry["hash"]]
                if (entry.get("target"), entry.get("counter")) != (item["new"], item["counter"]):
                    entry.update(target=item["new"], counter=item["counter"])
                    changed = True
        if changed:
            backup = backup_dir / path.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(path, backup)
            _write(path, data)


async def plan_project(
    folder: Path,
    artist: str,
    *,
    force_audit: bool = False,
    emit: Callable[[str], None] = lambda _: None,
    progress: ProgressReporter | None = None,
    native_observation=None,
    native_reserved=None,
) -> dict:
    """Construye un plan sin modificar archivos ni bases."""
    progress = progress or ProgressReporter()
    folder = folder.resolve()
    if not (folder / catalog.CATALOG_NAME).is_file():
        raise ValueError("El proyecto no tiene catálogo")
    progress("catalog_read")
    rows = await catalog.get_all_files(folder)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_name[str(row["filename"]).casefold()].append(row)
    by_hash = {row["hash"]: row for row in rows}
    physical = await _physical_files(folder, progress)
    initial_audit = (
        await catalog.get_meta_int(folder, AUDIT_META_KEY) or 0
    ) < AUDIT_VERSION
    errors: list[str] = []
    entries: list[dict] = []
    hashed = 0

    progress("hash", 0, len(physical))
    for position, path in enumerate(physical, 1):
        progress("hash", position - 1, len(physical), path.name)
        if position % 128 == 0:
            await asyncio.sleep(0)
        relative = path.relative_to(folder).as_posix()
        candidates = by_name.get(relative.casefold(), [])
        emit(relative)
        expected_size = next((
            int(row.get("file_size") or 0) for row in candidates
            if int(row.get("file_size") or 0) > 0
        ), 0)
        native=await native_observation(path,candidates) if native_observation is not None else None
        if native is not None:
            expected_size=native["size"]
        must_hash = (
            force_audit or not candidates
            or (expected_size and expected_size != path.stat().st_size)
        )
        if must_hash:
            file_hash = await _hash(path)
            hashed += 1
            matching = [row for row in candidates if row["hash"] == file_hash]
            if native is not None and file_hash==native["digest"]:
                matching=candidates
            if candidates and not matching:
                errors.append(
                    f"El archivo cambió desde que fue catalogado: {relative}"
                )
        else:
            file_hash = native["digest"] if native is not None else candidates[0]["hash"]
        entries.append({
            "old": relative,
            "hash": file_hash,
            "catalog_hash": native["catalog_hash"] if native is not None else file_hash,
            "catalog_size": native["catalog_size"] if native is not None else path.stat().st_size,
            "size": path.stat().st_size,
            "counter": _number(relative),
            "cataloged": file_hash in by_hash,
            "hash_verified": must_hash,
        })

    progress("hash", len(physical), len(physical))
    progress("plan")
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in entries:
        grouped[item["hash"]].append(item)

    keepers: list[dict] = []
    duplicates: list[dict] = []
    for file_hash, copies in grouped.items():
        copies.sort(key=lambda item: (item["counter"], item["old"].casefold()))
        keeper = copies[0]
        if len(copies) > 1 and not keeper["hash_verified"]:
            actual = await _hash(_inside(folder, keeper["old"]))
            hashed += 1
            keeper["hash_verified"] = True
            if actual != file_hash:
                errors.append(
                    f"El archivo cambió desde que fue catalogado: {keeper['old']}"
                )
        keeper["cataloged"] = file_hash in by_hash
        keepers.append(keeper)
        duplicates.extend(copies[1:])

    layout_v2 = (
        await catalog.get_meta_int(folder, "layout_version") or 0
    ) >= LAYOUT_VERSION
    counters = ({name: 0 for name in COUNTER_NAMESPACES}
                if layout_v2 else {"legacy": 0})
    reserved = await _reserved_numbers(folder, artist, layout_v2, progress)
    if native_reserved is not None:
        for key,numbers in (await native_reserved(artist,layout_v2,physical)).items():
            reserved[key].update(numbers)
    for key, numbers in reserved.items():
        counters[key] = max(numbers)
    by_route: dict[str, list[dict]] = defaultdict(list)
    for item in keepers:
        by_route[route(item["old"]).key if layout_v2 else "legacy"].append(item)
    for key, items in by_route.items():
        items.sort(key=lambda item: (item["counter"], item["old"].casefold()))
        counter = 0
        for item in items:
            counter += 1
            # A copied selection may share its original's existing number.
            # Keep that binding; never reuse a selected number for another file.
            while counter in reserved.get(key, ()) and counter != item["counter"]:
                counter += 1
            item["new"] = _renumber(item["old"], counter)
            item["counter"] = counter
        counters[key] = max(counters[key], counter)

    occupied = {
        path.relative_to(folder).as_posix().casefold(): path
        for path in physical
    }
    movable = {item["old"].casefold() for item in keepers + duplicates}
    targets: set[str] = set()
    for item in keepers:
        target = item["new"].casefold()
        if target in targets:
            errors.append(f"Destino repetido: {item['new']}")
        targets.add(target)
        if target in occupied and target not in movable:
            errors.append(f"Destino ocupado: {item['new']}")

    return {
        "version": 1,
        "audit_version": AUDIT_VERSION if initial_audit or force_audit else 0,
        "reviewed": len(physical),
        "hashed": hashed,
        "layout_v2": layout_v2,
        "project": artist,
        "folder": str(folder),
        "status": "planned",
        "files": keepers,
        "duplicates": duplicates,
        "counters": counters,
        "reserved_counters": {key: sorted(values) for key, values in reserved.items()},
        "errors": sorted(set(errors)),
    }


async def apply_plan(
    plan: dict,
    *,
    progress: ProgressReporter | None = None,
    move_file: Callable[[Path, Path, dict], Awaitable[bool]] | None = None,
    native_review: bool = False,
) -> dict:
    """Aplica o reanuda un plan persistido, verificándolo antes de borrar."""
    progress = progress or ProgressReporter()
    if native_review and move_file is None:
        raise ValueError("Native requiere un callback de movimiento; no hay fallback")
    if native_review and plan.get("duplicates"):
        raise ValueError("Duplicados Native requieren revisión sin purga automática")
    if plan.get("errors"):
        raise ValueError("El plan contiene errores; no se puede aplicar")
    folder = Path(plan["folder"]).resolve()
    state = _inside(folder, STATE_DIR)
    state.mkdir(exist_ok=True)
    plan_path = state / PLAN_NAME
    _write(plan_path, plan)
    progress("backup")
    _backup_db(folder, state)
    stage = state / "stage"
    quarantine = state / "duplicates"
    stage.mkdir(exist_ok=True)
    quarantine.mkdir(exist_ok=True)
    if plan.get("status") == "verified":
        shutil.rmtree(quarantine)
        plan["status"] = "completed"
        _write(plan_path, plan)
        return plan
    resuming = plan.get("status") == "applying"
    plan["status"] = "applying"
    _write(plan_path, plan)

    progress("deduplicate", 0, len(plan["duplicates"]))
    for index, item in enumerate(plan["duplicates"]):
        progress("deduplicate", index, len(plan["duplicates"]), item["old"])
        if index % 128 == 0:
            await asyncio.sleep(0)
        source = _inside(folder, item["old"])
        parked = quarantine / f"{index:06d}-{item['hash']}{Path(item['old']).suffix}"
        if parked.is_file():
            if await _hash(parked) != item["hash"]:
                raise ValueError(f"Cuarentena dañada: {parked.name}")
            continue
        if source.is_file():
            source.replace(parked)
        else:
            raise FileNotFoundError(item["old"])

    progress("deduplicate", len(plan["duplicates"]), len(plan["duplicates"]))
    renamed = [item for item in plan["files"] if item["old"] != item["new"]]
    progress("stage", 0, len(renamed))
    if not plan.get("staging_complete"):
        movable = {os.path.normcase(str(_inside(folder, item["old"]))) for item in renamed}
        # Validate the complete set first. Targets occupied by another planned
        # source are normal; a separate publication phase vacates them all.
        for item in renamed:
            source = _inside(folder, item["old"])
            temporary = Path(item["native_staging"]) if item.get("native_staging") else stage / f"{item['hash']}{Path(item['old']).suffix}"
            target = _inside(folder, item["new"])
            if temporary.exists():
                if not temporary.is_file() or await _hash(temporary) != item["hash"]:
                    raise ValueError(f"Staging dañado: {temporary.name}")
                if source.exists():
                    raise FileExistsError(f"Origen reaparecido: {item['old']}")
            elif not source.is_file():
                raise FileNotFoundError(item["old"])
            if target.exists() and os.path.normcase(str(target)) not in movable:
                raise FileExistsError(f"Destino ocupado: {item['new']}")
        for position, item in enumerate(renamed):
            progress("stage", position, len(renamed), item["new"])
            if position % 128 == 0:
                await asyncio.sleep(0)
            source = _inside(folder, item["old"])
            temporary = Path(item["native_staging"]) if item.get("native_staging") else stage / f"{item['hash']}{Path(item['old']).suffix}"
            if temporary.is_file():
                continue
            if move_file is None:
                source.rename(temporary)
            elif await move_file(source, temporary, item) is not True:
                if native_review:
                    raise ValueError("Movimiento Native no confirmado; no hay fallback")
                source.rename(temporary)
        # This one durable boundary distinguishes old sources from targets
        # already published after a cancellation/crash, without per-file writes.
        plan["staging_complete"] = True
        _write(plan_path, plan)

    progress("stage", len(renamed), len(renamed))
    progress("rename", 0, len(renamed))
    for position, item in enumerate(renamed):
        progress("rename", position, len(renamed), item["new"])
        if position % 128 == 0:
            await asyncio.sleep(0)
        temporary = Path(item["native_staging"]) if item.get("native_staging") else stage / f"{item['hash']}{Path(item['old']).suffix}"
        target = _inside(folder, item["new"])
        if temporary.is_file():
            if resuming and await _hash(temporary) != item["hash"]:
                raise ValueError(f"Staging dañado: {temporary.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(item["new"])
            if move_file is None:
                temporary.rename(target)
            elif await move_file(temporary, target, item) is not True:
                if native_review:
                    raise ValueError("Movimiento Native no confirmado; no hay fallback")
                temporary.rename(target)
        elif not target.is_file() or await _hash(target) != item["hash"]:
            raise ValueError(f"Destino pendiente o modificado: {item['new']}")

    progress("rename", len(renamed), len(renamed))
    progress("catalog_write")
    await catalog.apply_project_maintenance(
        folder,
        [dict(item,hash=item.get("catalog_hash",item["hash"]),size=item.get("catalog_size",item["size"])) for item in plan["files"]],
        plan["counters"],
        int(plan.get("audit_version") or 0),
        progress=progress,
    )
    progress("receipts")
    _update_receipts(folder, state, [dict(item,hash=item.get("catalog_hash",item["hash"])) for item in plan["files"]])

    progress("verify", 0, len(plan["files"]))
    for position, item in enumerate(plan["files"]):
        progress("verify", position, len(plan["files"]), item["new"])
        if position % 128 == 0:
            await asyncio.sleep(0)
        target = _inside(folder, item["new"])
        if not target.is_file() or target.stat().st_size != int(item["size"]):
            raise ValueError(f"Falló la verificación final: {item['new']}")
        if item.get("hash_verified") and await _hash(target) != item["hash"]:
            raise ValueError(f"Falló el hash final: {item['new']}")

    progress("verify", len(plan["files"]), len(plan["files"]))
    plan["status"] = "verified"
    _write(plan_path, plan)
    shutil.rmtree(quarantine)
    plan["status"] = "completed"
    _write(plan_path, plan)
    return plan


async def maintain_project(
    folder: Path,
    artist: str,
    *,
    force_audit: bool = False,
    apply: bool = True,
    emit: Callable[[str], None] = lambda _: None,
    progress: ProgressReporter | None = None,
    move_file: Callable[[Path, Path, dict], Awaitable[bool]] | None = None,
    before_apply: Callable[[dict], Awaitable[None]] | None = None,
    after_apply: Callable[[dict], Awaitable[None]] | None = None,
    native_review: bool = False,
    native_observation=None,
    native_reserved=None,
) -> tuple[MaintenanceResult, dict]:
    """Planea y opcionalmente aplica la auditoría/compactación del perfil."""
    progress = progress or ProgressReporter()
    folder = folder.resolve()
    pending = folder / STATE_DIR / PLAN_NAME
    if pending.is_file():
        saved = json.loads(pending.read_text(encoding="utf-8"))
        if saved.get("status") != "completed":
            plan = saved
        else:
            plan = await plan_project(folder, artist, force_audit=force_audit, emit=emit, progress=progress,native_observation=native_observation,native_reserved=native_reserved)
    else:
        plan = await plan_project(folder, artist, force_audit=force_audit, emit=emit, progress=progress,native_observation=native_observation,native_reserved=native_reserved)
    if native_review and plan.get("status") != "planned":
        raise ValueError("Plan histórico Native requiere recuperación vigente")
    if apply and plan.get("status") != "completed":
        if before_apply is not None:
            await before_apply(plan)
        plan = await apply_plan(plan, progress=progress, move_file=move_file,native_review=native_review)
        if after_apply is not None:
            await after_apply(plan)
    result = MaintenanceResult(
        audited=(int(plan.get("reviewed") or 0) if plan.get("audit_version")
                 else int(plan.get("hashed") or 0)),
        duplicates_removed=len(plan["duplicates"]),
        compacted=sum(item["old"] != item["new"] for item in plan["files"]),
        registered=sum(not item.get("cataloged") for item in plan["files"]),
        errors=list(plan.get("errors") or []),
    )
    return result, plan
