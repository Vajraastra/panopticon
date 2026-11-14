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
  - El host de archivos corre DDoS-Guard (el principal, Cloudflare).

## El ritmo lo puso su administrador

El host de archivos empezó respondiendo 403 con un mensaje escrito a mano:
«stop before I block every single one of your IPs, either download reasonably
or contact me with your reasoning, but TBs a day is just abusive». No era un
límite automático: era una persona pidiendo moderación.

David le escribió, y el 2026-09-05 contestó con cifras concretas: **1 petición
por segundo y 5 descargas simultáneas**. También explicó el origen del 403 —
alguien estaba martillando sus servidores desde múltiples IPs; el bloqueo era
preventivo y no lo provocó este cliente.

Esas dos cifras son lo que declaran `min_request_interval` y `max_workers`, y
no se suben. El ritmo lo hace cumplir el pacer del engine, que reparte turnos
sobre un reloj único: por eso 5 workers siguen siendo 1 petición por segundo y
no 5. `scan_page_delay` se queda en 0 a propósito — el pacer ya espacia las
páginas del scan, y sumarle un sleep propio pagaría el peaje dos veces.

Un archivador público lo paga alguien. Estas cifras son un acuerdo con quien
lo paga, no un parámetro de rendimiento.

## Lo que 5 workers midieron en vivo (2026-09-05, tarde)

La primera descarga real con estas cifras terminó bloqueada. La secuencia,
tomada del log y de las fechas de los archivos en disco:

  - 17:52-17:58 — 78 archivos, **3.854 MB en 6 minutos: 10,7 MB/s**. Casi todos
    de 60-140 MB, cinco en vuelo a la vez.
  - 17:58-18:09 — el goteo: 20 archivos, 254 MB, 0,4 MB/s. La numeración del
    catálogo salta 124 posiciones en dos minutos con un solo archivo guardado,
    o sea que ya casi todo era 403.
  - 18:09-18:25 — **774 peticiones, 774 respuestas 403**, exactamente 60 por
    minuto durante 13 minutos, hasta que se acabó la cola.

El pacer cumplió su parte: 60 peticiones/minuto clavadas. Lo que no cubre el
acuerdo es el **volumen**: 1 petición por segundo a 130 MB por archivo son
~10 MB/s, que sostenidos son casi un tera al día — justo lo que su
administrador llamó abusivo la primera vez. El servidor devolvía `Server:
ddos-guard` y un cuerpo de 143 bytes en texto plano, del largo de un mensaje
escrito a mano, que el motor tiraba sin leer.

Por eso los workers bajan a 2 (decisión de David) y aparecen dos límites que el
acuerdo no cubría, porque estaba escrito en peticiones y el problema estaba en
bytes:

  - `max_bytes_per_sec = 2 MB/s` agregado. Las 3.854 MB de esa tarde habrían
    tardado 32 minutos en vez de 6, y el día entero no pasaría de 172 GB en vez
    de proyectar casi un tera.
  - Ventana móvil configurable por archivos **o GB**, lo que ocurra primero.
    El tamaño anunciado se reserva antes de iniciar archivos grandes para que
    los dos workers no crucen juntos el límite.
  - Sin presupuesto diario. David decidió retirarlo después de comprobar que
    20 archivos cargaban sin rechazo con 2 workers, 2 MB/s y pausa entre tandas.
    Si una prueba viva muestra que el volumen acumulado también molesta, se
    vuelve a activar con una cifra basada en evidencia.

El techo de caudal es nuestra estimación de lo razonable, no una cifra dada por
el administrador. Puede moverse hacia abajo sin preguntar; subirlo requiere una
nueva prueba deliberada.

David autorizó 4 MiB/s compartidos y 25 GiB / 2 horas (2026-10-02):
el cap anterior impedía alcanzar el volumen. La cuota sigue siendo estricta,
incluidas las reservas concurrentes; un archivo que no cabe espera.

El 2026-09-16 el operador cambió la política del host de archivos: los clientes
automatizados deben identificarse y no fingir ser un navegador. El transporte
CDP empezó a recibir `reason=Failed` sin estado HTTP; la misma URL respondió
`206` por HTTP directo con el User-Agent identificable de abajo. Por eso los
archivos ya no pasan por Chromium.

## `preview_only`

