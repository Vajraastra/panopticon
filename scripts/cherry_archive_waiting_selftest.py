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
from third_party.cherry_dl.templates.pawchive import PawchiveTemplate, archive_file_route


class FakeEngine:
    def __init__(self, page: str | Exception, post: dict | None = None) -> None:
        self.page = page
        self.post = post
        self.calls = 0
        self.api_calls = []

    async def get_text(self, _url: str) -> str:
        self.calls += 1
        if isinstance(self.page, Exception):
            raise self.page
        return self.page

    async def get_json(self, url: str, retries: int | None = None) -> dict | None:
        self.api_calls.append((url, retries))
        return self.post


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"OK  {message}")


def file_info() -> FileInfo:
    file_hash = "a" * 64
    return FileInfo(
        url=f"https://n1.pawchive.pw/data/1aa/aa/{file_hash}.png?f=x.png",
        filename="x.png",
        artist_id="946522",
        artist_name="furanh",
        post_id="96303774",
        remote_hash=file_hash,
    )


async def check_rotated_route(artist: ArtistInfo) -> None:
    original = file_info()
    correct_hash = original.remote_hash
    post = {
        "id": original.post_id,
        "user": artist.artist_id,
        "service": artist.service,
        "has_full": True,
        "file": {"server": "https://n2.pawchive.pw", "path": f"/1bb/bb/{'b' * 64}.png"},
        "attachments": [
            {"server": "https://n3.pawchive.pw", "path": f"/1aa/aa/{correct_hash}.png",
             "preview_only": True},
            {"server": "https://n4.pawchive.pw.example", "path": f"/1aa/aa/{correct_hash}.png"},
            {"server": "https://n1.pawchive.pw", "path": f"/1aa/aa/{correct_hash}.png"},
            {"server": "https://n4.pawchive.pw", "path": f"/4aa/aa/{correct_hash}.png"},
        ],
    }
    engine = FakeEngine("<html>Published</html>", post)
    template = PawchiveTemplate(engine)
    check(await template.refresh_not_found_route(artist, original) is None,
          "una ruta nueva requiere un diagnóstico previo")
    check(await template.classify_not_found(artist, original) == "unknown",
          "el 404 conserva el archivo si la publicación sigue disponible")
    replacement = await template.refresh_not_found_route(artist, original)
    check(replacement == f"https://n4.pawchive.pw/data/4aa/aa/{correct_hash}.png?f={correct_hash}.png",
          "usa el nodo distinto con el mismo SHA y descarta miniaturas y otros hosts")
    recheck = await template.recheck_preview_post(artist, original.post_id)
    check(recheck is not None and archive_file_route(artist, original.url, original.remote_hash, recheck[0])
          == (f"https://n1.pawchive.pw/data/1aa/aa/{correct_hash}.png?f={correct_hash}.png", correct_hash),
          "la revisión puede confirmar la misma ruta y reutiliza el detalle del 404")
    original.remote_hash = "b" * 64
    check(await template.refresh_not_found_route(artist, original) is None,
          "si el hash guardado contradice la ruta no cambia la identidad")
    check(engine.calls == 1 and len(engine.api_calls) == 1,
          "recicla el detalle del mismo post sin consultas adicionales")

    foreign = PawchiveTemplate(FakeEngine("<html>Published</html>", {**post, "user": "otro"}))
    await foreign.classify_not_found(artist, file_info())
    check(await foreign.refresh_not_found_route(artist, file_info()) is None,
          "descarta metadatos de otro creador")


