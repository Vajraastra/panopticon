"""Comprueba perfiles Pawchive y actualiza las rutas verificadas de su cola.

Primero ejecuta con --report RUTA para guardar el resultado de las consultas.
Después ejecuta con --report RUTA --apply para aplicar las coincidencias.
Los archivos, el orden de las colas y las fechas de revisión quedan intactos.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import asdict
import json
from pathlib import Path
import re
import sys

import aiosqlite
import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from third_party.cherry_dl.catalog import (  # noqa: E402
    FileRoute, list_file_routes, update_file_routes,
)
from third_party.cherry_dl.config import INDEX_DB, load_config  # noqa: E402
from third_party.cherry_dl.engine import _Pacer  # noqa: E402
from third_party.cherry_dl.templates.base import ArtistInfo  # noqa: E402
from third_party.cherry_dl.templates.pawchive import (  # noqa: E402
    PawchiveTemplate, _extract_files_from_post, _PAGE_SIZE,
)

_CREATOR_URL = re.compile(r"https://pawchive\.pw/(patreon|fanbox)/user/([\w-]+)(?:[/?#].*)?", re.ASCII)
_DIRECT_HOST = re.compile(r"n[0-9]+\.pawchive\.pw")
_REPORT_VERSION = 1


class SiteUnavailable(Exception):
    """Pawchive asked this client to pause or stop."""


def _direct_file(url: str) -> bool:
    candidate = httpx.URL(url)
    return (candidate.scheme == "https" and _DIRECT_HOST.fullmatch(candidate.host) is not None
            and candidate.port is None and not candidate.userinfo
            and candidate.path.startswith("/data/"))


def _replacement_hash(template: PawchiveTemplate, row: FileRoute, url: str) -> str:
    if not _direct_file(url):
        return ""
    old_hash = template.transfer_hash(row.download_url, "")
    if not old_hash or (row.remote_hash and row.remote_hash.casefold() != old_hash):
        return ""
    return old_hash if template.transfer_hash(url, "") == old_hash else ""


class PawchiveClient:
    def __init__(self, client: httpx.AsyncClient, template: PawchiveTemplate) -> None:
        self.client = client
        self.template = template
        self.pacer = _Pacer(template.scan_policy.min_request_interval)

    async def page(self, artist: ArtistInfo, offset: int) -> list[dict]:
        url = f"https://pawchive.pw/api/v1/{artist.service}/user/{artist.artist_id}/posts"
        await self.pacer.wait()
        async with asyncio.timeout(25):
            response = await self.client.get(url, params={"o": offset}, headers={"Accept": "text/css"})
        self._check_pause(response)
        response.raise_for_status()
        return response.json()

    async def sample(self, url: str) -> str:
        for _ in range(3):
            if not _direct_file(url):
                return "redirect_unverified"
            await self.pacer.wait()
            async with asyncio.timeout(20):
                async with self.client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
                    self._check_pause(response)
                    if response.next_request is not None:
                        url = str(response.next_request.url)
                        continue
                    if response.status_code == 206:
                        if re.fullmatch(r"bytes 0-0/[1-9][0-9]*", response.headers.get("content-range", "")):
                            return "range_ok"
                        return "invalid_range"
                    if response.status_code == 200:
                        return "file_ok"
                    return f"http_{response.status_code}"
        return "redirect_limit"

    @staticmethod
    def _check_pause(response: httpx.Response) -> None:
        if response.status_code in (403, 429) or (
                response.status_code == 503 and "retry-after" in response.headers):
            raise SiteUnavailable(f"Pawchive HTTP {response.status_code}")


async def audit_source(client, template: PawchiveTemplate, artist: ArtistInfo,
                       rows: list[FileRoute]) -> dict:
    wanted: dict[str, dict[str, list[FileRoute]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        file_hash = template.transfer_hash(row.download_url, "")
        if row.post_id and file_hash and row.remote_hash.casefold() in ("", file_hash):
            wanted[row.post_id][file_hash].append(row)

    result = {"pages": 0, "status": "ok", "sample": "no_original", "changes": [],
              "unresolved": len(rows)}
    seen_posts = set()
    user_agent = template.transfer_headers(artist)["User-Agent"]
    for page in range(1000):
        try:
            posts = await client.page(artist, page * _PAGE_SIZE)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if page and status in (400, 404):
                break
            result["status"] = f"http_{status}"
            break
        except (httpx.RequestError, TimeoutError, ValueError) as exc:
            result["status"] = type(exc).__name__
            break

        result["pages"] += 1
        if not isinstance(posts, list) or any(not isinstance(p, dict) or not p.get("id") for p in posts):
            result["status"] = "invalid_page"
            break
        post_ids = {str(post["id"]) for post in posts}
        if posts and post_ids <= seen_posts:
            result["status"] = "repeated_page"
            break
        seen_posts.update(post_ids)
        for post in posts:
            post_id = str(post["id"])
            if (str(post.get("user", artist.artist_id)) != artist.artist_id
                    or post.get("service", artist.service) != artist.service):
                continue
            if post_id not in wanted and result["sample"] != "no_original":
                continue
            files = [f for f in _extract_files_from_post(post, artist, user_agent)
                     if f.remote_hash and not f.is_preview and _direct_file(f.url)]
            if files and result["sample"] == "no_original":
                try:
                    result["sample"] = await client.sample(files[0].url)
                except (httpx.RequestError, TimeoutError, ValueError) as exc:
                    result["sample"] = type(exc).__name__
            for file in files:
                matches = wanted.get(post_id, {}).pop(file.remote_hash, [])
                for row in matches:
                    file_hash = _replacement_hash(template, row, file.url)
                    if file_hash and (file.url, file_hash) != (row.download_url, row.remote_hash):
                        result["changes"].append({"row": asdict(row), "download_url": file.url,
                                                  "remote_hash": file_hash})
                if post_id in wanted and not wanted[post_id]:
                    wanted.pop(post_id)
        if len(posts) < _PAGE_SIZE or (
                wanted.keys() <= seen_posts and result["sample"] != "no_original"):
            break
    else:
        result["status"] = "page_limit"
    result["unresolved"] = len(rows) - len(result["changes"])
    return result


async def source_inventory() -> tuple[int, list[tuple]]:
    if not INDEX_DB.is_file():
        raise FileNotFoundError("No se encontró el índice de Cherry-DL")
    async with aiosqlite.connect(f"{INDEX_DB.as_uri()}?mode=ro", uri=True) as db:
        async with db.execute("SELECT COUNT(*) FROM profiles") as cursor:
            (count,) = await cursor.fetchone()
        async with db.execute("""
            SELECT p.folder_path, u.id, u.url FROM profile_urls u
            JOIN profiles p ON p.id = u.profile_id
            WHERE u.enabled = 1 AND u.site = 'pawchive'
            ORDER BY p.display_name, u.id
        """) as cursor:
            return count, await cursor.fetchall()


def _read_report(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    if any(row.get("version") != _REPORT_VERSION for row in rows):
        raise ValueError("Versión de reporte de rutas incompatible")
    return rows


def _append_report(path: Path, entry: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"version": _REPORT_VERSION, **entry}, ensure_ascii=False) + "\n")


async def audit(template: PawchiveTemplate, sources: list[tuple], report: Path, limit: int | None) -> None:
    prior = {entry["source_id"]: entry for entry in _read_report(report)}
    checked = 0
    config_headers = template.transfer_headers(ArtistInfo("", "", "", "pawchive"))
    headers = {"User-Agent": config_headers["User-Agent"], "Accept-Encoding": "identity"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers=headers) as http:
        client = PawchiveClient(http, template)
        for number, (folder, source_id, url) in enumerate(sources, start=1):
            if source_id in prior and prior[source_id]["status"] == "ok":
                continue
            if limit is not None and checked >= limit:
                break
            match = _CREATOR_URL.fullmatch(url)
            if match is None or not folder:
                _append_report(report, {"source_id": source_id, "status": "invalid_source",
                                        "sample": "not_checked", "changes": [],
                                        "pages": 0, "unresolved": 0})
                print(f"Perfil {number}/{len(sources)} | fuente no reconocida", flush=True)
                continue
            artist = ArtistInfo(match[2], "", match[1], "pawchive", url)
            rows = [row for row in await list_file_routes(Path(folder))
                    if row.profile_url_id == source_id and not _direct_file(row.download_url)]
            entry = await audit_source(client, template, artist, rows)
            _append_report(report, {"source_id": source_id, **entry})
            print(f"Perfil {number}/{len(sources)} | {entry['status']} | {entry['sample']} | "
                  f"páginas {entry['pages']} | coincidencias {len(entry['changes'])} | "
                  f"por confirmar {entry['unresolved']}", flush=True)
            checked += 1


async def apply(template: PawchiveTemplate, sources: list[tuple], report: Path) -> int:
    latest = {entry["source_id"]: entry for entry in _read_report(report)}
    if not latest:
        raise ValueError("El reporte todavía no contiene consultas")
    catalogs = defaultdict(list)
    for folder, source_id, profile_url in sources:
        if not folder or _CREATOR_URL.fullmatch(profile_url) is None or source_id not in latest:
            continue
        for change in latest[source_id]["changes"]:
            row = FileRoute(**change["row"])
            url = change["download_url"]
            file_hash = change["remote_hash"]
            verified = _replacement_hash(template, row, url)
            if row.profile_url_id != source_id or not verified or verified != file_hash:
                raise ValueError("El reporte contiene una ruta sin identidad verificada")
            catalogs[Path(folder)].append((row, url, file_hash))

    applied = already = stale = 0
    for directory, changes in catalogs.items():
        applied += await update_file_routes(directory, changes)
        current = {(row.table, row.profile_url_id, row.url_source): row
                   for row in await list_file_routes(directory)}
        for previous, url, file_hash in changes:
            found = current.get((previous.table, previous.profile_url_id, previous.url_source))
            if found and (found.download_url, found.remote_hash) == (url, file_hash):
                already += 1
            else:
                stale += 1
    print(f"Rutas aplicadas {applied} | verificadas {already} | filas que cambiaron durante la revisión {stale}")
    return int(bool(stale))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    config = load_config()
    template = PawchiveTemplate(argparse.Namespace(config=config))
    count, sources = await source_inventory()
    print(f"Perfiles {count} | fuentes Pawchive activas {len(sources)}", flush=True)
    if args.apply:
        return await apply(template, sources, args.report)
    try:
        await audit(template, sources, args.report, args.limit)
    except SiteUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