El listado paginado expone `preview_state: "pending"` / `has_full: false`; las
respuestas antiguas pueden traer `preview_only: true` en cada entrada. Esas
filas se entregan al servicio únicamente para registrar su estado en el
catálogo. Nunca se encolan ni se descargan: la miniatura es degradada y no
representa el archivo original. Cuando Pawchive publica el original, el mismo
identificador canónico reemplaza el marcador y entra al flujo normal.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import AsyncIterator
from urllib.parse import quote, urlsplit

import httpx

from .base import ArtistInfo, FileInfo, SiteTemplate, parse_date_utc
from ..site_policy import ScanPolicy, SourceCategory, TransferPolicy

# ── Constantes ─────────────────────────────────────────────────────────────────

_BASE = "https://pawchive.pw"

# El listado actual anuncia un nodo por archivo. Las respuestas antiguas
# siguen usando el host genérico como punto de entrada.
_FILES = "https://file.pawchive.pw"
_FILE_ORIGIN = re.compile(r"https://(?:file|n[0-9]+)\.pawchive\.pw/?", re.IGNORECASE)
_FILE_NODE = re.compile(r"n[0-9]+\.pawchive\.pw", re.IGNORECASE)
_HASH_PATH = re.compile(r"/[0-9a-f]{2,3}/[0-9a-f]{2}/([0-9a-f]{64})(?:\.[^/]*)?", re.IGNORECASE)
_SERVICES = frozenset(["patreon", "fanbox"])
_DOWNLOAD_USER_AGENT_BASE = (
    "Cherry-DL/0.1 "
    "(contact: https://github.com/Vajraastra/panopticon"
)

# URL válida: https://pawchive.pw/{service}/user/{id}
_URL_RE = re.compile(
    r"https?://pawchive\.pw/([a-z]+)/user/([^/?#]+)",
    re.IGNORECASE,
)

_PAGE_SIZE = 50  # la API devuelve 50 posts por página

_AWAITING_ARCHIVE_MARKERS = (
    "haven't archived this post yet",
    "haven’t archived this post yet",
    "previews only",
)


