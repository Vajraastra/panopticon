"""Regresión sin red para el orden persistente de pending_queue."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "third_party"))

from cherry_dl.catalog import (  # noqa: E402
    CATALOG_NAME,
    add_pending,
    get_pending_files,
    init_catalog,
    move_pending_to_back,
)


async def main() -> None:
    checks = 0

    def check(condition: bool, message: str) -> None:
        nonlocal checks
        assert condition, message
        checks += 1

    with tempfile.TemporaryDirectory(prefix="cherry-queue-") as tmp:
        artist = Path(tmp) / "artist"
        await init_catalog(artist)
        for name in ("a", "b", "c"):
            await add_pending(
                artist, f"source-{name}", f"download-{name}", f"{name}.bin",
                profile_url_id=7,
            )

        rows = await get_pending_files(artist, 7)
        check([row["url_source"] for row in rows] == [
            "source-a", "source-b", "source-c",
        ], "la inserción debe conservar FIFO")

        check(
            await move_pending_to_back(artist, "source-a"),
            "debe encontrar la fila fallida",
        )
        rows = await get_pending_files(artist, 7)
        check([row["url_source"] for row in rows] == [
            "source-b", "source-c", "source-a",
        ], "un fallo debe mover el archivo al fondo")

        await move_pending_to_back(artist, "source-b")
        rows = await get_pending_files(artist, 7)
        check([row["url_source"] for row in rows] == [
            "source-c", "source-a", "source-b",
        ], "fallos sucesivos deben seguir rotando la cola")

        await add_pending(
            artist, "source-a", "download-a-refreshed", "a.bin",
            profile_url_id=7,
        )
        rows = await get_pending_files(artist, 7)
        check([row["url_source"] for row in rows] == [
            "source-c", "source-a", "source-b",
        ], "refrescar una URL firmada no debe alterar el orden")
        check(rows[1]["download_url"] == "download-a-refreshed",
              "el refresco debe conservarse")
        check(not await move_pending_to_back(artist, "missing"),
              "una fila inexistente debe reportarse sin error")

    # Simula un catalog.db anterior a queue_position.
    with tempfile.TemporaryDirectory(prefix="cherry-queue-migration-") as tmp:
        artist = Path(tmp) / "artist"
        artist.mkdir()
        db_path = artist / CATALOG_NAME
        db = sqlite3.connect(db_path)
        try:
            db.execute("""
                CREATE TABLE pending_queue (
                    url_source TEXT PRIMARY KEY, download_url TEXT NOT NULL,
                    filename_hint TEXT NOT NULL, post_id TEXT,
                    post_published TEXT, remote_hash TEXT, extra_headers TEXT,
                    profile_url_id INTEGER, discovered_at INTEGER NOT NULL
                )
            """)
            for name in ("old-a", "old-b"):
                db.execute(
                    "INSERT INTO pending_queue VALUES (?, ?, ?, '', '', '', NULL, 4, 1)",
                    (name, name, f"{name}.bin"),
                )
            db.commit()
        finally:
            # sqlite3.Connection.__exit__ confirma la transacción, pero no
            # cierra el handle; Windows no permite borrar el archivo abierto.
            db.close()

        await init_catalog(artist)
        rows = await get_pending_files(artist, 4)
        check([row["url_source"] for row in rows] == ["old-a", "old-b"],
              "la migración debe conservar el FIFO anterior")
        await move_pending_to_back(artist, "old-a")
        rows = await get_pending_files(artist, 4)
        check([row["url_source"] for row in rows] == ["old-b", "old-a"],
              "un catálogo migrado debe permitir diferir")

    print(f"OK: {checks}/9 comprobaciones")


if __name__ == "__main__":
    asyncio.run(main())
