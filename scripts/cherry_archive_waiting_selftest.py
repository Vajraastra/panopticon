"""Regresión sin red para los originales aún no archivados de Pawchive."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from third_party.cherry_dl.catalog import (
    CATALOG_NAME,
    add_pending,
    awaiting_archive_count,
    defer_until_archived,
    get_pending_files,
    init_catalog,
    pending_count,
    pending_url_exists,
    reactivate_awaiting_archive,
    remove_pending_for_profile_url,
)
from third_party.cherry_dl.templates.base import ArtistInfo, FileInfo
from third_party.cherry_dl.templates.pawchive import PawchiveTemplate


class FakeEngine:
    def __init__(self, page: str | Exception) -> None:
        self.page = page
        self.calls = 0

    async def get_text(self, _url: str) -> str:
        self.calls += 1
        if isinstance(self.page, Exception):
            raise self.page
        return self.page


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"OK  {message}")


def file_info() -> FileInfo:
    return FileInfo(
        url="https://file.pawchive.pw/data/aa/bb/hash.png?f=x.png",
        filename="x.png",
        artist_id="946522",
        artist_name="furanh",
        post_id="96303774",
        remote_hash="a" * 64,
    )


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        await init_catalog(folder)
        await add_pending(
            folder, "source", "download", "x.png", post_id="post",
            remote_hash="a" * 64, profile_url_id=7,
        )
        check(await pending_count(folder) == 1, "la fila nace activa")
        check(
            await defer_until_archived(folder, "source", recheck_seconds=3600),
            "el 404 temporal se mueve",
        )
        check(await pending_count(folder) == 0, "sale de la cola activa")
        check(await awaiting_archive_count(folder) == 1, "queda en espera")
        check(await pending_url_exists(folder, "source"), "el scan no la duplica")
        check(
            await reactivate_awaiting_archive(folder, due_only=True) == 0,
            "no vuelve antes de su fecha",
        )
        check(
            await reactivate_awaiting_archive(folder, limit=25) == 1,
            "el botón fuerza una muestra",
        )
        rows = await get_pending_files(folder)
        check(len(rows) == 1 and rows[0]["post_id"] == "post", "conserva el payload")

        await defer_until_archived(folder, "source", recheck_seconds=3600)
        db = sqlite3.connect(folder / CATALOG_NAME)
        db.execute("UPDATE awaiting_archive SET next_check = 0")
        db.commit()
        db.close()
        check(
            await reactivate_awaiting_archive(folder, due_only=True) == 1,
            "una revisión semanal vencida vuelve a la cola",
        )
        await defer_until_archived(folder, "source")
        check(
            await remove_pending_for_profile_url(folder, 7) == 1,
            "quitar la fuente limpia también su espera",
        )

    artist = ArtistInfo(
        artist_id="946522", name="furanh", service="patreon",
        site="pawchive", url="https://pawchive.pw/patreon/user/946522",
    )
    for marker in (
        "haven't archived this post yet",
        "haven’t archived this post yet",
        "Previews only",
        "haven&#39;t archived this post yet",
    ):
        template = PawchiveTemplate(FakeEngine(f"<html>{marker}</html>"))
        check(
            await template.classify_not_found(artist, file_info()) == "awaiting",
            f"reconoce el anuncio {marker!r}",
        )
    engine = FakeEngine("<html>Previews only</html>")
    template = PawchiveTemplate(engine)
    await template.classify_not_found(artist, file_info())
    await template.classify_not_found(artist, file_info())
    check(engine.calls == 1, "una publicación se comprueba una sola vez por ejecución")
    template = PawchiveTemplate(FakeEngine("<html>ordinary missing post</html>"))
    check(
        await template.classify_not_found(artist, file_info()) == "permanent",
        "un 404 sin anuncio sigue siendo definitivo",
    )
    request = httpx.Request("GET", artist.url)
    response = httpx.Response(503, request=request)
    template = PawchiveTemplate(FakeEngine(httpx.HTTPStatusError(
        "503", request=request, response=response,
    )))
    check(
        await template.classify_not_found(artist, file_info()) == "unknown",
        "un fallo al verificar conserva la fila",
    )


if __name__ == "__main__":
    asyncio.run(main())