class _ArtistNameParser(HTMLParser):
    """Extrae el nombre canónico que Pawchive publica en la página."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.artist_name = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if self.artist_name or tag.casefold() != "meta":
            return
        values = {str(key).casefold(): value for key, value in attrs}
        if values.get("name", "").casefold() == "artist_name":
            self.artist_name = (values.get("content") or "").strip()


def _artist_name_from_page(page: str) -> str:
    parser = _ArtistNameParser()
    parser.feed(page)
    return parser.artist_name


# ── Template ───────────────────────────────────────────────────────────────────

class PawchiveTemplate(SiteTemplate):
    name = "pawchive"
    base_url = _BASE
    category = SourceCategory.REPOSITORY
    scan_policy = ScanPolicy(
        min_request_interval=1.0,
        cooldown_threshold=200,
        cooldown_seconds=30.0,
    )
    transfer_policy = TransferPolicy(
        workers=2,
        max_workers=2,
        retries=3,
        min_request_interval=1.0,
        post_download_delay=5.0,
        max_bytes_per_sec=4 * 1024**2,
        daily_byte_budget=0,
        # Fallbacks; download_service usa la ventana ajustable de config.
        download_window_limit=500,
        download_window_seconds=2 * 60 * 60,
        # HTTP directo conserva estados reales; no se arma un circuito con
        # errores ambiguos de red. Los 403/429 siguen su manejo normal.
        throttle_failure_codes=frozenset(),
        throttle_failure_streak=5,
        throttle_failure_window=120.0,
        throttle_cooldowns=(15 * 60, 30 * 60, 60 * 60, 3 * 60 * 60),
        transport="http",
    )
    provides_file_hashes = True  # el hash SHA-256 viene en el path
    scan_page_delay = 0.0        # lo espacia el pacer; sumar aquí sería doble

    def __init__(self, engine) -> None:
        super().__init__(engine)
        self._awaiting_post_pages: set[tuple[str, str, str]] = set()
        self._post_details: dict[tuple[str, str, str], dict] = {}

    def transfer_headers(
        self, artist: ArtistInfo, stored: dict | None = None,
    ) -> dict:
        """Inyecta la identidad vigente incluso en colas creadas antes del UA."""
        headers = dict(stored or {})
        headers["Referer"] = artist.url or (_BASE + "/")
        headers["User-Agent"] = _download_user_agent(self.engine)
        return headers

    def transfer_hash(self, url: str, stored_hash: str) -> str:
        """Recupera la identidad de filas guardadas con el formato anterior."""
        return stored_hash or _file_url_hash(url)

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

        canonical_url = f"{_BASE}/{service}/user/{creator_id}"
        name = ""
        try:
            # La página individual publica el nombre en
            # <meta name="artist_name" content="…">. Son ~75 KB frente a los
            # ~13 MB del índice global y resuelve justo el creador solicitado.
            name = _artist_name_from_page(
                await self.engine.get_text(canonical_url)
            )
        except Exception:
            # Compatibilidad con respuestas antiguas o fallos puntuales de la
            # página: el índice global sigue siendo el segundo camino.
            pass
        try:
            if not name:
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
            url=canonical_url,
        )

    async def classify_not_found(
        self, artist: ArtistInfo, file_info: FileInfo,
    ) -> str:
        """Distingue publicaciones retiradas, pendientes y fallos de ruta.

        Un 404 del host de archivos puede venir de un nodo en migración. Se
        conserva la fila si el post sigue publicado y no confirma la espera.
        """
        if not file_info.post_id:
            return "unknown"
        cache_key = (artist.service, artist.artist_id, file_info.post_id)
        detail = self._post_details.get(cache_key)
        if detail is not None:
            return "awaiting" if _post_is_preview(detail) else "unknown"
        if cache_key in self._awaiting_post_pages:
            return "awaiting"
        post_path = f"/{artist.service}/user/{artist.artist_id}/post/{file_info.post_id}"
        try:
            page = await self.engine.get_text(f"{_BASE}{post_path}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return "permanent"
            return "unknown"
        except Exception:
            return "unknown"
        text = html.unescape(page).casefold()
        if any(marker in text for marker in _AWAITING_ARCHIVE_MARKERS):
            self._awaiting_post_pages.add(cache_key)
            return "awaiting"
        detail = await self._get_post_detail(artist, file_info.post_id)
        return "awaiting" if detail and _post_is_preview(detail) else "unknown"

    async def refresh_not_found_route(
        self, artist: ArtistInfo, file_info: FileInfo,
    ) -> str | None:
        """Busca el mismo archivo en los metadatos obtenidos tras el 404."""
        key = (artist.service, artist.artist_id, file_info.post_id)
        detail = self._post_details.get(key)
        if not detail or _post_is_preview(detail) or not _file_url_hash(file_info.url):
            return None
        original = _url_location(file_info.url)
        candidates = [entry for entry in _extract_files_from_post(
            detail, artist, _download_user_agent(self.engine),
        ) if _url_location(entry.url) != original]
        replacement = archive_file_route(artist, file_info.url, file_info.remote_hash, candidates)
        return replacement[0] if replacement else None

    async def recheck_preview_post(
        self, artist: ArtistInfo, post_id: str,
    ) -> tuple[list[FileInfo], bool] | None:
        """Consulta una publicación en espera y devuelve solo sus originales."""
        post = await self._get_post_detail(artist, post_id)
        if post is None:
            return None
        if _post_is_preview(post):
            return [], False
        entries = list(_extract_files_from_post(post, artist, _download_user_agent(self.engine)))
        originals = [entry for entry in entries if not entry.is_preview]
        resolved = (bool(originals) or post.get("has_full") is True) and not any(
            entry.is_preview for entry in entries
        )
        return originals, resolved

    async def _get_post_detail(self, artist: ArtistInfo, post_id: str) -> dict | None:
        key = (artist.service, artist.artist_id, post_id)
        if key in self._post_details:
            return self._post_details[key]
        path = f"/{artist.service}/user/{artist.artist_id}/post/{post_id}"
        try:
            post = await self.engine.get_json(f"{_BASE}/api/v1{path}", retries=1)
        except Exception:
            return None
        if not _matches_post(post, artist, post_id) or not _valid_file_metadata(post):
            return None
        self._post_details[key] = {
            key: post.get(key) for key in (
                "id", "title", "published", "preview_state", "has_full", "file", "attachments",
            )
        }
        return self._post_details[key]

    # ── Listado de archivos ────────────────────────────────────────────────────

    async def iter_files(
        self,
        artist: ArtistInfo,
        since=None,
    ) -> AsyncIterator[FileInfo]:
        """
        Itera sobre todos los archivos del artista.

        Paginación por `?o=`, en múltiplos de 50, hasta una lista vacía o un
        400/404 en una página posterior. Con `since`, se corta en el primer
        post anterior a esa fecha: vienen del más nuevo al más antiguo.
        """
        service = artist.service
        creator_id = artist.artist_id
        offset = 0
        download_user_agent = _download_user_agent(self.engine)
        seen_posts: set[str] = set()

        while True:
            url = (
                f"{_BASE}/api/v1/{service}/user/{creator_id}"
                f"/posts?o={offset}"
            )

            # Sin sleep propio: `get_json` pasa por el pacer del engine, que
            # ya impone el turno de 1 s. Ver `scan_page_delay = 0.0` arriba.
            try:
                posts = await self.engine.get_json(url)
            except httpx.HTTPStatusError as e:
                if offset and e.response.status_code in (400, 404):
                    break
                raise

            if not isinstance(posts, list):
                raise ValueError("Pawchive devolvió una página sin lista de publicaciones")
            if not posts:
                break

            page_ids = set()
            for post in posts:
                post_id = post.get("id") if isinstance(post, dict) else None
                if (not isinstance(post_id, (str, int)) or isinstance(post_id, bool)
                        or not str(post_id).strip() or not _matches_post(post, artist, str(post_id))
                        or not _valid_file_metadata(post)
                        or not isinstance(post.get("published") or "", str)):
                    raise ValueError("Pawchive devolvió metadatos de publicación no válidos")
                page_ids.add(str(post_id))
            if page_ids <= seen_posts:
                raise ValueError("Pawchive devolvió una página sin publicaciones nuevas")

            for post in posts:
                post_id = str(post["id"])
                if post_id in seen_posts:
                    continue
                seen_posts.add(post_id)
                if since is not None:
                    pub = parse_date_utc(post.get("published", ""))
                    if pub is not None and pub < since:
                        return  # newest-first → no hay nada más reciente detrás
                for file_info in _extract_files_from_post(
                    post, artist, download_user_agent,
                ):
                    yield file_info

            offset += _PAGE_SIZE


# ── Helpers ────────────────────────────────────────────────────────────────────

def _download_user_agent(engine) -> str:
    """Identifica el cliente y declara la política efectiva a Pawchive."""
    config = getattr(engine, "config", None)
    pawchive = getattr(config, "pawchive", None)
    count = int(getattr(pawchive, "downloads_per_window", 500))
    gib = float(getattr(pawchive, "gigabytes_per_window", 25.0))
    hours = float(getattr(pawchive, "window_hours", 2.0))
    hour_word = "hour" if hours == 1 else "hours"
    if getattr(pawchive, "adaptive_quota", True):
        return (f"{_DOWNLOAD_USER_AGENT_BASE}; policy: 4 MiB/s normal, 2 MiB/s economic; "
                f"max 2 concurrent downloads; {count} successful files soft threshold; "
                f"max {gib:g} GiB raw download bodies per rolling {hours:g} {hour_word})")
    return (
        f"{_DOWNLOAD_USER_AGENT_BASE}; policy: 4 MiB/s sustained bandwidth; "
        f"max 2 concurrent downloads; max {count} successful files; "
        f"max {gib:g} GiB raw download bodies per rolling {hours:g} {hour_word})"
    )


def _post_is_preview(post: dict) -> bool:
    return (str(post.get("preview_state") or "").strip().casefold() == "pending"
            or post.get("has_full") is False)


def archive_file_route(
    artist: ArtistInfo, old_url: str, old_hash: str, candidates: list[FileInfo],
) -> tuple[str, str] | None:
    """Devuelve una ruta respaldada por el SHA y el nodo que anuncia el post."""
    file_hash = _file_url_hash(old_url)
    if not file_hash or old_hash.casefold() not in ("", file_hash):
        return None
    for candidate in candidates:
        if (candidate.is_preview or candidate.artist_id != artist.artist_id
                or candidate.content_service != artist.service
                or candidate.remote_hash != file_hash):
            continue
        if (_file_url_hash(candidate.url) == file_hash
                and _FILE_NODE.fullmatch(urlsplit(candidate.url).netloc)):
            return candidate.url, file_hash
    return None


def _file_url_hash(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if (parts.path.startswith("/data/")
            and _FILE_ORIGIN.fullmatch(f"{parts.scheme}://{parts.netloc}")):
        return _hash_from_path(parts.path.removeprefix("/data")) or ""
    return ""


def _url_location(url: str) -> tuple[str | None, str]:
    parts = urlsplit(url)
    return parts.hostname, parts.path


def _matches_post(post: object, artist: ArtistInfo, post_id: str) -> bool:
    return (isinstance(post, dict) and str(post.get("id")) == post_id
            and str(post.get("user", artist.artist_id)) == artist.artist_id
            and post.get("service", artist.service) == artist.service)


def _valid_file_metadata(post: dict) -> bool:
    file = post.get("file")
    attachments = post.get("attachments")
    if file is not None and not isinstance(file, dict):
        return False
    if attachments is not None and (not isinstance(attachments, list)
                                    or any(not isinstance(entry, dict) for entry in attachments)):
        return False
    entries = [file, *(attachments or [])] if file else attachments or []
    return all(isinstance(entry.get("path") or "", str)
               and isinstance(entry.get("name") or "", str) for entry in entries)


def _extract_files_from_post(
    post: dict, artist: ArtistInfo, download_user_agent: str,
):
    """
    Genera un FileInfo por cada archivo descargable del post: el `file`
    principal y cada uno de sus `attachments`.

    Las previews sólo producen un marcador de catálogo; el servicio evita que
    lleguen a la cola de descarga.
    """
    post_id = str(post.get("id", ""))
    post_title = post.get("title", "")
    date_published = post.get("published", "")
    request_headers = {
        "Referer": artist.url,
        "User-Agent": download_user_agent,
    }
    post_is_preview = _post_is_preview(post)

    entradas: list[tuple[str, int, dict]] = []
    main_file = post.get("file")
    if main_file:
        entradas.append(("primary", 0, main_file))
    entradas.extend(
        ("attachment", position, entry)
        for position, entry in enumerate(post.get("attachments") or [], start=1)
    )

    if post_is_preview and post_id and not any(entry.get("path") for _, _, entry in entradas):
        yield FileInfo(
            url="", filename="", artist_id=artist.artist_id, artist_name=artist.name,
            post_id=post_id, content_service=artist.service, content_role="post_archive",
            url_source=f"pawchive://{artist.service}/{artist.artist_id}/post/{post_id}/archive",
            is_preview=True, post_title=post_title, date_published=date_published,
        )
        return

    for role, position, entrada in entradas:
        path = entrada.get("path")
        if not path:
            continue
        is_preview = bool(entrada.get("preview_only")) or post_is_preview
        original_name = entrada.get("name") or _name_from_path(path)
        filename = _safe_filename(original_name)
        server = entrada.get("server")
        origin = (server.rstrip("/") if isinstance(server, str)
                  and _FILE_ORIGIN.fullmatch(server) else _FILES)
        yield FileInfo(
            url=f"{origin}/data{quote(path)}?f={quote(filename)}",
            url_source=(
                f"pawchive://{artist.service}/{artist.artist_id}/post/"
                f"{post_id}/{role}/{position}"
            ),
            filename=filename,
            artist_id=artist.artist_id,
            artist_name=artist.name,
            post_id=post_id,
            content_service=artist.service,
            original_filename=original_name,
            content_role=role,
            content_position=position,
            remote_size=entrada.get("size") or entrada.get("size_bytes"),
            is_preview=is_preview,
            post_title=post_title,
            date_published=date_published,
            remote_hash=_hash_from_path(path) or "",
            extra_headers=request_headers,
        )


def _hash_from_path(path: str) -> str | None:
    """Extrae el SHA-256 en rutas originales y segmentadas por nodo."""
    match = _HASH_PATH.fullmatch(path)
    return match[1].lower() if match else None


def _name_from_path(path: str) -> str:
    """Extrae el nombre de archivo de una ruta URL."""
    return path.split("/")[-1] or "file"


def _safe_filename(name: str) -> str:
    """Sanitiza un nombre de archivo eliminando caracteres no válidos."""
    invalid = r'\/:*?"<>|'
    for ch in invalid:
        name = name.replace(ch, "_")
    return name.strip() or "file"
