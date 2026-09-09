"""Regresión sin red para el catálogo por lotes usado durante el scan."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "third_party"))

from cherry_dl import catalog  # noqa: E402
from cherry_dl.catalog import (  # noqa: E402
    ScanCatalogBatch,
    add_file,
    add_pending,
    awaiting_archive_count,
    defer_until_archived,
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

    with tempfile.TemporaryDirectory(prefix="cherry-catalog-batch-") as tmp:
        artist = Path(tmp) / "artist"
        await init_catalog(artist)
        await add_file(
            artist, "a" * 64, "downloaded.bin", "source-downloaded",
        )
        await add_pending(
            artist, "source-a", "signed-old", "a.bin",
            extra_headers='{"old": true}', profile_url_id=7,
        )
        await add_pending(
            artist, "source-b", "stable", "b.bin", profile_url_id=7,
        )
        await move_pending_to_back(artist, "source-a")
        await add_pending(
            artist, "source-waiting", "waiting-old", "waiting.bin",
            profile_url_id=7,
        )
        await defer_until_archived(artist, "source-waiting")

        real_db = catalog._db
        opened = 0

        def counted_db(db_path):
            nonlocal opened
            opened += 1
            return real_db(db_path)

        catalog._db = counted_db
        try:
            batch = await ScanCatalogBatch.open(artist, batch_size=250)
            check(opened == 1, "la instantánea debe usar una sola conexión")
            check(
                batch.is_downloaded("source-downloaded"),
                "debe detectar una URL ya descargada en memoria",
            )
            check(
                batch.is_downloaded("otra-url", "a" * 64),
                "debe detectar un hash ya descargado en memoria",
            )
            check(
                not batch.is_downloaded("source-new", "b" * 64),
                "una identidad nueva no debe dar falso positivo",
            )
            check(
                not await batch.stage_pending(
                    "source-waiting", "waiting-new", "waiting.bin",
                    profile_url_id=7,
                ),
                "awaiting_archive no debe reactivarse durante el scan",
            )
            check(
                not await batch.stage_pending(
                    "source-a", "signed-new", "a.bin",
                    extra_headers='{"new": true}', profile_url_id=7,
                ),
                "refrescar una fila activa no debe contarla como nueva",
            )
            check(
                await batch.stage_pending(
                    "source-repeated", "signed-1", "repeated.bin",
                    profile_url_id=7,
                ),
                "la primera aparición dentro del bloque debe ser nueva",
            )
            check(
                not await batch.stage_pending(
                    "source-repeated", "signed-2", "repeated.bin",
                    profile_url_id=7,
                ),
                "repetir una fila aún no confirmada no debe duplicarla",
            )
            for number in range(501):
                check(
                    await batch.stage_pending(
                        f"source-new-{number:03}",
                        f"download-{number:03}",
                        f"{number:03}.bin",
                        profile_url_id=7,
                    ),
                    "cada identidad inédita debe contar como nueva",
                )
            await batch.close()
            check(opened == 1, "los commits no deben abrir conexiones nuevas")
            check(
                batch.write_commits == 3,
                "503 intentos agrupados con bloques de 250 deben usar tres commits",
            )
        finally:
            catalog._db = real_db

        rows = await get_pending_files(artist, 7)
        check(
            [row["url_source"] for row in rows[:2]] == ["source-b", "source-a"],
            "el refresh no debe cambiar el FIFO existente",
        )
        refreshed = next(row for row in rows if row["url_source"] == "source-a")
        check(
            refreshed["download_url"] == "signed-new",
            "el lote debe persistir la URL firmada renovada",
        )
        check(
            refreshed["extra_headers"] == '{"new": true}',
            "el lote debe renovar también los headers firmados",
        )
        check(
            not any(row["url_source"] == "source-waiting" for row in rows),
            "awaiting_archive debe permanecer fuera de la cola activa",
        )
        check(
            await awaiting_archive_count(artist, 7) == 1,
            "la fila en espera debe conservarse intacta",
        )

    with tempfile.TemporaryDirectory(prefix="cherry-catalog-resume-") as tmp:
        artist = Path(tmp) / "artist"
        await init_catalog(artist)
        staged = asyncio.Event()

        async def interrupted_scan() -> None:
            batch = await ScanCatalogBatch.open(artist, batch_size=250)
            try:
                for number in range(17):
                    await batch.stage_pending(
                        f"resume-{number}", f"download-{number}",
                        f"{number}.bin", profile_url_id=9,
                    )
                staged.set()
                await asyncio.sleep(3600)
            finally:
                await asyncio.shield(batch.close())

        task = asyncio.create_task(interrupted_scan())
        await staged.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("la tarea debía propagar CancelledError")
        checks += 1

        rows = await get_pending_files(artist, 9)
        check(len(rows) == 17, "cancelar debe confirmar el bloque parcial")

        resumed = await ScanCatalogBatch.open(artist)
        try:
            check(
                not await resumed.stage_pending(
                    "resume-0", "download-0", "0.bin", profile_url_id=9,
                ),
                "reanudar no debe duplicar lo ya confirmado",
            )
            check(
                await resumed.stage_pending(
                    "resume-17", "download-17", "17.bin", profile_url_id=9,
                ),
                "reanudar debe aceptar el siguiente pendiente",
            )
        finally:
            await resumed.close()
        rows = await get_pending_files(artist, 9)
        check(len(rows) == 18, "la reanudación debe continuar la cola")

    print(f"OK: {checks} comprobaciones")


if __name__ == "__main__":
    asyncio.run(main())

