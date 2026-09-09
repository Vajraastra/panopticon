"""
Clase base abstracta para templates de sitios.

Cada template implementa la lógica específica de su sitio:
  - Detectar si puede manejar una URL
  - Extraer información del artista
  - Iterar sobre todos los archivos disponibles
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from ..engine import DownloadEngine


# ── Modelo de archivo a descargar ──────────────────────────────────────────────

@dataclass
class FileInfo:
    """Representa un archivo listo para descargar."""
    url: str
    filename: str
    artist_id: str
    artist_name: str
    post_id: str
    # Metadatos opcionales del post
    post_title: str = ""
    date_published: str = ""
    # Hash remoto extraído del path del servidor (si el sitio lo expone)
    remote_hash: str = ""
    # URI canónica estable para dedup (usada como url_source en catalog.db).
    # Si está vacía, se usa `url` como clave de dedup (comportamiento Kemono).
    # Para Patreon: "patreon://media/{id}" o "patreon://attachment/{id}"
    # Para Pixiv:   "pixiv://illust/{id}/p{n}" o "pixiv://ugoira/{id}"
    url_source: str = ""
    # Headers adicionales que el engine debe incluir al descargar este archivo.
    # Pixiv requiere Referer: https://www.pixiv.net/ en todas las descargas.
    extra_headers: dict = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """Clave canónica para dedup — puede diferir de la URL de descarga."""
        return self.url_source if self.url_source else self.url


# ── Modelo de artista ──────────────────────────────────────────────────────────

@dataclass
class ArtistInfo:
    """Información del artista extraída del sitio."""
    artist_id: str
    name: str
    service: str        # nombre del servicio dentro del template (ej: "patreon")
    site: str           # nombre del template (ej: "kemono")
    url: str = ""


# ── Template base ──────────────────────────────────────────────────────────────

class SiteTemplate(ABC):
    """
    Clase base para todos los templates de descarga.

    Subclases deben definir:
      - name: str          — identificador único del sitio
      - base_url: str      — URL base
      - workers: int       — pool size recomendado para este sitio
    """

    name: str = ""
    base_url: str = ""
    workers: int = 3

    # Orden entre varias fuentes de un mismo perfil. Los sitios oficiales usan
    # el default; los archivos/mirrors declaran un número mayor para funcionar
    # como respaldo. El sort es estable, así que dentro de la misma categoría
    # se conserva el orden en que el usuario agregó las URLs.
    source_priority: int = 0

    # False conserva el reconocimiento de URLs y catálogos históricos, pero
    # impide que el servicio programe scans o descargas contra ese sitio.
    download_enabled: bool = True

    # True si el sitio expone el hash del archivo antes de descargar (ej. Kemono).
    # False obliga a descargar todo en el primer scan y deduplicar por hash local.
    provides_file_hashes: bool = False

    # Delay entre páginas de API durante el scan (segundos).
    # 0.0 = sin delay extra (Patreon y Pixiv ya tienen jitter interno).
    # Kemono lo sobreescribe a 1.0 para evitar bursts agresivos.
    scan_page_delay: float = 0.0

    # Si el scan descubre >= cooldown_threshold URLs nuevas (scan agresivo),
    # el engine espera cooldown_seconds antes de iniciar las descargas.
    # Permite que las protecciones del servidor se "olviden" del burst de API.
    cooldown_threshold: int = 200
    cooldown_seconds: float = 10.0

    # Tope rígido de descargas simultáneas del sitio. None = manda la config
    # del usuario. Un perfil NUNCA puede subir de aquí: existe para límites que
    # no son nuestros, sino de quien opera el sitio.
    max_workers: int | None = None

    # Segundos mínimos entre peticiones, AGREGADOS entre todos los workers.
    # 0.0 = sin ritmo propio; manda el delay aleatorio global de la config.
    #
    # Distíngase de `scan_page_delay`, que solo espacia las páginas del scan y
    # duerme dentro de un único generador. Esto es un contrato de tasa que el
    # engine hace cumplir sobre TODA petición del sitio —scan, descarga y
    # reintentos— con un turno compartido, así que subir los workers no lo
    # multiplica. Se pone cuando el operador del sitio pide un ritmo concreto.
    min_request_interval: float = 0.0

    # Pausa mínima desde que termina una descarga hasta que puede empezar
    # otro archivo, compartida por todos los workers. 0.0 = sin pausa extra.
    # Es distinta del ritmo de peticiones: evita la rotación rápida de muchos
    # archivos pequeños aunque el caudal y la concurrencia estén limitados.
    post_download_delay: float = 0.0

    # Techo de bytes por segundo, AGREGADO entre todos los workers.
    # 0.0 = sin techo.
    #
    # Distíngase de `min_request_interval`, que limita cuántas VECES se pide;
    # esto limita cuánto se trae. Una petición por segundo con archivos de
    # 130 MB son ~10 MB/s, casi un tera al día: cumplir un ritmo de peticiones
    # no dice nada del caudal, y es el caudal lo que le cuesta dinero a quien
    # paga el servidor. Se pone cuando el sitio es un archivo público con un
    # operador detrás.
    max_bytes_per_sec: float = 0.0

    # Tope de bytes a traer del sitio en un día natural. 0 = sin tope.
    # El caudal limita el pico; esto limita el total, que es la cifra en la que
    # piensa quien paga el ancho de banda. Se lleva en un sidecar propio
    # (`~/.cherry-dl/site_budget.json`), fuera de las bases del motor.
    daily_byte_budget: int = 0

    # Cuota móvil por sitio. 0 desactiva. El contador es global y persistente,
    # y reserva cupo antes de cada request para no cruzarlo con varios workers.
    download_window_limit: int = 0
    download_window_seconds: float = 0.0

    # Cortacircuitos opcional para firmas concretas del transporte. Errores no
    # incluidos (404, red local, un archivo aislado) reinician la racha.
    throttle_failure_codes: frozenset[str] = frozenset()
    throttle_failure_streak: int = 5
    throttle_failure_window: float = 120.0
    throttle_cooldowns: tuple[float, ...] = (900.0, 1800.0, 3600.0, 10800.0)

    # Transporte de archivos. ``http`` usa el cliente normal; ``browser_image``
    # crea un subrecurso desde ``browser_origin`` y extrae su respuesta por CDP.
    download_transport: str = "http"
    browser_origin: str = ""

    def __init__(self, engine: "DownloadEngine") -> None:
        self.engine = engine

    @classmethod
    @abstractmethod
    def can_handle(cls, url: str) -> bool:
        """Retorna True si este template puede procesar la URL dada."""
        ...

    @abstractmethod
    async def get_artist_info(self, url: str) -> ArtistInfo:
        """Extrae y retorna información del artista desde la URL."""
        ...

    async def classify_not_found(
        self, artist: ArtistInfo, file_info: FileInfo,
    ) -> str:
        """Clasifica un 404 como ``permanent``, ``awaiting`` o ``unknown``.

        Los sitios normales conservan la semántica histórica: 404 definitivo.
        Un archivador puede sobreescribirlo si publica evidencia explícita de
        que el original está anunciado pero todavía no disponible.
        """
        return "permanent"

    @abstractmethod
    def iter_files(
        self,
        artist: ArtistInfo,
        since: datetime | None = None,
    ) -> AsyncIterator[FileInfo]:
        """
        Genera todos los archivos disponibles para un artista.
        Implementa paginación interna según el sitio.

        Las subclases deben implementarlo como async generator (async def + yield).
        Declararlo como `def` aquí permite que Pyright infiera correctamente el tipo
        de retorno en los llamadores sin envolverlo en una coroutine.
        """
        ...

    def __repr__(self) -> str:
        return f"<Template:{self.name}>"


# ── Utilidad de fechas ─────────────────────────────────────────────────────────

def parse_date_utc(s: str) -> datetime | None:
    """
    Parsea una cadena ISO 8601 y la normaliza a datetime UTC sin tzinfo.

    Acepta formatos:
      - "2024-03-15T10:30:00"          (naive, asumido UTC)
      - "2024-03-15T10:30:00+00:00"    (UTC explícito)
      - "2024-03-15T10:30:00.000+00:00" (Patreon)
      - "2026-03-25 14:30:00"           (SQLite datetime('now'))
      - "2024-03-15T10:30:00Z"          (forma compacta UTC)
    Retorna None si la cadena está vacía o no es parseable.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None
