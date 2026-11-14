"""Regresión sin red para el catálogo por lotes usado durante el scan."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "third_party"))

from cherry_dl import catalog  # noqa: E402
from cherry_dl.catalog import (  # noqa: E402
    ScanCatalogBatch,
    add_file,
    add_pending,
    awaiting_archive_count,
    defer_until_archived,
    due_archive_routes,
    due_preview_posts,
    finish_preview_post_check,
    get_pending_files,
    init_catalog,
    list_file_routes,
    move_pending_to_back,
    resolve_archive_route_checks,
    update_file_routes,
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

        old = f"https://file.pawchive.pw/data/aa/aa/{'a' * 64}.png"
        current = f"https://n2.pawchive.pw/data/1aa/aa/{'a' * 64}.png"
        await add_pending(artist, "paw-active", old, "a.png", profile_url_id=7)
        await move_pending_to_back(artist, "paw-active")
        await add_pending(artist, "paw-waiting", old, "b.png", profile_url_id=7)
        await defer_until_archived(artist, "paw-waiting")

        def snapshot(table: str) -> tuple:
            with closing(sqlite3.connect(artist / catalog.CATALOG_NAME)) as db:
                return tuple(db.execute(f"SELECT * FROM {table} ORDER BY url_source"))

        tables = ("pending_queue", "awaiting_archive")
        prior = {table: snapshot(table) for table in tables}
        routes = await list_file_routes(artist)
        check({(route.table, route.url_source) for route in routes} == {
            ("pending_queue", "paw-active"), ("awaiting_archive", "paw-waiting"),
        }, "lee rutas Pawchive activas y en espera")
        check(await update_file_routes(artist, [
            (replace(routes[0], profile_url_id=8), current, "a" * 64),
        ]) == 0, "no aplica una ruta si cambió la fuente desde la lectura")
        changes = [(route, current, "a" * 64) for route in routes]
        check(await update_file_routes(artist, changes) == 2, "actualiza ambas rutas")
        check(await update_file_routes(artist, changes) == 0, "el plan es idempotente")
        for table in tables:
            expected = tuple(
                (row[0], current, *row[2:5], "a" * 64, *row[6:]) if row[1] == old else row
                for row in prior[table]
            )
            check(snapshot(table) == expected, f"{table} conserva las otras columnas y filas")

        legacy = artist.parent / "legacy"
        legacy.mkdir()
        with closing(sqlite3.connect(legacy / catalog.CATALOG_NAME)) as db:
            db.execute(catalog._CREATE_PENDING)
            db.execute(
                "INSERT INTO pending_queue(url_source, download_url, filename_hint, discovered_at) "
                "VALUES ('legacy', ?, 'c.png', 1)", (old,),
            )
            db.commit()
        (legacy_route,) = await list_file_routes(legacy)
        check(legacy_route.remote_hash == "", "una cola antigua puede traer hash nulo")
        check(await update_file_routes(legacy, [(legacy_route, current, "a" * 64)]) == 1,
              "actualiza un catálogo anterior sin tabla de espera")
        missing = artist.parent / "missing"
        check(await list_file_routes(missing) == [], "la lectura no crea un catálogo")
        check(await update_file_routes(missing, changes) == 0 and not missing.exists(),
              "una actualización nunca crea la carpeta ni el catálogo")

        previews = artist.parent / "previews"
        await init_catalog(previews)
        week = catalog.ARCHIVE_RECHECK_SECONDS
        now = 2 * week
        batch = await ScanCatalogBatch.open(previews)
        try:
            for key, post, source, marked in (
                ("marker", "100", 7, 1),
                ("attachment", "100", 7, 2),
                ("second", "200", 7, week),
                ("third", "300", 7, 3),
                ("recent-third", "300", 7, week + 1),
                ("other-source", "100", 8, 1),
                ("no-post", "", 7, 1),
            ):
                with patch.object(catalog.time, "time", return_value=marked):
                    await batch.stage_preview_only(key, key, post_id=post, profile_url_id=source)
        finally:
            await batch.close()

        with patch.object(catalog.time, "time", return_value=now):
            check(await due_preview_posts(previews, 7) == ["100", "200"],
                  "agrupa por publicación y exige que venza su marcador más reciente")
            check(await due_preview_posts(previews, 7, limit=1) == ["100"],
                  "aplica el límite a publicaciones ordenadas por antigüedad")
            check(await due_preview_posts(previews, 7, limit=0) == [],
                  "permite omitir la revisión sin abrir el catálogo")
            await finish_preview_post_check(previews, 7, "100")
            check(await due_preview_posts(previews, 7) == ["200"],
                  "una revisión pendiente pospone todos los marcadores del post")
            await finish_preview_post_check(previews, 7, "100", resolved=True)
            check(await due_preview_posts(previews, 8) == ["100"],
                  "resolver no afecta el mismo post_id de otra fuente")
            check(await due_preview_posts(missing, 7) == [], "omitir un catálogo ausente")
            await finish_preview_post_check(missing, 7, "100", resolved=True)
            check(not missing.exists(), "confirmar una revisión no crea un catálogo")

        with patch.object(catalog.time, "time", return_value=now + week):
            check(await due_preview_posts(previews, 7) == ["200", "300"],
                  "los marcadores resueltos no vuelven a programarse")

        waiting = artist.parent / "waiting"
        await init_catalog(waiting)
        for key, post, source, checked in (
            ("still-waiting", "100", 7, 1),
            ("now-ready", "100", 7, 2),
            ("changed-route", "200", 7, 3),
            ("occupied", "200", 7, 4),
            ("other-source", "100", 8, 1),
            ("missing-post", "", 7, 1),
            ("not-due", "300", 7, now),
        ):
            with patch.object(catalog.time, "time", return_value=checked):
                await add_pending(waiting, key, old, f"{key}.bin", post_id=post,
                                  profile_url_id=source)
                await defer_until_archived(waiting, key)
        await add_pending(waiting, "older-pending", old, "older.bin", profile_url_id=7)
        await add_pending(waiting, "occupied", "already-pending", "occupied.bin", profile_url_id=7)

        with patch.object(catalog.time, "time", return_value=now):
            due = await due_archive_routes(waiting, 7)
            check([row.url_source for row in due] == ["still-waiting", "now-ready", "changed-route", "occupied"],
                  "la revisión semanal elige solo la fuente y las filas con post, en orden")
            check(await due_archive_routes(waiting, 7, limit=1) == due[:1],
                  "limita la muestra sin tocar su estado")
            check(await update_file_routes(waiting, [(due[2], current, "a" * 64)]) == 1,
                  "la ruta concurrente queda visible al comparar el resultado")
            checks_plan = [(due[0], None, ""), *((row, current, "a" * 64) for row in due[1:])]
            check(await resolve_archive_route_checks(waiting, checks_plan) == 1,
                  "solo la ruta verificada e intacta pasa a la cola")
            check(await resolve_archive_route_checks(waiting, checks_plan) == 0,
                  "repetir el plan no duplica ni vuelve a posponer el mismo archivo")
            check([row.url_source for row in await due_archive_routes(waiting, 7)] == ["changed-route"],
                  "una confirmación pendiente reprograma y un cambio concurrente no se altera")
            check(len(await due_archive_routes(waiting, 8)) == 1,
                  "la revisión solo toca la fuente elegida")

        with closing(sqlite3.connect(waiting / catalog.CATALOG_NAME)) as db:
            pending = db.execute(
                "SELECT url_source, download_url, remote_hash, filename_hint, discovered_at "
                "FROM pending_queue ORDER BY queue_position",
            ).fetchall()
            check(pending[0][0] == "older-pending" and pending[1][1] == "already-pending"
                  and pending[2] == ("now-ready", current, "a" * 64, "now-ready.bin", 2),
                  "la ruta confirmada se agrega al final y conserva cuando se vio por primera vez")
            dates = db.execute(
                "SELECT first_seen, last_checked, next_check, check_count "
                "FROM awaiting_archive WHERE url_source = 'still-waiting'",
            ).fetchone()
            check(dates == (1, now, now + week, 2),
                  "una espera confirmada conserva el historial y vuelve en una semana")
            occupied = db.execute(
                "SELECT download_url, next_check FROM awaiting_archive WHERE url_source = 'occupied'",
            ).fetchone()
            check(occupied == (old, now + week), "si la cola está ocupada se conserva y pospone la espera")

        check(await due_archive_routes(legacy, 7) == [], "omite catálogos sin tabla de espera")
        check(await resolve_archive_route_checks(missing, checks_plan) == 0 and not missing.exists(),
              "verificar una ruta nunca crea un catálogo")

    print(f"OK: {checks} comprobaciones")


if __name__ == "__main__":
    asyncio.run(main())