async def check_empty_post_recheck(artist: ArtistInfo) -> None:
    post_id = "123"
    file_hash = "e" * 64
    archived = {
        "id": post_id, "user": artist.artist_id, "service": artist.service,
        "has_full": True, "file": {
            "server": "https://n3.pawchive.pw", "path": f"/1ee/ee/{file_hash}.png",
        }, "attachments": [],
    }
    engine = FakeEngine("", {**archived, "preview_state": "pending", "file": {}})
    template = PawchiveTemplate(engine)
    check(await template.recheck_preview_post(artist, post_id) == ([], False),
          "un post todavía pendiente conserva su marcador sin adjuntos")
    engine.post = archived
    check(await template.recheck_preview_post(artist, post_id) == ([], False) and len(engine.api_calls) == 1,
          "las comprobaciones de una misma ejecución comparten el detalle válido")
    template = PawchiveTemplate(engine)
    result = await template.recheck_preview_post(artist, post_id)
    check(result is not None and result[1] and len(result[0]) == 1
          and result[0][0].dedup_key == f"pawchive://patreon/{artist.artist_id}/post/{post_id}/primary/0"
          and result[0][0].remote_hash == file_hash,
          "un post archivado devuelve el original y conserva su identidad de descarga")
    engine.post = {**archived, "attachments": [
        {"path": f"/1ff/ff/{'f' * 64}.png", "preview_only": True},
    ]}
    template = PawchiveTemplate(engine)
    result = await template.recheck_preview_post(artist, post_id)
    check(result is not None and not result[1] and len(result[0]) == 1
          and result[0][0].remote_hash == file_hash,
          "un archivo todavía en preview conserva el marcador y permite encolar el original disponible")
    engine.post = {**archived, "file": {}}
    check(await PawchiveTemplate(engine).recheck_preview_post(artist, post_id) == ([], True),
          "un post declarado completo puede quedar sin archivos")
    engine.post = {**archived, "service": "fanbox"}
    check(await PawchiveTemplate(engine).recheck_preview_post(artist, post_id) is None,
          "una respuesta de otro servicio no cierra el marcador")
    engine.post = {**archived, "file": {"path": f"/ee/ee/{file_hash}.png"}}
    result = await PawchiveTemplate(engine).recheck_preview_post(artist, post_id)
    old_url = f"https://file.pawchive.pw/data/ee/ee/{file_hash}.png"
    check(result is not None and result[0] and archive_file_route(artist, old_url, file_hash, result[0]) is None,
          "una ruta genérica sin nodo publicado no reactiva un original en la revisión automática")


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
    check(await template.refresh_not_found_route(artist, file_info()) is None and not engine.api_calls,
          "un aviso de espera no dispara otra consulta de rutas")
    post_id = file_info().post_id
    engine = FakeEngine("<html>No notice</html>", {
        "id": post_id, "preview_state": "pending", "has_full": True,
        "file": {}, "attachments": [],
    })
    template = PawchiveTemplate(engine)
    check(
        await template.classify_not_found(artist, file_info()) == "awaiting",
        "la API confirma el estado pendiente aunque la página no lo anuncie",
    )
    await template.classify_not_found(artist, file_info())
    check(engine.calls == 1 and engine.api_calls == [
        (f"https://pawchive.pw/api/v1/patreon/user/946522/post/{post_id}", 1),
    ], "reutiliza la confirmación del post y no reintenta la consulta de diagnóstico")
    template = PawchiveTemplate(FakeEngine("<html>Published</html>", {
        "id": post_id, "preview_state": "scraped", "has_full": True,
    }))
    check(await template.classify_not_found(artist, file_info()) == "unknown",
          "un 404 de archivo conserva el pendiente si la publicación sigue disponible")
    request = httpx.Request("GET", artist.url)
    response = httpx.Response(404, request=request)
    template = PawchiveTemplate(FakeEngine(httpx.HTTPStatusError(
        "404", request=request, response=response,
    )))
    check(await template.classify_not_found(artist, file_info()) == "permanent",
          "un 404 de la publicación confirma que dejó de estar accesible")
    response = httpx.Response(503, request=request)
    template = PawchiveTemplate(FakeEngine(httpx.HTTPStatusError(
        "503", request=request, response=response,
    )))
    check(
        await template.classify_not_found(artist, file_info()) == "unknown",
        "un fallo al verificar conserva la fila",
    )
    await check_rotated_route(artist)
    await check_empty_post_recheck(artist)


if __name__ == "__main__":
    asyncio.run(main())
