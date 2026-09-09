"""Template Pixiv sobre la App API autenticada con OAuth2 PKCE."""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx

from ..auth.pixiv import (
    NeedsPixivAuth,
    clear_pixiv_session,
    ensure_pixiv_access_token,
)
from .base import ArtistInfo, FileInfo, SiteTemplate, parse_date_utc
from ..site_policy import ScanPolicy, SourceCategory, TransferPolicy

_BASE = "https://www.pixiv.net"
_APP_API = "https://app-api.pixiv.net"
_DELAY_MIN = 0.5
_DELAY_MAX = 1.5

_API_HEADERS = {
    "App-OS": "ios",
    "App-OS-Version": "16.7.2",
    "App-Version": "7.19.1",
    "User-Agent": "PixivIOSApp/7.19.1 (iOS 16.7.2; iPhone12,8)",
    "Referer": "https://app-api.pixiv.net/",
    "Accept-Language": "en-US",
}
_DOWNLOAD_HEADERS = {"Referer": "https://app-api.pixiv.net/"}

_URL_RE = re.compile(
    r"https?://(?:www\.)?pixiv\.net"
    r"(?:/en)?"
    r"(?:/users/(\d+)|/member\.php\?(?:.*&)?id=(\d+))",
    re.IGNORECASE,
)
_URI_ILLUST = "pixiv://illust/{}/p{}"
_URI_UGOIRA = "pixiv://ugoira/{}"


class PixivTemplate(SiteTemplate):
    name = "pixiv"
    base_url = _BASE
    category = SourceCategory.FREE_OFFICIAL
    scan_policy = ScanPolicy(delay_min=_DELAY_MIN, delay_max=_DELAY_MAX)
    transfer_policy = TransferPolicy(workers=2, max_workers=2)
    provides_file_hashes = False

    _http: httpx.AsyncClient | None = None

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(_URL_RE.search(url))

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http is not None:
            return self._http
        access_token = await ensure_pixiv_access_token()
        self._http = httpx.AsyncClient(
            headers={**_API_HEADERS, "Authorization": f"Bearer {access_token}"},
            http2=False,
            follow_redirects=True,
            timeout=httpx.Timeout(connect=30, read=60, write=30, pool=30),
        )
        return self._http

    async def _close_client(self) -> None:
        client = self._http
        self._http = None
        if client is not None:
            await client.aclose()

    async def discard_auth_client(self) -> None:
        await self._close_client()

    async def _request_json(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
    ) -> dict:
        client = await self._get_client()
        await asyncio.sleep(random.uniform(
            self.scan_policy.delay_min or 0,
            self.scan_policy.delay_max or 0,
        ))
        response = await client.get(url, params=params)
        if response.status_code in (401, 403):
            clear_pixiv_session()
            raise NeedsPixivAuth(
                f"El acceso OAuth de Pixiv fue rechazado ({response.status_code})."
            )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("Pixiv devolvió una respuesta inválida")
        if error := body.get("error"):
            message = str(error.get("message") if isinstance(error, dict) else error)
            lowered = message.lower()
            if "oauth" in lowered or "access token" in lowered or "authentication" in lowered:
                clear_pixiv_session()
                raise NeedsPixivAuth("El acceso OAuth de Pixiv venció.")
            raise ValueError(f"Pixiv App API: {message}")
        return body

    async def get_artist_info(self, url: str) -> ArtistInfo:
        user_id = _extract_user_id(url)
        if not user_id:
            raise ValueError(f"URL no reconocida por PixivTemplate: {url}")
        body = await self._request_json(
            f"{_APP_API}/v1/user/detail",
            params={"user_id": user_id, "filter": "for_ios"},
        )
        user = body.get("user") or {}
        if not isinstance(user, dict) or not user.get("id"):
            raise ValueError(f"Pixiv no devolvió el artista {user_id}")
        return ArtistInfo(
            artist_id=user_id,
            name=str(user.get("name") or user.get("account") or user_id),
            service="pixiv",
            site=self.name,
            url=url,
        )

    async def iter_files(
        self,
        artist: ArtistInfo,
        since: datetime | None = None,
    ) -> AsyncIterator[FileInfo]:
        try:
            async for item in self._iter_all(artist, since):
                yield item
        finally:
            await self._close_client()

    async def _iter_all(
        self,
        artist: ArtistInfo,
        since: datetime | None,
    ) -> AsyncIterator[FileInfo]:
        next_url: str | None = f"{_APP_API}/v1/user/illusts"
        params: dict[str, object] | None = {
            "user_id": artist.artist_id,
            "filter": "for_ios",
        }
        while next_url:
            body = await self._request_json(next_url, params=params)
            params = None
            works = body.get("illusts") or []
            if not isinstance(works, list):
                raise ValueError("Pixiv devolvió una lista de obras inválida")
            for work in works:
                if not isinstance(work, dict):
                    continue
                published = parse_date_utc(str(work.get("create_date") or ""))
                if since is not None and published is not None and published < since:
                    continue
                async for item in self._files_for_work(work, artist):
                    yield item
            value = body.get("next_url")
            next_url = str(value) if value else None

    async def _files_for_work(
        self,
        work: dict,
        artist: ArtistInfo,
    ) -> AsyncIterator[FileInfo]:
        illust_id = str(work.get("id") or "")
        if not illust_id:
            return
        if work.get("type") == "ugoira":
            body = await self._request_json(
                f"{_APP_API}/v1/ugoira/metadata",
                params={"illust_id": illust_id},
            )
            metadata = body.get("ugoira_metadata") or {}
            zip_urls = metadata.get("zip_urls") or {}
            url = str(zip_urls.get("medium") or "")
            if url:
                url = url.replace("_ugoira600x600", "_ugoira1920x1080", 1)
                yield _make_file_info(illust_id, 0, url, work, artist, ugoira=True)
            return

        pages = work.get("meta_pages") or []
        if pages:
            for page, metadata in enumerate(pages):
                urls = metadata.get("image_urls") or {}
                url = str(urls.get("original") or "")
                if url:
                    yield _make_file_info(illust_id, page, url, work, artist)
            return

        single = work.get("meta_single_page") or {}
        url = str(single.get("original_image_url") or "")
        if url:
            yield _make_file_info(illust_id, 0, url, work, artist)


