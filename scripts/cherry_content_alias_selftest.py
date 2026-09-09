"""Banco O4: identidad cruzada conservadora antes de descargar."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "third_party"))

from cherry_dl.catalog import (  # noqa: E402
    CATALOG_NAME,
    ScanCatalogBatch,
    add_content_alias,
    add_file,
    add_pending,
    content_alias_hash,
    defer_until_archived,
    get_pending_files,
    init_catalog,
    reactivate_awaiting_archive,
)
from cherry_dl.templates.base import ArtistInfo  # noqa: E402
from cherry_dl.templates.kemono import (  # noqa: E402
    _extract_files_from_post as kemono_files,
)
from cherry_dl.templates.patreon import (  # noqa: E402
    _extract_files_from_post as patreon_files,
)
from cherry_dl.templates.pawchive import (  # noqa: E402
    _extract_files_from_post as pawchive_files,
)


async def main() -> None:
    checks = 0

    def check(condition: bool, message: str) -> None:
        nonlocal checks
        assert condition, message
        checks += 1

    with tempfile.TemporaryDirectory(prefix="cherry-o4-") as tmp:
        root = Path(tmp)
        artist = root / "artist"
        await init_catalog(artist)
        db_path = artist / CATALOG_NAME

        with closing(sqlite3.connect(db_path)) as db:
            alias_columns = {
                row[1] for row in db.execute("PRAGMA table_info(content_aliases)")
            }
            pending_columns = {
                row[1] for row in db.execute("PRAGMA table_info(pending_queue)")
            }
        check("file_hash" in alias_columns, "crea tabla de alias ligada al hash")
        check(
            {"content_service", "original_filename", "content_role",
             "content_position", "remote_size"} <= pending_columns,
            "la cola conserva toda la identidad semántica",
        )

        hash_a = "a" * 64
        hash_b = "b" * 64
        await add_file(artist, hash_a, "Artist_00001.png", "patreon://media/1")
        await add_file(artist, hash_b, "Artist_00002.png", "patreon://media/2")

        check(
            not await add_content_alias(
                artist, "c" * 64, "patreon", "10", "cover.png", "primary", 0,
            ),
            "un alias nunca apunta a un hash no catalogado",
        )
        check(
            await add_content_alias(
                artist, hash_a, "Patreon", "10", "cover.png", "primary", 0, 123,
            ),
            "registra el alias sólo después del hash final",
        )
        check(
            await content_alias_hash(
                artist, "patreon", "10", "cover.png", 0, 123,
            ) == hash_a,
            "resuelve servicio sin depender de mayúsculas",
        )
        check(
            await content_alias_hash(
                artist, "patreon", "11", "cover.png", 0, 123,
            ) is None,
            "el basename idéntico en otro post no se omite",
        )
        check(
            await content_alias_hash(
                artist, "patreon", "10", "cover.png", 0, 999,
            ) is None,
            "un tamaño remoto contradictorio invalida el alias",
        )
        check(
            await content_alias_hash(
                artist, "patreon", "10", "cover.png", 1, 123,
            ) is None,
            "la posición evita colisiones dentro de una publicación",
        )

        batch = await ScanCatalogBatch.open(artist)
        try:
            check(
                batch.find_downloaded_hash(
                    "mirror://new", hash_b,
                    service="patreon", post_id="10",
                    original_filename="cover.png", position=0, remote_size=123,
                ) == hash_b,
                "el hash remoto tiene prioridad sobre un alias contradictorio",
            )
            check(
                batch.find_downloaded_hash(
                    "mirror://new", service="patreon", post_id="10",
                    original_filename="cover.png", position=0, remote_size=123,
                ) == hash_a,
                "el alias lógico evita transferencia cuando no hay hash remoto",
            )
            check(
                batch.find_downloaded_hash("patreon://media/2") == hash_b,
                "la URL estable permanece como última defensa pre-descarga",
            )
            await batch.stage_content_alias(
                hash_b, "patreon", "12", "source.psd", "attachment", 1, 456,
            )
        finally:
            await batch.close()
        check(
            await content_alias_hash(
                artist, "patreon", "12", "source.psd", 1, 456,
            ) == hash_b,
            "el scan persiste alias encontrados por hash remoto",
        )

        await add_pending(
            artist, "pawchive://10/0", "https://files.invalid/old", "old.png",
            profile_url_id=7,
        )
        await add_pending(
            artist,
            "pawchive://10/0",
            "https://files.invalid/a",
            "cover.png",
            post_id="10",
            content_service="patreon",
            original_filename="cover.png",
            content_role="primary",
            content_position=0,
            remote_size=123,
            profile_url_id=7,
        )
        pending = (await get_pending_files(artist, 7))[0]
        check(
            pending["download_url"] == "https://files.invalid/a",
            "un re-scan refresca URL y metadatos sin duplicar la fila",
        )
        check(pending["content_service"] == "patreon", "cola conserva servicio")
        check(pending["original_filename"] == "cover.png", "cola conserva nombre exacto")
        check(pending["content_role"] == "primary", "cola conserva rol")
        check(pending["content_position"] == 0, "cola conserva posición")
        check(pending["remote_size"] == 123, "cola conserva tamaño remoto")
        check(
            await defer_until_archived(artist, "pawchive://10/0", recheck_seconds=1),
            "puede diferir el original con su identidad completa",
        )
        check(
            await reactivate_awaiting_archive(artist, 7, limit=1) == 1,
            "reactiva el original diferido",
        )
        restored = (await get_pending_files(artist, 7))[0]
        check(
            (restored["content_service"], restored["content_position"],
             restored["remote_size"]) == ("patreon", 0, 123),
            "el ciclo de archivo preserva la identidad",
        )

        origin = ArtistInfo("creator", "Artist", "patreon", "kemono", "https://x")
        post = {
            "id": "77",
            "file": {"name": "raw:name.png", "path": "/aa/bb/" + hash_a + "/x"},
            "attachments": [{"name": "source.psd", "path": "/cc/dd/" + hash_b + "/x"}],
        }
        kemono = list(kemono_files(post, origin))
        pawchive = list(pawchive_files(post, origin))
        check(kemono[0].original_filename == "raw:name.png", "Kemono conserva nombre sin sanitizar")
        check(pawchive[0].original_filename == "raw:name.png", "Pawchive conserva nombre sin sanitizar")
        check(
            [(f.content_service, f.content_position) for f in kemono]
            == [(f.content_service, f.content_position) for f in pawchive],
            "Kemono y Pawchive producen la misma identidad posicional",
        )

        patreon_artist = ArtistInfo("creator", "Artist", "patreon", "patreon", "https://x")
        patreon_post = {
            "id": "88",
            "attributes": {"post_type": "image_file"},
            "relationships": {"images": {"data": [{"id": "m1"}]},
                              "attachments": {"data": []}},
        }
        patreon = patreon_files(
            patreon_post,
            {("media", "m1"): {"state": "ready", "download_url": "https://x/a",
                                  "file_name": "exact:name.png", "size_bytes": 321}},
            patreon_artist,
        )[0]
        check(patreon.original_filename == "exact:name.png", "Patreon conserva nombre exacto")
        check(patreon.filename == "exact_name.png", "el nombre de disco sigue siendo seguro")
        check(patreon.remote_size == 321, "Patreon propaga size_bytes")

        # Migración desde un catálogo anterior a O4.
        legacy = root / "legacy"
        legacy.mkdir()
        with closing(sqlite3.connect(legacy / CATALOG_NAME)) as db:
            db.execute("CREATE TABLE files (hash TEXT PRIMARY KEY, filename TEXT NOT NULL, url_source TEXT, date_added INTEGER NOT NULL, file_size INTEGER, counter INTEGER)")
            db.execute("CREATE TABLE pending_queue (url_source TEXT PRIMARY KEY, download_url TEXT NOT NULL, filename_hint TEXT NOT NULL, post_id TEXT, post_published TEXT, remote_hash TEXT, extra_headers TEXT, profile_url_id INTEGER, discovered_at INTEGER NOT NULL, queue_position INTEGER)")
            db.execute("CREATE TABLE awaiting_archive (url_source TEXT PRIMARY KEY, download_url TEXT NOT NULL, filename_hint TEXT NOT NULL, post_id TEXT, post_published TEXT, remote_hash TEXT, extra_headers TEXT, profile_url_id INTEGER, first_seen INTEGER NOT NULL, last_checked INTEGER NOT NULL, next_check INTEGER NOT NULL, check_count INTEGER NOT NULL DEFAULT 1)")
        await init_catalog(legacy)
        with closing(sqlite3.connect(legacy / CATALOG_NAME)) as db:
            for table in ("pending_queue", "awaiting_archive"):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                check("content_position" in columns, f"migra {table} sin perder datos")

    print(f"O4 aliases: {checks}/{checks} comprobaciones")


if __name__ == "__main__":
    asyncio.run(main())
