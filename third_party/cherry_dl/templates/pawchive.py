"""
Template para Pawchive (pawchive.pw)

Pawchive se define a sí mismo como «a public archiver based on Kemono», y la
API lo confirma: es la misma, campo por campo. De ahí que este template se
parezca tanto a `kemono.py`; lo que cambia es el host, los servicios y de dónde
salen los archivos.

Verificado en vivo el 2026-09-05:
  - `/api/v1/creators` → 93.572 creadores, mismo esquema que Kemono
    ({id, name, service, indexed, updated, favorited, ever_imported}).
  - `/api/v1/{service}/user/{id}/posts?o=N` → 50 posts por página, mismos campos
    ({id, user, service, title, content, file, attachments, published, …}).
  - Solo dos servicios: patreon (68.947) y fanbox (24.625). No hay discord ni
    fantia, aunque la web los mencione.
  - El hash SHA-256 va en el path (`/cf/ea/cfea…jpg`) igual que en Kemono, así
    que el dedup por hash funciona antes de descargar.
  - Los archivos NO se sirven desde el dominio principal: van por
    `https://file.pawchive.pw/data{path}`, con soporte de Range (206).
  - El host de archivos corre DDoS-Guard (el principal, Cloudflare), así que
    aplica el mismo bypass `Accept: text/css` y las cookies DDG del engine.

## Por qué un solo worker

El host de archivos responde 403 con un mensaje escrito a mano por su
administrador: «stop before I block every single one of your IPs, either
download reasonably or contact me with your reasoning, but TBs a day is just
abusive». No es un límite automático: es una persona pidiendo moderación.

Por eso `max_workers = 1` es un tope rígido y los delays son más generosos que
en Kemono. Un archivador público lo paga alguien; si el operador dice que el
volumen le duele, el cliente se adapta.

## `preview_only`

Algunos posts traen `preview_only: true` en su `file` o en sus `attachments`.
Aparece por creador —en uno medido eran 100/100 posts, y en una muestra de 12
creadores distintos 0 de 2.964 adjuntos— lo que encaja con contenido indexado
pero no importado todavía (el índice de creadores tiene su `ever_imported`).

Esos archivos **se saltan**. Bajar una vista previa metería en la biblioteca un
archivo degradado que además, al deduplicarse por hash del contenido, contaría
como distinto del original y no se reemplazaría solo. Como el scan se repite en
cada sync, el día que el original se importe entrará sin hacer nada.
"""

from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator

import httpx

from .base import ArtistInfo, FileInfo, SiteTemplate, parse_date_utc

# ── Constantes ─────────────────────────────────────────────────────────────────

_BASE = "https://pawchive.pw"

# Los archivos viven en otro host, y solo bajo /data/. El dominio principal
# devuelve 404 para esas rutas; `img.pawchive.pw` sirve únicamente miniaturas.
_FILES = "https://file.pawchive.pw"

_SERVICES = frozenset(["patreon", "fanbox"])

# URL válida: https://pawchive.pw/{service}/user/{id}
_URL_RE = re.compile(
    r"https?://pawchive\.pw/([a-z]+)/user/([^/?#]+)",
    re.IGNORECASE,
)

_PAGE_SIZE = 50  # la API devuelve 50 posts por página


# ── Template ───────────────────────────────────────────────────────────────────

