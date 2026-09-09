"""
Lectura de perfiles de `index.db` (A2) — Python puro, sin UI.

Todo lo que expone `third_party/cherry_dl/index.py` es asíncrono (aiosqlite), así
que nada de esto se puede llamar desde el hilo GUI: corre en el loop de
`CherryRuntime`. Por eso A1 era prerrequisito de A2 — incluso listar perfiles
necesita el puente.

**Aquí solo se LEE.** Regla intocable del proyecto: Panopticon nunca escribe los
archivos de cherry-dl. Eso incluye no llamar a `init_index()`, que crea tablas y
aplica migraciones, y también evitar `aiosqlite.connect()` sobre una ruta
inexistente: SQLite crearía el archivo vacío, que ya es escribir. De ahí el
`exists()` antes de cada conexión.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


def index_db_path() -> Path:
    """Ruta de `index.db` (hoy `~/.cherry-dl/`, ver deuda CF11)."""
    from third_party.cherry_dl.config import INDEX_DB
    return INDEX_DB


def index_exists(db_path: Path | None = None) -> bool:
    """Barato y síncrono: se puede llamar desde el hilo GUI."""
    return (db_path or index_db_path()).is_file()


def _check_folders(paths: list[str]) -> list[bool]:
    return [Path(p).is_dir() for p in paths]


async def load_profiles(db_path: Path | None = None) -> list[dict]:
    """Todos los perfiles del índice, más la clave `folder_exists`.

    Las claves que vienen del motor son las de `list_profiles()`: `id`,
    `display_name`, `folder_path`, `primary_site`, `created_at`, `last_checked`
    y `url_count`.

    La comprobación de carpetas va a un hilo aparte: son rutas de una unidad
    externa (`G:` hoy) y un disco dormido convierte 46 `is_dir()` en segundos de
    loop bloqueado. La GUI no se congelaría, pero sí se pararía cualquier
    descarga que estuviera corriendo en el mismo loop.
    """
    db = db_path or index_db_path()
    if not db.is_file():
        return []

    from third_party.cherry_dl.index import list_profiles

    rows = await list_profiles(db)
    if not rows:
        return []
    flags = await asyncio.to_thread(_check_folders, [r["folder_path"] for r in rows])
    from third_party.cherry_dl.catalog import awaiting_archive_count
    counts = await asyncio.gather(*(
        awaiting_archive_count(Path(row["folder_path"])) if ok
        else asyncio.sleep(0, result=0)
        for row, ok in zip(rows, flags)
    ))
    for row, ok, waiting in zip(rows, flags, counts):
        row["folder_exists"] = ok
        row["awaiting_archive_count"] = int(waiting)
    return rows


async def load_profile_detail(profile_id: int, db_path: Path | None = None) -> dict | None:
    """Un perfil con sus fuentes (`urls`), o None si ya no está en el índice.

    Se pide bajo demanda al seleccionar una fila, no de entrada para los 46:
    `get_profile()` es una consulta por perfil.
    """
    db = db_path or index_db_path()
    if not db.is_file():
        return None

    from third_party.cherry_dl.index import get_profile

    profile = await get_profile(db, profile_id)
    if profile is None:
        return None
    flags = await asyncio.to_thread(_check_folders, [profile["folder_path"]])
    profile["folder_exists"] = flags[0]
    if flags[0]:
        from third_party.cherry_dl.catalog import awaiting_archive_count
        profile["awaiting_archive_count"] = await awaiting_archive_count(
            Path(profile["folder_path"])
        )
    else:
        profile["awaiting_archive_count"] = 0
    return profile


async def load_profile_details(profile_ids: list[int], db_path: Path | None = None) -> list[dict]:
    """Varios perfiles con sus fuentes, en el orden pedido, saltando los que ya
    no estén. Lo que necesita una descarga por lotes: `run_profile_download`
    exige la clave `urls`, que `list_profiles()` no trae.
    """
    out = []
    for pid in profile_ids:
        detail = await load_profile_detail(pid, db_path)
        if detail is not None:
            out.append(detail)
    return out
