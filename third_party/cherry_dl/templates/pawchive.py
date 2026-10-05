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

import httpx

from .base import ArtistInfo, FileInfo, SiteTemplate, parse_date_utc
from ..site_policy import ScanPolicy, SourceCategory, TransferPolicy

# ── Constantes ─────────────────────────────────────────────────────────────────

_BASE = "https://pawchive.pw"

# Los archivos viven en otro host, y solo bajo /data/. El dominio principal
# devuelve 404 para esas rutas; `img.pawchive.pw` sirve únicamente miniaturas.
_FILES = "https://file.pawchive.pw"
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

    def transfer_headers(
        self, artist: ArtistInfo, stored: dict | None = None,
    ) -> dict:
        """Inyecta la identidad vigente incluso en colas creadas antes del UA."""
        headers = dict(stored or {})
        headers["Referer"] = artist.url or (_BASE + "/")
        headers["User-Agent"] = _download_user_agent(self.engine)
        return headers

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
        """Consulta la publicación y reconoce solo los avisos de Pawchive.

        Un ``Server: ddos-guard`` no basta: también sirve 404 genuinos. La
        evidencia autoritativa es el anuncio humano de la página del post.
        Si no podemos consultar la página devolvemos ``unknown`` para conservar
        la fila activa; nunca convertimos un fallo de diagnóstico en pérdida.
        """
        if not file_info.post_id:
            return "permanent"
        cache = getattr(self, "_not_found_cache", None)
        if cache is None:
            cache = self._not_found_cache = {}
        cache_key = f"{artist.service}:{artist.artist_id}:{file_info.post_id}"
        if cache_key in cache:
            return cache[cache_key]
        post_url = (
            f"{_BASE}/{artist.service}/user/{artist.artist_id}"
            f"/post/{file_info.post_id}"
        )
        try:
            page = await self.engine.get_text(post_url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return "permanent"
            return "unknown"
        except Exception:
            return "unknown"
        text = html.unescape(page).casefold()
        state = (
            "awaiting"
            if any(marker in text for marker in _AWAITING_ARCHIVE_MARKERS)
            else "permanent"
        )
        cache[cache_key] = state
        return state

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
        download_user_agent = _download_user_agent(self.engine)

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
    preview_state = str(post.get("preview_state") or "").strip().casefold()
    post_is_preview = preview_state == "pending" or post.get("has_full") is False

    entradas: list[tuple[str, int, dict]] = []
    main_file = post.get("file")
    if main_file:
        entradas.append(("primary", 0, main_file))
    entradas.extend(
        ("attachment", position, entry)
        for position, entry in enumerate(post.get("attachments") or [], start=1)
    )

    for role, position, entrada in entradas:
        path = entrada.get("path")
        if not path:
            continue
        is_preview = bool(entrada.get("preview_only")) or post_is_preview
        original_name = entrada.get("name") or _name_from_path(path)
        filename = _safe_filename(original_name)
        yield FileInfo(
            # El `?f=` es el nombre que el sitio sugiere al navegador. Se manda
            # porque es lo que hace su propia página, y así la petición no se
            # distingue de una normal.
            url=f"{_FILES}/data{path}?f={filename}",
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