class PawchiveTemplate(SiteTemplate):
    name = "pawchive"
    base_url = _BASE
    workers = 1                  # ver "Por qué un solo worker" arriba
    max_workers = 1              # tope rígido: el perfil no puede subirlo
    provides_file_hashes = True  # el hash SHA-256 viene en el path
    scan_page_delay = 2.0        # 2 s entre páginas de API (Kemono usa 1)
    cooldown_threshold = 50      # con >50 URLs nuevas, enfriar antes de bajar
    cooldown_seconds = 120.0     # enfriamiento largo, por lo mismo

    # ── Detección ──────────────────────────────────────────────────────────────

    @classmethod
    def can_handle(cls, url: str) -> bool:
        m = _URL_RE.match(url)
        if not m:
            return False
        return m.group(1).lower() in _SERVICES

    # ── Caché de creadores (por instancia de template) ────────────────────────
    # El índice pesa ~12,8 MB, así que se pide UNA vez por sesión y se reutiliza.
    _creators_cache: dict[tuple[str, str], str] | None = None

    async def _get_creators_index(self) -> dict[tuple[str, str], str]:
        """Carga /api/v1/creators una sola vez → {(service, id): name}."""
        if PawchiveTemplate._creators_cache is not None:
            return PawchiveTemplate._creators_cache

        creators = await self.engine.get_json(f"{_BASE}/api/v1/creators")
        index: dict[tuple[str, str], str] = {}
        if isinstance(creators, list):
            for c in creators:
                key = (c.get("service", ""), str(c.get("id", "")))
                index[key] = c.get("name", "")
        PawchiveTemplate._creators_cache = index
        return index

    # ── Info del artista ───────────────────────────────────────────────────────

    async def get_artist_info(self, url: str) -> ArtistInfo:
        """Extrae service y creator_id de la URL y resuelve el nombre real."""
        m = _URL_RE.match(url)
        if not m:
            raise ValueError(f"URL no reconocida por PawchiveTemplate: {url}")

        service = m.group(1).lower()
        creator_id = m.group(2)

        name = ""
        try:
            index = await self._get_creators_index()
            name = index.get((service, creator_id), "")
        except Exception:
            # Sin índice se sigue adelante: el id sirve de nombre.
            pass

        return ArtistInfo(
            artist_id=creator_id,
            name=name or creator_id,
            service=service,
            site=self.name,
            url=f"{_BASE}/{service}/user/{creator_id}",
        )

    # ── Listado de archivos ────────────────────────────────────────────────────

    async def iter_files(
        self,
        artist: ArtistInfo,
        since=None,
    ) -> AsyncIterator[FileInfo]:
        """
        Itera sobre todos los archivos del artista.

        Paginación por `?o=`, en múltiplos de 50, hasta que la respuesta llega
        vacía o con 400/404. Con `since`, se corta en el primer post anterior a
        esa fecha: vienen del más nuevo al más antiguo.
        """
        service = artist.service
        creator_id = artist.artist_id
        offset = 0

        while True:
            url = (
                f"{_BASE}/api/v1/{service}/user/{creator_id}"
                f"/posts?o={offset}"
            )

            if self.scan_page_delay > 0:
                await asyncio.sleep(self.scan_page_delay)

            try:
                posts = await self.engine.get_json(url)
            except httpx.HTTPStatusError as e:
                # 400 / 404 en paginación = offset fuera de rango → fin normal
                if e.response.status_code in (400, 404):
                    break
                raise

            if not posts:
                break

            for post in posts:
                if since is not None:
                    pub = parse_date_utc(post.get("published", ""))
                    if pub is not None and pub < since:
                        return  # newest-first → no hay nada más reciente detrás
                for file_info in _extract_files_from_post(post, artist):
                    yield file_info

            offset += _PAGE_SIZE


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_files_from_post(post: dict, artist: ArtistInfo):
    """
    Genera un FileInfo por cada archivo descargable del post: el `file`
    principal y cada uno de sus `attachments`.

    Se saltan los marcados `preview_only` — ver la cabecera del módulo.
    """
    post_id = str(post.get("id", ""))
    post_title = post.get("title", "")
    date_published = post.get("published", "")
    referer = {"Referer": artist.url}

    entradas = []
    main_file = post.get("file")
    if main_file:
        entradas.append(main_file)
    entradas.extend(post.get("attachments") or [])

    for entrada in entradas:
        path = entrada.get("path")
        if not path or entrada.get("preview_only"):
            continue
        filename = _safe_filename(entrada.get("name") or _name_from_path(path))
        yield FileInfo(
            # El `?f=` es el nombre que el sitio sugiere al navegador. Se manda
            # porque es lo que hace su propia página, y así la petición no se
            # distingue de una normal.
            url=f"{_FILES}/data{path}?f={filename}",
            filename=filename,
            artist_id=artist.artist_id,
            artist_name=artist.name,
            post_id=post_id,
            post_title=post_title,
            date_published=date_published,
            remote_hash=_hash_from_path(path) or "",
            extra_headers=referer,
        )


def _hash_from_path(path: str) -> str | None:
    """
    Extrae el hash SHA-256 del path.

    Formato: /{2 chars}/{2 chars}/{sha256}.{ext}
    Retorna el hash en minúsculas, o None si el path no tiene esa forma.
    """
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 3 and len(parts[0]) == 2 and len(parts[1]) == 2:
        candidate = parts[2].split(".")[0]
    else:
        return None

    if len(candidate) == 64 and all(c in "0123456789abcdef" for c in candidate.lower()):
        return candidate.lower()
    return None


def _name_from_path(path: str) -> str:
    """Extrae el nombre de archivo de una ruta URL."""
    return path.split("/")[-1] or "file"


def _safe_filename(name: str) -> str:
    """Sanitiza un nombre de archivo eliminando caracteres no válidos."""
    invalid = r'\/:*?"<>|'
    for ch in invalid:
        name = name.replace(ch, "_")
    return name.strip() or "file"