def _extract_user_id(url: str) -> str:
    match = _URL_RE.search(url)
    return (match.group(1) or match.group(2) or "") if match else ""


def _make_file_info(
    illust_id: str,
    page: int,
    url: str,
    work: dict,
    artist: ArtistInfo,
    *,
    ugoira: bool = False,
) -> FileInfo:
    filename = (
        f"{illust_id}_ugoira.zip"
        if ugoira
        else f"{illust_id}_p{page}{_ext_from_url(url)}"
    )
    return FileInfo(
        url=url,
        url_source=(
            _URI_UGOIRA.format(illust_id)
            if ugoira
            else _URI_ILLUST.format(illust_id, page)
        ),
        filename=_safe_filename(filename),
        artist_id=artist.artist_id,
        artist_name=artist.name,
        post_id=illust_id,
        content_service=artist.service,
        original_filename=filename,
        content_role="ugoira" if ugoira else "page",
        content_position=page,
        post_title=str(work.get("title") or ""),
        date_published=str(work.get("create_date") or ""),
        extra_headers=dict(_DOWNLOAD_HEADERS),
    )


def _ext_from_url(url: str) -> str:
    path = urlparse(url).path
    if "." not in path:
        return ""
    ext = "." + path.rsplit(".", 1)[-1].lower()
    return ext if len(ext) <= 5 else ""


def _safe_filename(name: str) -> str:
    for character in r'\/:*?"<>|':
        name = name.replace(character, "_")
    return name.strip() or "file"
