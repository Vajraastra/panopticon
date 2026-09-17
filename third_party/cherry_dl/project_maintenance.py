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
from typing import Callable

from . import catalog
from .file_routing import COUNTER_NAMESPACES, LAYOUT_VERSION, route

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


def _physical_files(folder: Path) -> list[Path]:
    files = []
    for path in folder.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(folder)
        if any(part.startswith(".cherry-") for part in relative.parts):
            continue
        if path.name == catalog.CATALOG_NAME or _number(relative.as_posix()) is None:
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(folder).as_posix().casefold())


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
            archive.update(filename=item["new"], counter=item["counter"])
            changed = True
        for entry in data.get("entries") or []:
            if entry.get("hash") in mapping:
                item = mapping[entry["hash"]]
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
) -> dict:
    """Construye un plan sin modificar archivos ni bases."""
    folder = folder.resolve()
    if not (folder / catalog.CATALOG_NAME).is_file():
        raise ValueError("El proyecto no tiene catálogo")
    rows = await catalog.get_all_files(folder)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_name[str(row["filename"]).casefold()].append(row)
    by_hash = {row["hash"]: row for row in rows}
    physical = _physical_files(folder)
    initial_audit = (
        await catalog.get_meta_int(folder, AUDIT_META_KEY) or 0
    ) < AUDIT_VERSION
    errors: list[str] = []
    entries: list[dict] = []
    hashed = 0

    for path in physical:
        relative = path.relative_to(folder).as_posix()
        candidates = by_name.get(relative.casefold(), [])
        emit(relative)
        expected_size = next((
            int(row.get("file_size") or 0) for row in candidates
            if int(row.get("file_size") or 0) > 0
        ), 0)
        must_hash = (
            force_audit or not candidates
            or (expected_size and expected_size != path.stat().st_size)
        )
        if must_hash:
            file_hash = await _hash(path)
            hashed += 1
            matching = [row for row in candidates if row["hash"] == file_hash]
            if candidates and not matching:
                errors.append(
                    f"El archivo cambió desde que fue catalogado: {relative}"
                )
        else:
            file_hash = candidates[0]["hash"]
        entries.append({
            "old": relative,
            "hash": file_hash,
            "size": path.stat().st_size,
            "counter": _number(relative),
            "cataloged": file_hash in by_hash,
            "hash_verified": must_hash,
        })

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
    by_route: dict[str, list[dict]] = defaultdict(list)
    for item in keepers:
        by_route[route(item["old"]).key if layout_v2 else "legacy"].append(item)
    for key, items in by_route.items():
        items.sort(key=lambda item: (item["counter"], item["old"].casefold()))
        for counter, item in enumerate(items, 1):
            item["new"] = _renumber(item["old"], counter)
            item["counter"] = counter
        counters[key] = len(items)

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
        "errors": sorted(set(errors)),
    }


async def apply_plan(plan: dict) -> dict:
    """Aplica o reanuda un plan persistido, verificándolo antes de borrar."""
    if plan.get("errors"):
        raise ValueError("El plan contiene errores; no se puede aplicar")
    folder = Path(plan["folder"]).resolve()
    state = _inside(folder, STATE_DIR)
    state.mkdir(exist_ok=True)
    plan_path = state / PLAN_NAME
    _write(plan_path, plan)
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
    plan["status"] = "applying"
    _write(plan_path, plan)

    for index, item in enumerate(plan["duplicates"]):
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

    for item in plan["files"]:
        if item["old"] == item["new"]:
            continue
        source = _inside(folder, item["old"])
        temporary = stage / f"{item['hash']}{Path(item['old']).suffix}"
        target = _inside(folder, item["new"])
        if target.is_file():
            if temporary.exists() or await _hash(target) != item["hash"]:
                raise FileExistsError(f"Estado ambiguo: {item['old']}")
            continue
        if temporary.is_file():
            if await _hash(temporary) != item["hash"]:
                raise ValueError(f"Staging dañado: {temporary.name}")
            continue
        if source.is_file():
            source.replace(temporary)
        else:
            raise FileNotFoundError(item["old"])

    for item in plan["files"]:
        if item["old"] == item["new"]:
            continue
        temporary = stage / f"{item['hash']}{Path(item['old']).suffix}"
        target = _inside(folder, item["new"])
        if temporary.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(item["new"])
            temporary.replace(target)

    await catalog.apply_project_maintenance(
        folder,
        plan["files"],
        plan["counters"],
        int(plan.get("audit_version") or 0),
    )
    _update_receipts(folder, state, plan["files"])

    for item in plan["files"]:
        target = _inside(folder, item["new"])
        if not target.is_file() or target.stat().st_size != int(item["size"]):
            raise ValueError(f"Falló la verificación final: {item['new']}")
        if item.get("hash_verified") and await _hash(target) != item["hash"]:
            raise ValueError(f"Falló el hash final: {item['new']}")

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
) -> tuple[MaintenanceResult, dict]:
    """Planea y opcionalmente aplica la auditoría/compactación del perfil."""
    folder = folder.resolve()
    pending = folder / STATE_DIR / PLAN_NAME
    if pending.is_file():
        saved = json.loads(pending.read_text(encoding="utf-8"))
        if saved.get("status") != "completed":
            plan = await apply_plan(saved) if apply else saved
        else:
            plan = await plan_project(folder, artist, force_audit=force_audit, emit=emit)
    else:
        plan = await plan_project(folder, artist, force_audit=force_audit, emit=emit)
    if apply and plan.get("status") != "completed":
        plan = await apply_plan(plan)
    result = MaintenanceResult(
        audited=(int(plan.get("reviewed") or 0) if plan.get("audit_version")
                 else int(plan.get("hashed") or 0)),
        duplicates_removed=len(plan["duplicates"]),
        compacted=sum(item["old"] != item["new"] for item in plan["files"]),
        registered=sum(not item.get("cataloged") for item in plan["files"]),
        errors=list(plan.get("errors") or []),
    )
    return result, plan
