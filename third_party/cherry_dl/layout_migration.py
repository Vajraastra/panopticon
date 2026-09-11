"""Migración recuperable al layout v2 de nombres y contadores por categoría."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

from . import catalog
from .file_routing import COUNTER_NAMESPACES, LAYOUT_VERSION, route, routed_name

STATE_DIR = ".cherry-layout-v2"
PLAN_NAME = "plan.json"
_OLD_COUNTER = re.compile(r"[_-](\d{5,})$")


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


def _counter_from_name(filename: str) -> int:
    match = _OLD_COUNTER.search(Path(filename).stem)
    return int(match.group(1)) if match else 0


def _receipt_names(folder: Path) -> tuple[dict[str, str], list[Path]]:
    names: dict[str, str] = {}
    receipts: list[Path] = []
    state = folder / ".cherry-archives"
    if not state.is_dir():
        return names, receipts
    for path in state.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        archive = data.get("archive") or {}
        if archive.get("hash") and archive.get("filename"):
            names[archive["hash"]] = archive["filename"]
        for entry in data.get("entries") or []:
            if entry.get("hash") and entry.get("target"):
                names[entry["hash"]] = entry["target"]
        receipts.append(path)
    return names, receipts


def _read_rows_compatible(folder: Path) -> list[dict]:
    """Lee catálogos históricos sin forzar migraciones durante el dry-run."""
    path = folder / catalog.CATALOG_NAME
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        columns = {row[1] for row in db.execute("PRAGMA table_info(files)")}
        wanted = (
            "hash", "filename", "url_source", "date_added", "file_size",
            "counter", "is_preview", "content_service", "post_id",
            "content_role", "content_position",
        )
        select = [name for name in wanted if name in columns]
        rows = [dict(row) for row in db.execute(
            "SELECT " + ", ".join(select) + " FROM files"
        )]
    for row in rows:
        for name in wanted:
            row.setdefault(name, None)
    return rows


async def plan_project(folder: Path, artist: str) -> dict:
    folder = folder.resolve()
    rows = _read_rows_compatible(folder)
    receipt_names, receipts = _receipt_names(folder)
    grouped: dict[str, list[dict]] = defaultdict(list)
    visual: list[dict] = []
    errors: list[str] = []

    for row in rows:
        classify_name = receipt_names.get(row["hash"], row["filename"])
        spec = route(classify_name)
        item = dict(row, classify_name=classify_name, route=spec.key)
        (visual if spec.key == "visual" else grouped[spec.key]).append(item)

    updates: list[dict] = []
    counters = {name: 0 for name in COUNTER_NAMESPACES}
    for row in visual:
        counter = int(row.get("counter") or _counter_from_name(row["filename"]))
        counters["visual"] = max(counters["visual"], counter)
        old = row["filename"]
        source = _inside(folder, old)
        new = old
        updates.append({
            "hash": row["hash"], "old": old, "new": new,
            "counter": counter, "route": "visual",
            "exists": source.is_file(),
            "size": int(row.get("file_size") or (source.stat().st_size if source.is_file() else 0)),
        })

    for key, items in grouped.items():
        items.sort(key=lambda row: (
            int(row.get("counter") or 2**63 - 1),
            int(row.get("date_added") or 0),
            row["filename"].casefold(),
            row["hash"],
        ))
        for counter, row in enumerate(items, 1):
            name = routed_name(artist, counter, row["classify_name"])
            spec = route(row["classify_name"])
            new = (Path(spec.folder) / name).as_posix()
            source = _inside(folder, row["filename"])
            updates.append({
                "hash": row["hash"], "old": row["filename"], "new": new,
                "counter": counter, "route": key,
                "exists": source.is_file(),
                "size": int(row.get("file_size") or (source.stat().st_size if source.is_file() else 0)),
            })
        counters[key] = len(items)

    targets: dict[str, str] = {}
    old_paths = {item["old"].casefold() for item in updates if item["exists"]}
    for item in updates:
        if item["old"] == item["new"]:
            continue
        folded = item["new"].casefold()
        previous = targets.get(folded)
        if previous and previous != item["hash"]:
            errors.append(f"Destino repetido: {item['new']}")
        targets[folded] = item["hash"]
        target = _inside(folder, item["new"])
        if (target.exists() and item["new"].casefold() not in old_paths
                and item["old"].casefold() != folded):
            errors.append(f"Destino ocupado por archivo no catalogado: {item['new']}")

    return {
        "version": LAYOUT_VERSION,
        "project": artist,
        "folder": str(folder),
        "status": "planned",
        "updates": updates,
        "counters": counters,
        "receipts": [path.relative_to(folder).as_posix() for path in receipts],
        "errors": sorted(set(errors)),
    }


def _backup_db(folder: Path, state: Path) -> Path:
    backup = state / "catalog-before-v2.db"
    if backup.exists():
        return backup
    with sqlite3.connect((folder / catalog.CATALOG_NAME).resolve().as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(backup) as dst:
            src.backup(dst)
    return backup


def _update_receipts(folder: Path, state: Path, plan: dict) -> None:
    mapping = {item["hash"]: item for item in plan["updates"]}
    backup_dir = state / "receipt-backups"
    for relative in plan.get("receipts", []):
        path = _inside(folder, relative)
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


async def apply_plan(plan: dict) -> dict:
    if plan.get("errors"):
        raise ValueError("El plan contiene errores; no se puede aplicar")
    folder = Path(plan["folder"]).resolve()
    state = _inside(folder, STATE_DIR)
    state.mkdir(exist_ok=True)
    plan_path = state / PLAN_NAME
    _write(plan_path, plan)
    _backup_db(folder, state)
    await catalog.init_catalog(folder)
    stage = state / "stage"
    stage.mkdir(exist_ok=True)

    for item in plan["updates"]:
        if not item["exists"] or item["old"] == item["new"]:
            continue
        source = _inside(folder, item["old"])
        temporary = stage / (item["hash"] + Path(item["old"]).suffix.lower())
        target = _inside(folder, item["new"])
        if source.is_file():
            if temporary.exists() or target.exists():
                raise FileExistsError(f"Estado ambiguo al preparar {item['old']}")
            source.rename(temporary)
        elif not temporary.is_file() and not target.is_file():
            raise FileNotFoundError(item["old"])

    for item in plan["updates"]:
        if not item["exists"] or item["old"] == item["new"]:
            continue
        temporary = stage / (item["hash"] + Path(item["old"]).suffix.lower())
        target = _inside(folder, item["new"])
        if temporary.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(item["new"])
            temporary.rename(target)
        if not target.is_file() or (item["size"] and target.stat().st_size != item["size"]):
            raise ValueError(f"Falló la verificación física: {item['new']}")

    await catalog.apply_layout_version(
        folder,
        [(item["new"], item["counter"], item["hash"]) for item in plan["updates"]],
        plan["counters"],
        int(plan["version"]),
    )
    _update_receipts(folder, state, plan)

    after = {row["hash"]: row for row in await catalog.get_all_files(folder)}
    for item in plan["updates"]:
        row = after[item["hash"]]
        if row["filename"] != item["new"] or int(row.get("counter") or 0) != item["counter"]:
            raise ValueError(f"Falló la verificación del catálogo: {item['new']}")
    plan["status"] = "completed"
    _write(plan_path, plan)
    return plan


async def migrate_project(folder: Path, artist: str, apply: bool = False) -> dict:
    state = folder.resolve() / STATE_DIR
    pending = state / PLAN_NAME
    if pending.is_file():
        saved = json.loads(pending.read_text(encoding="utf-8"))
        if saved.get("status") != "completed":
            return await apply_plan(saved) if apply else saved
    plan = await plan_project(folder, artist)
    return await apply_plan(plan) if apply else plan
