"""Auditoría aislada de una descarga real del perfil Hoovesart.

Copia index.db y catalog.db a un temporal, conserva sólo la fuente Pawchive y
cancela después del primer resultado de archivo. No modifica la cola real.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "third_party")]

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


async def main() -> int:
    from cherry_dl import config as cherry_config
    from cherry_dl import site_throttle

    real_index = cherry_config.INDEX_DB
    audit_root = Path(tempfile.mkdtemp(prefix="cherry-hoovesart-audit-"))
    audit_index = audit_root / "index.db"
    audit_folder = audit_root / "Hoovesart"
    audit_folder.mkdir()
    shutil.copy2(real_index, audit_index)

    with sqlite3.connect(audit_index) as db:
        row = db.execute(
            "SELECT id, folder_path FROM profiles "
            "WHERE lower(display_name) = 'hoovesart'"
        ).fetchone()
        if row is None:
            raise RuntimeError("No existe el perfil Hoovesart")
        profile_id, real_folder = row
        shutil.copy2(Path(real_folder) / "catalog.db", audit_folder / "catalog.db")
        db.execute(
            "UPDATE profiles SET folder_path = ? WHERE id = ?",
            (str(audit_folder), profile_id),
        )
        db.execute(
            "UPDATE profile_urls SET enabled = CASE WHEN site = 'pawchive' "
            "THEN 1 ELSE 0 END WHERE profile_id = ?",
            (profile_id,),
        )
        db.commit()

    cherry_config.INDEX_DB = audit_index
    site_throttle.THROTTLE_FILE = audit_root / "site_throttle.json"

    # Se importan después de redirigir INDEX_DB: download_service toma esa
    # constante al cargar el módulo.
    from cherry_dl.config import load_config
    from cherry_dl.download_service import (
        Counters, Log, WorkerDone, WorkerProgress, run_profile_download,
    )
    from cherry_dl.downloads import _decode_profile_filter
    from cherry_dl.index import get_profile
    from cherry_dl.templates.pawchive import PawchiveTemplate

    profile = await get_profile(audit_index, profile_id)
    if profile is None:
        raise RuntimeError("La copia del perfil no se pudo leer")
    _groups, ext_filter = _decode_profile_filter(profile.get("ext_filter", ""))
    cfg = load_config()
    cfg.workers = 1
    cfg.daily_limit_enabled = False
    PawchiveTemplate.workers = 1
    PawchiveTemplate.max_workers = 1

    done = asyncio.Event()
    last_counters = None
    progress_marks: dict[int, int] = {}

    def emit(event) -> None:
        nonlocal last_counters
        if isinstance(event, Log):
            print(f"[{event.level}] {event.msg}")
        elif isinstance(event, WorkerProgress):
            percent = int(event.done * 100 / event.total) if event.total else 0
            mark = percent // 10
            if mark > progress_marks.get(event.slot, -1):
                progress_marks[event.slot] = mark
                print(
                    f"[WorkerProgress] slot={event.slot} "
                    f"{event.done}/{event.total} ({percent}%)"
                )
        else:
            print(f"[{type(event).__name__}] {event}")
        if isinstance(event, Counters):
            last_counters = event
        if isinstance(event, WorkerDone):
            done.set()

    task = asyncio.create_task(run_profile_download(
        profile,
        workers=1,
        ext_filter=ext_filter,
        emit=emit,
        config=cfg,
    ))
    waiter = asyncio.create_task(done.wait())
    try:
        finished, _ = await asyncio.wait(
            {task, waiter}, timeout=420.0, return_when=asyncio.FIRST_COMPLETED,
        )
        if not finished:
            raise TimeoutError("La auditoría no obtuvo un resultado en 7 minutos")
        if waiter in finished and not task.done():
            # WorkerDone se emite después de catalogar/quitar de la cola clonada.
            await asyncio.sleep(0)
            task.cancel()
        try:
            summary = await task
        except asyncio.CancelledError:
            summary = last_counters
        print(f"AUDIT_RESULT={summary}")
        return 0
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        logging.shutdown()
        shutil.rmtree(audit_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
