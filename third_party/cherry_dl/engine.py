"""
Engine de descargas paralelas.

Implementa un pool de workers async donde cada slot libre inmediatamente
toma la siguiente tarea de la cola, sin esperar a que los otros terminen.

Manejo de DDoS-Guard (DDG):
  - Accept: text/css  — bypass DDG, solo para requests al API de kemono.cr.
    NO se envía en descargas de archivos: el CDN responde 500 con ese header.
  - Cookies DDG persistidas entre sesiones (~/.cherry-dl/session.json)
  - Retry con backoff exponencial via tenacity
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
import time
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Callable
from urllib.parse import urlsplit

import httpx
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import UserConfig, load_session, save_session


# ── Helpers de I/O en thread ───────────────────────────────────────────────────

# Cuánto se acumula en RAM antes de volcar al .tmp. Acota la memoria por
# descarga a este valor en vez de al tamaño del archivo, y mantiene las
# escrituras en unas pocas por MB en lugar de una por chunk de 64 KiB.
_FLUSH_EVERY = 8 * 1024 * 1024

# Tasa por debajo de la cual una descarga se considera estancada, a efectos del
# presupuesto de `total_timeout`. Ver el cálculo del budget en `_do_download`.
_MIN_RATE = 256 * 1024   # 256 KiB/s


def _write_chunks(fh: BinaryIO, hasher, parts: list[bytes]) -> None:
    """
    Vuelca al .tmp los chunks acumulados y los suma al hash incremental.

    Se ejecuta en un thread executor para no bloquear el event loop: write() es
    I/O síncrona y sha256 es CPU, y a 10 MB/s esto se llama poco más de una vez
    por segundo.
    """
    for part in parts:
        hasher.update(part)
        fh.write(part)


def _finalize_download(fh: BinaryIO, tmp_file: Path, dest_file: Path) -> None:
    """
    Cierra el .tmp ya completo y lo renombra atómicamente al destino final.

    El hash no se calcula aquí: viene del hasher incremental que se alimentó
    mientras la descarga bajaba, así que el archivo nunca se relee ni se
    duplica en memoria.
    """
    fh.close()
    try:
        # replace() (no rename()): en Windows rename lanza FileExistsError si el
        # destino existe; replace sobrescribe atómicamente en ambos OS.
        tmp_file.replace(dest_file)
    except OSError:
        tmp_file.unlink(missing_ok=True)
        raise


# ── Clasificación de errores ───────────────────────────────────────────────────

class ErrorKind:
    """Categorías de error para decidir la estrategia de reintento."""
    NOT_FOUND    = "not_found"    # 404 — el archivo no existe; saltar
    AUTH         = "auth"         # 401/403 — sin acceso al archivo; saltar
    URL_EXPIRED  = "url_expired"  # firma del CDN vencida; re-resolver, no reintentar
    CLOUDFLARE   = "cloudflare"   # CF challenge/ban; esperar y reintentar
    RATE_LIMIT   = "rate_limit"   # 429 — demasiadas peticiones; esperar y reintentar
    SERVER       = "server_error" # 5xx — error del servidor; reintentar
    NETWORK      = "network"      # error de conexión; reintentar
    TIMEOUT      = "timeout"      # tiempo de espera; reintentar
    STALL        = "stall"        # descarga iniciada pero sin datos por N s; diferir
    UNKNOWN      = "unknown"      # otro error no clasificado

    # Tipos que deben ir a la cola diferida en lugar de descartarse
    DEFERRABLE = {"stall", "timeout", "network", "server_error", "cloudflare",
                  "rate_limit"}


# ── Constantes ─────────────────────────────────────────────────────────────────

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Headers base del cliente — User-Agent neutro para todas las requests.
# NOTA: Accept: text/css es el bypass de DDoS-Guard de Kemono, pero solo
# aplica a requests del API. Enviarlo en descargas de archivos causa 500
# en el CDN de kemono.cr. Se aplica por-request en _get_json_with_retry().
_CLIENT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}

# Header DDG exclusivo para llamadas al API de kemono.cr
_DDG_ACCEPT = {"Accept": "text/css"}

# Nombre de las cookies DDG a persistir
_DDG_COOKIES = {"__ddg1_", "__ddg8_", "__ddg9_", "__ddg10_"}


# ── Resultado de descarga ──────────────────────────────────────────────────────

class DownloadResult:
    __slots__ = ("url", "filename", "dest", "file_hash", "file_size", "skipped",
                 "error", "error_kind")

    def __init__(
        self,
        url: str,
        filename: str,
        dest: Path | None = None,
        file_hash: str | None = None,
        file_size: int = 0,
        skipped: bool = False,
        error: str | None = None,
        error_kind: str | None = None,
    ) -> None:
        self.url        = url
        self.filename   = filename
        self.dest       = dest
        self.file_hash  = file_hash
        self.file_size  = file_size
        self.skipped    = skipped
        self.error      = error
        self.error_kind = error_kind  # uno de ErrorKind.*

    @property
    def ok(self) -> bool:
        return self.error is None


# ── Ritmo de peticiones ────────────────────────────────────────────────────────

class _Pacer:
    """
    Espaciado mínimo entre peticiones, COMPARTIDO por todos los workers.

    No es lo mismo que `_delay()`, y la diferencia es justo la que importa
    cuando el operador de un sitio pide un ritmo concreto: `_delay()` duerme
    dentro de cada worker, así que N workers a 1 s producen N peticiones por
    segundo, no una. Este pacer reparte turnos sobre un reloj único, de modo
    que la tasa agregada es 1/interval sin importar cuántos workers haya.

    Cada llamada reserva su turno bajo el lock y duerme FUERA de él: los
    workers esperan en paralelo y despiertan escalonados, en vez de hacer cola
    serializada detrás del que duerme.
    """

    __slots__ = ("interval", "_lock", "_next")

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now  = loop.time()
            slot = max(now, self._next)
            self._next = slot + self.interval
        remaining = slot - loop.time()
        if remaining > 0:
            await asyncio.sleep(remaining)


class _CompletionGate:
    """Pausa global entre terminar un archivo y empezar el siguiente.

    Es compartida por todos los workers. El bucle de ``wait`` vuelve a mirar
    el reloj al despertar porque otro worker puede completar un archivo
    mientras este esperaba y, por tanto, desplazar de nuevo la apertura.
    """

    __slots__ = ("interval", "_lock", "_last_completed")

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last_completed: float | None = None

    def completed(self) -> None:
        self._last_completed = asyncio.get_running_loop().time()

    def ready(self) -> bool:
        if self._last_completed is None:
            return True
        return (asyncio.get_running_loop().time() - self._last_completed
                >= self.interval)

    async def wait(self) -> None:
        async with self._lock:
            while self._last_completed is not None:
                remaining = (
                    self._last_completed + self.interval
                    - asyncio.get_running_loop().time()
                )
                if remaining <= 0:
                    return
                await asyncio.sleep(remaining)


class _ByteBucket:
    """
    Techo de bytes por segundo, COMPARTIDO por todos los workers.

    El pacer limita cuántas VECES se pide; esto limita cuánto se trae. Son
    cosas distintas y hacen falta las dos: una petición por segundo con
    archivos de 130 MB da 10 MB/s, que sostenidos son casi un tera al día. Eso
    fue exactamente lo que bloqueó a Pawchive el 2026-09-05, cumpliendo su
    ritmo de peticiones al pie de la letra.

    Es un cubo de fichas clásico: se rellena a `rate` fichas por segundo hasta
    un tope de `burst`, y cada chunk recibido paga su tamaño. Un archivo grande
    lo vacía, así que la descarga siguiente arranca esperando a que se rellene:
    el espaciado proporcional al tamaño sale solo, sin un segundo sleep que
    pagar. Por eso no hay «delay según el peso del archivo anterior» aparte —
    sería el mismo peaje cobrado dos veces, el error que ya costó una sesión
    con `scan_page_delay`.

    `burst` decide cuánto puede correr una descarga que llega con el cubo
    lleno. Un cubo del tamaño de dos segundos de caudal deja arrancar rápido
    sin que el pico signifique nada en el promedio.
    """

    __slots__ = ("rate", "burst", "_lock", "_tokens", "_last")

    def __init__(self, rate: float, burst: float | None = None) -> None:
        self.rate = rate
        self.burst = burst if burst is not None else rate * 2
        self._lock = asyncio.Lock()
        self._tokens = self.burst
        self._last = 0.0

    async def take(self, n: int) -> float:
        """Paga `n` bytes; devuelve los segundos que hubo que esperar."""
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            if self._last:
                self._tokens = min(self.burst,
                                   self._tokens + (now - self._last) * self.rate)
            self._last = now
            self._tokens -= n
            debt = -self._tokens
        if debt <= 0:
            return 0.0
        # Se duerme FUERA del lock: los workers esperan en paralelo. El cubo ya
        # quedó en descubierto, así que el que venga detrás esperará su parte.
        wait = debt / self.rate
        await asyncio.sleep(wait)
        return wait


# ── Engine ─────────────────────────────────────────────────────────────────────

class DownloadEngine:
    """
    Gestiona un pool de workers async y un cliente HTTP compartido.

    Uso como context manager:
        async with DownloadEngine(config) as engine:
            result = await engine.download(url, dest)
    """

    def __init__(self, config: UserConfig, workers: int | None = None) -> None:
        self.config = config
        self._workers = workers or config.workers
        self._semaphore = asyncio.Semaphore(self._workers)
        self._client: httpx.AsyncClient | None = None
        # Solo cookies planas (strings) — los bloques de servicios como
        # {"pixiv": {...}} son dicts anidados y no son cookies HTTP.
        raw = load_session()
        self._session_cookies: dict[str, str] = {
            k: v for k, v in raw.items() if isinstance(v, str)
        }
        # Ritmo pedido por el sitio activo; None = delay global del config.
        self._pacer: _Pacer | None = None
        self._bucket: _ByteBucket | None = None
        self._completion_gate: _CompletionGate | None = None
        self._browser_transport = None
        self._browser_key: tuple[str, str] | None = None

    async def __aenter__(self) -> "DownloadEngine":
        # http2=False a propósito: con HTTP/2 todas las descargas se multiplexan
        # sobre UNA conexión TCP y el read-timeout de httpx mide actividad del
        # SOCKET, no del stream. Los frames de control de Cloudflare
        # (PING/keepalive) mantenían el socket "vivo" mientras un stream quedaba
        # huérfano sin recibir DATA → cuelgue indefinido de los workers sin
        # disparar ningún timeout. Con HTTP/1.1 cada descarga usa su propia
        # conexión del pool.
        #
        # Timeouts: connect/write/pool = config.timeout (corto). read = None
        # A PROPÓSITO: el read-timeout de httpx (vía anyio) arma un timer por
        # CADA lectura del stream, y bajo qasync cada timer es un timer de
        # Windows que solo se libera al dispararse, no al cancelarse. A ~160
        # lecturas/s eso fuga miles de timers y agota los USER handles del
        # proceso ("QEventDispatcherWin32: Failed to create a timer") → crash de
        # Qt en runs largas. La detección de stall la hace el watchdog throttled
        # de _do_download (asyncio.timeout reprogramado como mucho cada pocos
        # segundos), que crea un puñado de timers en vez de uno por lectura.
        self._client = httpx.AsyncClient(
            headers=_CLIENT_HEADERS,
            cookies=self._session_cookies,
            http2=False,
            follow_redirects=True,
            timeout=httpx.Timeout(
                connect=self.config.timeout,
                read=None,
                write=self.config.timeout,
                pool=self.config.timeout,
            ),
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._browser_transport is not None:
            await self._browser_transport.close()
            self._browser_transport = None
            self._browser_key = None
        if self._client:
            # Persistir cookies DDG actualizadas antes de cerrar
            self._persist_ddg_cookies()
            await self._client.aclose()

    def _persist_ddg_cookies(self) -> None:
        """Guarda las cookies DDG del cliente en disco para la próxima sesión."""
        if self._client is None:
            return
        # Iterar el jar directamente para evitar CookieConflict de httpx
        # cuando existen múltiples cookies con el mismo nombre (distintos paths)
        updated = {}
        for cookie in self._client.cookies.jar:
            if cookie.name in _DDG_COOKIES:
                updated[cookie.name] = cookie.value
        if updated:
            self._session_cookies.update(updated)
            # Releer y fusionar sobre la sesión COMPLETA en disco. save_session
            # escribe el dict entero, así que volcar solo las cookies DDG planas
            # borraría los bloques anidados de otros servicios (patreon/pixiv) y
            # forzaría re-login en cada descarga. Solo tocamos las claves DDG.
            data = load_session()
            data.update(updated)
            save_session(data)

    # ── API pública ──────────────────────────────────────────────────────────

    def set_workers(self, workers: int) -> None:
        """
        Cambia el tamaño del pool de descargas simultáneas.

        Se llama al entrar en cada fuente, porque el pool correcto es el del
        SITIO, no el del perfil: un perfil puede tener a la vez una fuente que
        admite 5 simultáneas y otra que solo tolera 2. Reemplazar el semáforo
        es seguro aquí y solo aquí — el bucle de fuentes es secuencial, así que
        no hay ninguna descarga en vuelo esperando el semáforo viejo.
        """
        if workers < 1:
            workers = 1
        if workers == self._workers:
            return
        self._workers  = workers
        self._semaphore = asyncio.Semaphore(workers)

    def set_pace(self, interval: float | None) -> None:
        """
        Fija el ritmo del sitio que se está procesando: como mucho una petición
        cada `interval` segundos entre TODOS los workers. `None` o 0 vuelve al
        delay aleatorio global de la config.

        Se llama al entrar en cada fuente, no una vez por engine: un perfil
        puede tener fuentes de sitios con ritmos distintos, y el bucle de
        fuentes es secuencial, así que el pacer activo siempre es el del sitio
        que se está bajando.
        """
        if not interval or interval <= 0:
            self._pacer = None
        elif self._pacer is None or self._pacer.interval != interval:
            self._pacer = _Pacer(interval)
        # Mismo ritmo que la fuente anterior: se conserva el pacer con su turno
        # ya devengado. Recrearlo por fuente regalaba una petición inmediata
        # cada vez, y un perfil con 43 fuentes del mismo sitio son 43 turnos
        # que nadie esperó.

    def set_rate(self, bytes_per_sec: float | None) -> None:
        """
        Fija el caudal máximo del sitio que se está procesando, agregado entre
        todos los workers. `None` o 0 lo quita.

        Se llama junto a `set_pace`, y por el mismo motivo: es un límite del
        sitio, no del perfil, y el bucle de fuentes es secuencial.
        """
        if not bytes_per_sec or bytes_per_sec <= 0:
            self._bucket = None
        elif self._bucket is None or self._bucket.rate != bytes_per_sec:
            self._bucket = _ByteBucket(bytes_per_sec)
        # Igual que el pacer, y aquí importa más: el cubo arranca lleno, así
        # que rehacerlo en cada fuente regalaría un burst entero por fuente
        # —con 43 de Pawchive, 172 MB fuera de todo límite—.

    def set_completion_gap(self, seconds: float | None) -> None:
        """Fija la pausa tras completar un archivo, agregada entre workers."""
        if not seconds or seconds <= 0:
            self._completion_gate = None
        elif (self._completion_gate is None
              or self._completion_gate.interval != seconds):
            self._completion_gate = _CompletionGate(seconds)
        # Se conserva si el valor no cambia entre fuentes del mismo sitio: la
        # última descarga de una fuente también debe separar la primera de la
        # siguiente.

    @property
    def paced(self) -> bool:
        """True si hay un ritmo por sitio activo (en vez del delay global)."""
        return self._pacer is not None

    @property
    def rate_limited(self) -> bool:
        """True si hay un techo de caudal por sitio activo."""
        return self._bucket is not None

    async def get_json(self, url: str, retries: int | None = None) -> dict | list:
        """
        GET con retry. Retorna el JSON parseado.
        Respeta el delay configurado entre requests.
        """
        max_attempts = retries or self.config.network.retries_api
        return await self._get_json_with_retry(url, max_attempts)

    async def download(
        self,
        url: str,
        dest_dir: Path,
        filename: str,
        progress: Progress | None = None,
        task_id: TaskID | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        extra_headers: dict | None = None,
        total_timeout: float | None = None,
        on_status: Callable[[str], None] | None = None,
        max_retries: int | None = None,
        transport: str = "http",
        browser_origin: str | None = None,
    ) -> DownloadResult:
        """
        Descarga un archivo al directorio destino.
        Respeta el semáforo del pool (max N descargas simultáneas).
        Retorna DownloadResult con hash SHA-256 y tamaño.

        on_progress:  callback(bytes_done, total_bytes) por chunk.
        extra_headers: headers adicionales por archivo (ej. Referer de Pixiv).
                       Se fusionan con los headers base del cliente.
        total_timeout: timeout total en segundos para toda la descarga
                       (incluyendo reintentos). Si se excede, retorna
                       DownloadResult(error_kind=TIMEOUT) de forma limpia,
                       sin lanzar excepciones.
        on_status:    callback(msg: str) llamado en cada transición de estado
                      importante (delay, conexión, stall, retry, etc.).
        max_retries:  sobreescribe config.network.retries_file para esta llamada.
                      Útil en batch (max_retries=1) para falla rápida —
                      el loop de iteraciones del batch es el mecanismo de reintento.
        transport:    ``http`` por defecto; ``browser_image`` carga el archivo
                      como subrecurso desde ``browser_origin`` y lo extrae por CDP.
        """
        async with self._semaphore:
            if transport == "browser_image":
                result = await self._do_browser_download(
                    url, dest_dir, filename, browser_origin or "",
                    progress, task_id, on_progress, on_status,
                )
            else:
                result = await self._do_download(
                    url, dest_dir, filename, progress, task_id,
                    on_progress, extra_headers, total_timeout, on_status, max_retries,
                )
            # Una respuesta fallida también termina un intento de archivo. Si
            # el sitio acaba de decir 403/429, arrancar el siguiente al ritmo
            # normal contradice precisamente la pausa de cortesía.
            if self._completion_gate is not None:
                self._completion_gate.completed()
            return result

    async def _do_browser_download(
        self,
        url: str,
        dest_dir: Path,
        filename: str,
        browser_origin: str,
        progress: Progress | None,
        task_id: TaskID | None,
        on_progress: Callable[[int, int], None] | None,
        on_status: Callable[[str], None] | None,
    ) -> DownloadResult:
        """Descarga una vez mediante Chromium; no reintenta dentro del archivo."""
        if not browser_origin:
            return DownloadResult(
                url=url, filename=filename,
                error="El transporte de navegador no tiene página de origen",
                error_kind=ErrorKind.UNKNOWN,
            )
        try:
            from .browser_transport import BrowserImageTransport

            browser_key = (browser_origin, urlsplit(url).netloc)
            if (self._browser_transport is None
                    or self._browser_key != browser_key):
                if self._browser_transport is not None:
                    await self._browser_transport.close()
                self._browser_transport = BrowserImageTransport(browser_origin, url)
                self._browser_key = browser_key

            rate = self._bucket.rate if self._bucket is not None else 0.0
            if on_status:
                on_status("◉ preparando navegador…")
            await self._browser_transport.start(rate)
            await self._wait_browser_turn(on_status)

            def report(done: int, total: int) -> None:
                if progress and task_id is not None:
                    progress.update(task_id, completed=done, total=total or None)
                if on_progress:
                    on_progress(done, total)

            outcome = await self._browser_transport.download(
                url, dest_dir, filename,
                take_bytes=(self._bucket.take if self._bucket is not None else None),
                on_progress=report,
                on_status=on_status,
                stall_timeout=self.config.network.stall_timeout,
            )
        except Exception as exc:
            if self._browser_transport is not None:
                await self._browser_transport.close()
                self._browser_transport = None
                self._browser_key = None
            return DownloadResult(
                url=url, filename=filename,
                error=f"No se pudo usar el navegador: {type(exc).__name__}: {exc}",
                error_kind=ErrorKind.NETWORK,
            )

        if outcome.error is None:
            return DownloadResult(
                url=url, filename=filename, dest=outcome.dest,
                file_hash=outcome.file_hash, file_size=outcome.file_size,
            )

        code = outcome.status
        if code == 404:
            kind = ErrorKind.NOT_FOUND
        elif code in (401, 403):
            kind = ErrorKind.AUTH
        elif code == 429:
            kind = ErrorKind.RATE_LIMIT
        elif code is not None and code >= 500:
            kind = ErrorKind.SERVER
        elif "recibir datos" in outcome.error or "cabeceras" in outcome.error:
            kind = ErrorKind.TIMEOUT
        else:
            kind = ErrorKind.NETWORK
        return DownloadResult(
            url=url, filename=filename,
            error=outcome.error, error_kind=kind,
        )

    async def _wait_browser_turn(
        self, on_status: Callable[[str], None] | None,
    ) -> None:
        """Aplica la misma compuerta global y pacer antes de crear el ``<img>``."""
        while True:
            if self._completion_gate is not None:
                if on_status and not self._completion_gate.ready():
                    on_status(
                        f"⏳ pausa entre archivos "
                        f"({self._completion_gate.interval:.0f}s)…"
                    )
                await self._completion_gate.wait()
            if self._pacer is not None:
                if on_status:
                    on_status(f"⏳ turno ({self._pacer.interval:.0f}s/petición)…")
                await self._pacer.wait()
            else:
                await asyncio.sleep(random.uniform(
                    self.config.network.delay_min,
                    self.config.network.delay_max,
                ))
            if self._completion_gate is None or self._completion_gate.ready():
                return

    async def run_queue(
        self,
        tasks: AsyncIterator[tuple[str, Path, str]],
        progress: Progress,
    ) -> list[DownloadResult]:
        """
        Procesa una cola de tareas (url, dest_dir, filename) con el pool.
        Cada worker libre toma la siguiente tarea inmediatamente.
        Retorna lista de DownloadResult al terminar.
        """
        results: list[DownloadResult] = []
        pending: set[asyncio.Task] = set()

        async for url, dest_dir, filename in tasks:
            task_id = progress.add_task(f"[cyan]{filename[:40]}", total=None)
            coro = self.download(url, dest_dir, filename, progress, task_id)
            t = asyncio.create_task(coro)
            pending.add(t)
            t.add_done_callback(pending.discard)

            # Limitar tareas en vuelo al doble del pool para no saturar RAM
            while len(pending) >= self._workers * 2:
                done, pending_set = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for finished in done:
                    results.append(finished.result())
                pending = set(pending_set)

        # Esperar las restantes
        if pending:
            done, _ = await asyncio.wait(pending)
            for finished in done:
                results.append(finished.result())

        return results

    # ── Internals ────────────────────────────────────────────────────────────

    async def _get_json_with_retry(self, url: str, max_attempts: int) -> dict | list:
        """
        GET con backoff exponencial para llamadas a API.

        Estrategias por código:
          404      → error permanente; lanza inmediatamente (el recurso no existe)
          403 CF   → Cloudflare; espera 60 s + 30 s/intento, luego retry
          429      → rate limit; espera 35 s + 10 s/intento, luego retry
          5xx      → error servidor; backoff exponencial, luego retry
          red/tout → backoff exponencial, luego retry
        """
        last_error: Exception | None = None

        for attempt in range(max_attempts):
            try:
                await self._delay()
                resp = await self._client.get(url, headers=_DDG_ACCEPT)
                code = resp.status_code

                # 404 — recurso permanentemente ausente
                if code == 404:
                    raise httpx.HTTPStatusError(
                        f"404 Not Found: {url}", request=resp.request, response=resp
                    )

                # 403 Cloudflare — temporal, hay que esperar
                if code == 403 and _is_cloudflare_block(resp):
                    wait = 60 + attempt * 30
                    last_error = RuntimeError(f"Cloudflare 403 en {url}")
                    await asyncio.sleep(wait)
                    continue

                # 403 sin CF — acceso denegado permanente
                if code == 403:
                    raise httpx.HTTPStatusError(
                        f"403 Forbidden: {url}", request=resp.request, response=resp
                    )

                # 429 — rate limit
                if code == 429:
                    wait = 35 + attempt * 10
                    last_error = RuntimeError(f"Rate limit 429 en {url}")
                    await asyncio.sleep(wait)
                    continue

                # 5xx — error de servidor, reintentar
                if code >= 500:
                    backoff = 2 ** attempt + random.uniform(0, 1)
                    last_error = RuntimeError(f"HTTP {code} en {url}")
                    await asyncio.sleep(backoff)
                    continue

                resp.raise_for_status()
                return resp.json()

            except httpx.HTTPStatusError:
                raise   # 404 / 403 permanente — propagar sin retry

            except (httpx.RequestError, httpx.TimeoutException) as e:
                last_error = e
                backoff = 2 ** attempt + random.uniform(0, 1)
                await asyncio.sleep(backoff)

        raise RuntimeError(
            f"Falló tras {max_attempts} intentos ({last_error}): {url}"
        )

    async def _do_download(
        self,
        url: str,
        dest_dir: Path,
        filename: str,
        progress: Progress | None,
        task_id: TaskID | None,
        on_progress: Callable[[int, int], None] | None = None,
        extra_headers: dict | None = None,
        total_timeout: float | None = None,
        on_status: Callable[[str], None] | None = None,
        max_retries: int | None = None,
    ) -> DownloadResult:
        """
        Descarga un archivo con retry y soporte de Range requests para CDNs
        que sirven archivos en chunks (ej. kemono.cr: chunks de 4 MB).

        Dos tipos de reintentos con contadores separados:
          Range resume  : CDN truncó la conexión → continuar desde `written`.
                          No cuenta como error; no re-descarga bytes ya recibidos.
          Error real    : timeout, connection error, 5xx → preserva progreso previo
                          y reintenta con backoff exponencial. Cuenta hacia max_errors.

        Estrategias por código HTTP:
          404 / 401  → retorno inmediato, sin retry
          403 origen → retorno inmediato, sin retry (firma vencida o sin acceso)
          403 CF     → esperar 60 s + 30 s por error, luego retry desde 0
          429        → esperar 35 s + 10 s por error, luego retry desde 0
          5xx        → backoff exponencial, luego retry desde 0
          red/timeout → backoff exponencial, preservar progreso si written > 0

        total_timeout: si se especifica, el engine retorna DownloadResult(TIMEOUT)
          antes de agotar ese tiempo. Esto evita depender de asyncio.wait_for,
          que en Python 3.12 puede propagar CancelledError en lugar de TimeoutError
          cuando httpx re-crea la excepción durante cleanup de streams HTTP/2.
        """
        max_errors = max_retries if max_retries is not None else self.config.network.retries_file
        last_error: str = ""
        last_kind:  str = ErrorKind.UNKNOWN

        # Estado de descarga — persiste entre range resumes.
        # El archivo se vuelca al .tmp según llega y el hash se calcula
        # incremental: nada de acumularlo entero en RAM. Antes se guardaba en
        # una lista de chunks que además se unía con b"".join() al final, así
        # que el pico de memoria era ~3x el tamaño del archivo (medido: 197 MiB
        # para uno de 64 MiB) y un pack .zip de varios GB tumbaba el proceso.
        written:     int = 0            # bytes YA volcados al .tmp
        buf:         list[bytes] = []   # recibidos y aún sin volcar
        buf_len:     int = 0
        hasher                   = hashlib.sha256()
        full_size:   int = 0            # tamaño total declarado por el servidor
        error_count: int = 0            # solo errores reales (no range resumes)

        # Tiempo de inicio para timeout total del engine.
        # get_running_loop().time() usa el reloj monotónico del loop.
        _t_start = asyncio.get_running_loop().time() if total_timeout else 0.0

        # Firma del CDN ya vencida antes de empezar: el 403 es seguro. Se falla
        # de inmediato para no gastar ni el request ni el delay previo; el
        # archivo se queda en `pending` y la próxima sync pide una firma nueva.
        _aged = _signed_url_expiry(url)
        if _aged is not None:
            return DownloadResult(
                url=url, filename=filename,
                error=(f"URL firmada vencida hace {_aged / 3600:.1f} h "
                       f"- se re-resuelve en la proxima sync"),
                error_kind=ErrorKind.URL_EXPIRED,
            )

        dest_file = dest_dir / filename
        tmp_file  = dest_dir / (filename + ".tmp")
        dest_dir.mkdir(parents=True, exist_ok=True)

        async def _flush() -> None:
            """Vuelca al .tmp lo recibido desde el último volcado."""
            nonlocal buf, buf_len, written
            if not buf:
                return
            await asyncio.to_thread(_write_chunks, fh, hasher, buf)
            written += buf_len
            buf = []
            buf_len = 0

        def _restart() -> None:
            """Descarta lo bajado y deja el .tmp listo para empezar de cero."""
            nonlocal buf, buf_len, written, hasher, full_size
            buf = []
            buf_len = 0
            written = 0
            hasher = hashlib.sha256()
            full_size = 0
            fh.seek(0)
            fh.truncate(0)

        fh = open(tmp_file, "wb")
        try:
            while True:
                if error_count >= max_errors:
                    return DownloadResult(
                        url=url, filename=filename,
                        error=f"{last_error} (tras {max_errors} intentos)",
                        error_kind=last_kind,
                    )

                # Timeout total del engine: retornar limpiamente antes de que el
                # asyncio.wait_for del TUI dispare CancelledError. En Python 3.12
                # httpx puede re-crear CancelledError al hacer cleanup de streams
                # HTTP/2, lo que impide que wait_for lo convierta correctamente a
                # TimeoutError y causa que el worker desaparezca sin log.
                #
                # El presupuesto no es fijo: son `total_timeout` segundos de
                # gracia MÁS lo que costaría bajar a `_MIN_RATE` lo ya
                # confirmado. Con un tope fijo un archivo grande es imposible
                # de bajar — un .zip de 3 GB no cabe en los 300 s que pasa el
                # worker, así que al primer tropiezo se aborta, se difiere, y
                # en la cola diferida se le vuelven a dar 300 s: nunca termina.
                # Lo que sigue cortando en seco es lo que NO progresa.
                if total_timeout:
                    # Con un techo de caudal, el suelo de velocidad esperada no
                    # puede estar por encima de lo que nosotros mismos dejamos
                    # correr: si no, el presupuesto mataría descargas que van
                    # despacio porque se lo pedimos.
                    floor = _MIN_RATE
                    if self._bucket is not None:
                        floor = min(floor, self._bucket.rate)
                    budget = total_timeout + written / floor
                    if asyncio.get_running_loop().time() - _t_start >= budget:
                        return DownloadResult(
                            url=url, filename=filename,
                            error=f"Timeout total ({budget:.0f}s)",
                            error_kind=ErrorKind.TIMEOUT,
                        )

                try:
                    # Delay completo solo en el PRIMER intento del archivo: es
                    # el espaciado entre peticiones que evita el 429, y en un
                    # reintento ese papel ya lo cumple el backoff que se acaba
                    # de pagar. Sumar los dos añadía 10-30 s por error, hasta
                    # ~2 min extra en un archivo que agota los reintentos.
                    # Entre range resumes, pausa mínima para no cerrar el flujo.
                    first_request = written == 0 and error_count == 0
                    while True:
                        if first_request and self._completion_gate is not None:
                            if on_status and not self._completion_gate.ready():
                                on_status(
                                    f"⏳ pausa entre archivos "
                                    f"({self._completion_gate.interval:.0f}s)…"
                                )
                            await self._completion_gate.wait()

                        if self._pacer is not None:
                            # Ritmo de sitio: TODA petición pasa por el turno,
                            # sea primer intento, reintento o continuación de
                            # rango. Un reintento cuesta una petición igual.
                            if on_status:
                                on_status(
                                    f"⏳ turno "
                                    f"({self._pacer.interval:.0f}s/petición)…"
                                )
                            await self._pacer.wait()
                        elif first_request:
                            if on_status:
                                d_min = self.config.network.delay_min
                                d_max = self.config.network.delay_max
                                on_status(
                                    f"⏳ esperando ({d_min:.0f}–{d_max:.0f}s)…"
                                )
                            await self._delay()
                        else:
                            if on_status:
                                on_status(
                                    "↺ continuando rango…"
                                    if written else "↺ reintentando…"
                                )
                            await asyncio.sleep(random.uniform(0.5, 2.0))

                        # Otro worker pudo terminar mientras este esperaba su
                        # turno del pacer. En ese caso el turno se descarta y se
                        # vuelve a esperar: la pausa post-descarga manda.
                        if (not first_request
                                or self._completion_gate is None
                                or self._completion_gate.ready()):
                            break

                    # Añadir Range header si estamos resumiendo un archivo parcial.
                    req_headers = dict(extra_headers or {})
                    if written > 0:
                        req_headers["Range"] = f"bytes={written}-"

                    received_now = 0   # bytes recibidos en ESTE request

                    if on_status:
                        on_status("→ conectando…")
                    async with self._client.stream(
                        "GET", url, headers=req_headers
                    ) as resp:
                        code = resp.status_code

                        # ── Errores permanentes: no reintentar ─────────────────
                        if code == 404:
                            return DownloadResult(
                                url=url, filename=filename,
                                error="Archivo no encontrado en el servidor (404)",
                                error_kind=ErrorKind.NOT_FOUND,
                            )
                        if code == 401:
                            return DownloadResult(
                                url=url, filename=filename,
                                error="No autorizado — el archivo requiere sesión (401)",
                                error_kind=ErrorKind.AUTH,
                            )

                        # ── Cloudflare: 403 con cabeceras CF ──────────────────
                        if code == 403 and _is_cloudflare_block(resp):
                            wait = 60 + error_count * 30
                            last_error = f"Cloudflare bloqueó la descarga (403 CF) — esperando {wait}s"
                            last_kind  = ErrorKind.CLOUDFLARE
                            error_count += 1
                            if on_status:
                                on_status(f"🛡 Cloudflare — esperando {wait}s…")
                            _restart()
                            await asyncio.sleep(wait)
                            continue

                        # ── 403 del origen: denegación, no bloqueo de CF ───────
                        # No se reintenta: ni una firma vencida ni la falta de
                        # permiso cambian esperando. El archivo se queda pendiente
                        # y la próxima sync lo resuelve con una URL nueva.
                        if code == 403:
                            aged = _signed_url_expiry(url)
                            if aged is not None:
                                return DownloadResult(
                                    url=url, filename=filename,
                                    error=(f"URL firmada vencida hace {aged / 3600:.1f} h "
                                           f"- se re-resuelve en la proxima sync"),
                                    error_kind=ErrorKind.URL_EXPIRED,
                                )
                            # El cuerpo puede ser un mensaje escrito a mano por
                            # el operador del sitio. Cuesta unos cientos de
                            # bytes de una respuesta que ya llegó.
                            detail = await read_short_body(resp)
                            return DownloadResult(
                                url=url, filename=filename,
                                error=("Acceso denegado (403) - la sesión no da acceso "
                                       "a este archivo" + detail),
                                error_kind=ErrorKind.AUTH,
                            )

                        # ── Rate limit ─────────────────────────────────────────
                        if code == 429:
                            wait = 35 + error_count * 10
                            last_error = f"Demasiadas peticiones (429) — esperando {wait}s"
                            last_kind  = ErrorKind.RATE_LIMIT
                            error_count += 1
                            if on_status:
                                on_status(f"⏸ rate limit 429 — esperando {wait}s…")
                            _restart()
                            await asyncio.sleep(wait)
                            continue

                        # ── Error de servidor ──────────────────────────────────
                        if code >= 500:
                            backoff = 2 ** error_count + random.uniform(0, 1)
                            last_error = f"Error del servidor ({code})"
                            last_kind  = ErrorKind.SERVER
                            error_count += 1
                            if on_status:
                                on_status(
                                    f"↺ error {code} — reintentando "
                                    f"({error_count}/{max_errors})…"
                                )
                            _restart()
                            await asyncio.sleep(backoff)
                            continue

                        # ── Otros códigos HTTP no exitosos ─────────────────────
                        if code >= 400:
                            return DownloadResult(
                                url=url, filename=filename,
                                error=f"HTTP {code} — no se puede descargar",
                                error_kind=ErrorKind.UNKNOWN,
                            )

                        # ── 206 Partial Content — range request aceptado ───────
                        if code == 206:
                            cr_total = _parse_content_range_total(
                                resp.headers.get("content-range", "")
                            )
                            if cr_total:
                                full_size = cr_total
                            if progress and task_id is not None and full_size:
                                progress.update(task_id, total=full_size)

                        # ── 200 OK — server ignoró Range o primer request ───────
                        elif code == 200:
                            if written > 0:
                                # Server no soporta Range: descartar parcial y aceptar todo
                                _restart()
                            full_size = int(resp.headers.get("content-length", 0))
                            if progress and task_id is not None:
                                progress.update(task_id, total=full_size or None)

                        # ── Leer chunks del stream ─────────────────────────────
                        # Watchdog de progreso: asyncio.timeout se reprograma con
                        # CADA chunk recibido. Si pasan más de stall_timeout segundos
                        # sin un chunk nuevo, lanza TimeoutError → tratado abajo como
                        # stall (reintento con Range, preservando lo recibido).
                        # Mide progreso real de ESTA descarga, no actividad del
                        # socket — cubre el caso del read-timeout de httpx que no
                        # disparaba con streams huérfanos (ver __aenter__).
                        _first_chunk = True
                        _stall = self.config.network.stall_timeout
                        _loop = asyncio.get_running_loop()
                        # Reprogramar el watchdog COMO MUCHO cada _resched_every s, NO
                        # por chunk. Bajo qasync cada reschedule (call_at → startTimer)
                        # crea un timer de Windows que solo se libera cuando SE DISPARA,
                        # no cuando se cancela. Reprogramar por chunk (~160/s a 10 MB/s)
                        # acumula miles de timers vivos (cada uno dura _stall segundos)
                        # y agota los USER handles del proceso → "QEventDispatcherWin32:
                        # Failed to create a timer … máximo de manipuladores" y crash de
                        # Qt en runs largas. Throttlearlo mantiene la detección de stall
                        # (latencia ≤ _stall + _resched_every) con un puñado de timers.
                        _resched_every = max(1.0, _stall / 4)
                        _next_resched = _loop.time() + _resched_every
                        async with asyncio.timeout(_stall) as _wd:
                            async for chunk in resp.aiter_bytes(chunk_size=65536):
                                # Techo de caudal del sitio, si lo hay. Va antes
                                # de reprogramar el watchdog a propósito: lo que
                                # se duerme aquí es decisión nuestra, no falta de
                                # datos, y contarlo como stall abortaría la
                                # descarga por ir despacio adrede.
                                if self._bucket is not None:
                                    await self._bucket.take(len(chunk))
                                _now = _loop.time()
                                if _now >= _next_resched:
                                    _wd.reschedule(_now + _stall)
                                    _next_resched = _now + _resched_every
                                if _first_chunk:
                                    if on_status:
                                        on_status("↓ descargando…")
                                    _first_chunk = False
                                buf.append(chunk)
                                buf_len += len(chunk)
                                received_now += len(chunk)
                                if buf_len >= _FLUSH_EVERY:
                                    await _flush()
                                if progress and task_id is not None:
                                    progress.update(task_id, advance=len(chunk))
                                if on_progress:
                                    on_progress(written + buf_len, full_size)

                    # ── Post-stream: evaluar completitud ───────────────────────

                    # Volcar lo que quedó en el buffer ANTES de decidir nada: a
                    # partir de aquí `written` tiene que ser la verdad completa
                    # de lo recibido, porque es lo que fija el offset del Range.
                    await _flush()

                    # Guard: CDN devolvió 0 bytes en un range request — error real.
                    # Si ya teníamos datos de requests anteriores, conservarlos y
                    # reintentar el Range desde la misma posición.
                    if received_now == 0 and full_size > 0 and written < full_size:
                        backoff = 2 ** error_count + random.uniform(0, 1)
                        last_error = "CDN devolvió 0 bytes en range request"
                        last_kind  = ErrorKind.NETWORK
                        error_count += 1
                        if written == 0:
                            _restart()
                        # Si written > 0: conservar lo bajado y reintentar Range
                        await asyncio.sleep(backoff)
                        continue

                    total = written

                    # Truncación: CDN cerró el chunk antes de terminar el archivo.
                    # Continuar con Range request — NO es un error real, no se
                    # incrementa error_count ni se descartan los bytes recibidos.
                    if full_size > 0 and total < full_size:
                        last_error = (
                            f"Chunk recibido: {total}/{full_size} bytes "
                            f"({total * 100 // full_size}%) — continuando con Range…"
                        )
                        last_kind = ErrorKind.NETWORK
                        continue

                    # ── Descarga completa ───────────────────────────────────────
                    # El contenido ya está en el .tmp y el hash salió del hasher
                    # incremental; aquí solo queda cerrar y renombrar, en thread
                    # para no bloquear el event loop.
                    file_hash = hasher.hexdigest()
                    await asyncio.to_thread(
                        _finalize_download, fh, tmp_file, dest_file
                    )

                    if progress and task_id is not None:
                        progress.update(task_id, description=f"[green]{filename[:40]}")

                    return DownloadResult(
                        url=url, filename=filename,
                        dest=dest_file, file_hash=file_hash, file_size=total,
                    )

                except (httpx.TimeoutException, TimeoutError):
                    # httpx.TimeoutException = read/connect timeout del socket.
                    # TimeoutError = watchdog de progreso (asyncio.timeout) que se
                    # disparó por falta de chunks nuevos. Mismo tratamiento: stall.
                    backoff = 2 ** error_count + random.uniform(0, 1)
                    last_error = "Timeout en descarga"
                    last_kind  = ErrorKind.TIMEOUT
                    error_count += 1
                    # Preservar progreso acumulado: volcar al .tmp lo recibido
                    # antes del corte, para que `written` marque la posición
                    # exacta desde la que reanudar el Range.
                    # Solo reiniciar desde 0 si no tenemos ningún dato.
                    await _flush()
                    if written == 0:
                        _restart()
                    if on_status:
                        on_status(
                            f"⏱ stall/timeout — reintentando "
                            f"({error_count}/{max_errors}) en {backoff:.1f}s…"
                        )
                    await asyncio.sleep(backoff)

                except httpx.RequestError as e:
                    # Error de conexión: misma lógica — preservar progreso previo.
                    backoff = 2 ** error_count + random.uniform(0, 1)
                    last_error = f"Error de conexión: {type(e).__name__}"
                    last_kind  = ErrorKind.NETWORK
                    error_count += 1
                    await _flush()
                    if written == 0:
                        _restart()
                    if on_status:
                        on_status(
                            f"⚠ red ({type(e).__name__}) — reintentando "
                            f"({error_count}/{max_errors}) en {backoff:.1f}s…"
                        )
                    await asyncio.sleep(backoff)

        finally:
            # Cierre garantizado incluso si el worker se cancela: en Windows un
            # handle abierto impide borrar el .tmp, y quedaría ocupando disco
            # hasta el siguiente scan. Si la descarga terminó bien el .tmp ya se
            # renombró al destino, así que aquí no queda nada que borrar.
            fh.close()
            tmp_file.unlink(missing_ok=True)

    async def _delay(self) -> None:
        """
        Espaciado antes de una petición.

        Con ritmo de sitio activo manda el pacer compartido (tasa agregada
        exacta); si no, el delay aleatorio de la config, que es por worker.
        """
        if self._pacer is not None:
            await self._pacer.wait()
            return
        d = random.uniform(
            self.config.network.delay_min,
            self.config.network.delay_max,
        )
        await asyncio.sleep(d)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_content_range_total(header: str) -> int | None:
    """
    Extrae el tamaño total del archivo de un header Content-Range.

    Formato: "bytes START-END/TOTAL"  →  TOTAL
    Retorna None si el header está vacío, mal formado, o TOTAL es '*'
    (tamaño desconocido, usado por algunos servidores en streaming).
    """
    try:
        total_str = header.rsplit("/", 1)[-1].strip()
        if not total_str or total_str == "*":
            return None
        return int(total_str)
    except (ValueError, IndexError):
        return None


# Patreon firma sus URLs de media con `token-time=<epoch UTC>`; pasado ese
# instante el CDN responde 403 y ninguna espera lo revierte.
_TOKEN_TIME_RE = re.compile(r"[?&]token-time=(\d+)")


def _signed_url_expiry(url: str) -> float | None:
    """
    Segundos transcurridos desde que venció la firma de una URL de CDN, o None
    si la URL no va firmada o su firma sigue vigente.

    Sirve para no gastar un request —ni el ciclo de reintentos— en una URL que
    el servidor va a rechazar con certeza: la firma no se renueva reintentando,
    hay que volver a resolver el post para obtener otra.
    """
    m = _TOKEN_TIME_RE.search(url)
    if not m:
        return None
    try:
        expiry = int(m.group(1))
    except ValueError:
        return None
    elapsed = time.time() - expiry
    return elapsed if elapsed > 0 else None


async def read_short_body(resp: httpx.Response, limit: int = 300) -> str:
    """
    Los primeros bytes del cuerpo de un rechazo, si son texto legible.

    Un 403 puede traer un mensaje escrito por una persona: el de Pawchive eran
    143 bytes de `text/plain` y el motor los descartaba sin leerlos, tirando
    justo la información que explicaba el bloqueo. Se devuelve ya formateado
    para concatenar a un mensaje de error, o cadena vacía.

    No levanta nunca: un cuerpo ilegible no es motivo para perder el error real.
    """
    ctype = resp.headers.get("content-type", "")
    if "text/" not in ctype and "json" not in ctype:
        return ""
    try:
        raw = await resp.aread()
    except Exception:
        return ""
    txt = " ".join(raw[:limit].decode("utf-8", "replace").split())
    return f" — el servidor dice: «{txt}»" if txt else ""


def _is_cloudflare_block(resp: httpx.Response) -> bool:
    """
    Detecta que Cloudflare está MITIGANDO la petición (challenge o ban), no que
    la respuesta simplemente venga servida a través de su CDN.

    La distinción decide la estrategia de reintento, y confundirla sale caro:
    patreonusercontent.com sirve TODO detrás de Cloudflare, así que `cf-ray` y
    `server: cloudflare` los cumple igual un 403 del origen —firma vencida, sin
    permiso—, que esperando no se arregla. Tratarlo como bloqueo costaba el
    ciclo completo (60+90+...+240 s ~ 17 min por archivo) para fallar igual.

    Un challenge real se anuncia con `cf-mitigated` o, en su defecto, sirve una
    página HTML; una denegación del origen es un cuerpo corto de texto plano.
    """
    headers = resp.headers
    if "cf-mitigated" in headers:
        return True
    behind_cf = (
        "cf-ray" in headers
        or headers.get("server", "").lower() == "cloudflare"
    )
    if not behind_cf:
        return False
    return "text/html" in headers.get("content-type", "").lower()


# ── Progress bar factory ───────────────────────────────────────────────────────

def make_progress() -> Progress:
    """Crea una barra de progreso Rich para el engine."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeElapsedColumn(),
        expand=True,
    )
