"""
Puente entre `download_service.run_profile_download` y el hilo GUI (A1.2-A1.4).

El motor de cherry-dl publica su progreso llamando a un callback `emit(evento)`
con 13 dataclasses distintas. Su docstring dice que `emit` "es síncrono y seguro
para tocar widgets directamente" — cierto bajo qasync, donde todo comparte el
hilo GUI, y **falso aquí**: en Panopticon la corrutina corre en el loop de
`CherryRuntime`, es decir, en otro hilo. Tocar un widget desde ahí es corrupción
de memoria, no una excepción.

Este puente es la traducción: `emit` no hace más que `signal.emit(...)`. Como el
puente vive en el hilo GUI y el emisor está en otro, Qt resuelve la conexión como
**queued** por sí solo (AutoConnection compara la afinidad de hilo del receptor);
los argumentos se copian y se entregan en el bucle de eventos de la GUI. Por eso
el `DownloadBridge` NUNCA debe pasarse por `moveToThread`.

`resolve_auth` va al revés — del worker a la GUI y de vuelta: la corrutina se
queda esperando un `asyncio.Future` mientras el hilo GUI abre un diálogo modal;
cuando el usuario responde, `resolve_auth_result()` resuelve el Future con
`call_soon_threadsafe` (única forma thread-safe de tocarlo).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from PySide6.QtCore import QObject, Signal

from .runtime import CherryRuntime

log = logging.getLogger(__name__)

# Mínimo entre dos WorkerProgress del mismo slot. El motor llama a on_progress
# por cada chunk leído del socket: sin freno son miles de señales por segundo
# encoladas en el bucle de la GUI, que se atasca aunque el hilo no bloquee.
_PROGRESS_MIN_INTERVAL = 0.05  # 20 Hz por slot


class DownloadBridge(QObject):
    """Lanza una descarga en el runtime y reparte sus eventos como señales Qt.

    Un puente = una descarga a la vez (guard anti doble-inicio). Para descargas
    concurrentes de varios perfiles, un puente por perfil sobre el mismo runtime.
    """

    # ── los 13 DownloadEvent del motor ──────────────────────────────────────
    log_line         = Signal(str, str)        # msg, level: info|dim|ok|warn|error
    workers_resolved = Signal(int)             # workers efectivos tras el cap
    source_started   = Signal(str, str, str)   # url, artista, sitio
    cooldown         = Signal(float)           # segundos de enfriamiento
    batch_info       = Signal(int, int)        # total, offset (resume)
    scan_progress    = Signal(int, int)        # vistos, encolados
    scan_complete    = Signal(int)             # pendientes (solo scan_only)
    worker_start     = Signal(int, str)        # slot, filename
    worker_progress  = Signal(int, int, int)   # slot, done, total
    worker_done      = Signal(int, str, str)   # slot, filename, icono
    worker_idle      = Signal(int)             # slot
    counters         = Signal(int, int, int, int)  # bajados, saltados, err, difer.
    source_done      = Signal(int, int)        # url_id, file_count
    site_blocked     = Signal(str, int, str)   # sitio, racha, lo que dijo el server
    unknown_event    = Signal(object)          # evento que el upstream añadió

    # ── autenticación interactiva ───────────────────────────────────────────
    auth_required = Signal(str, int)  # sitio (patreon|pixiv), token
    auth_aborted  = Signal(int)       # token: la descarga murió esperando

    # ── ciclo de vida de la descarga ────────────────────────────────────────
    started_signal   = Signal()
    finished_signal  = Signal(object)  # DownloadSummary
    cancelled_signal = Signal()
    failed_signal    = Signal(str)     # texto del error

    def __init__(self, runtime: CherryRuntime, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._task: Optional[asyncio.Task] = None
        self._future = None  # concurrent.futures.Future de la descarga en curso
        self._busy = False
        self._auth_futures: dict[int, asyncio.Future] = {}
        self._auth_token = 0
        self._last_progress: dict[int, float] = {}

    # ── API del hilo GUI ────────────────────────────────────────────────────

    @property
    def is_busy(self) -> bool:
        """Ocupado desde que se ENCOLA la descarga, no desde que arranca.

        La distinción importa: entre `submit()` y el primer statement de la
        corrutina pasan milisegundos en los que el loop aún no creó la task.
        Un guard basado en `self._task` deja esa ventana abierta y un doble
        clic lanza dos descargas del mismo perfil.
        """
        return self._busy

    def start(
        self,
        profile: dict,
        *,
        workers: int = 4,
        ext_filter: set[str] | None = None,
        exclude_mode: bool = False,
        force_full: bool = False,
        scan_only: bool = False,
        config: Any = None,
    ) -> bool:
        """Arranca la descarga de un perfil. Devuelve False si ya hay una
        corriendo o si el runtime está apagado (guard anti doble-inicio)."""
        if self.is_busy:
            log.warning("cherry: descarga ya en curso; se ignora el segundo start")
            return False
        if not self._runtime.is_running:
            log.error("cherry: el runtime no está corriendo")
            return False

        self._busy = True
        self._last_progress.clear()
        coro = self._runner(
            profile,
            workers=workers,
            ext_filter=ext_filter or set(),
            exclude_mode=exclude_mode,
            force_full=force_full,
            scan_only=scan_only,
            config=config,
        )
        try:
            self._future = self._runtime.submit(coro)
        except RuntimeError as exc:
            self._busy = False
            log.error("cherry: no se pudo encolar la descarga: %s", exc)
            return False
        # Red de seguridad para el caso en que la task muera ANTES de ejecutar
        # una sola línea del runner (cancelada mientras estaba encolada): ahí
        # el `finally` del runner nunca corre y el puente quedaría ocupado.
        self._future.add_done_callback(self._on_future_done)
        self.started_signal.emit()
        return True

    def cancel(self) -> bool:
        """Cancela la descarga en curso (A1.4).

        `run_profile_download` propaga `CancelledError` limpiamente: cancela el
        productor y los workers y espera a que mueran, así que la `pending_queue`
        queda consistente — lo ya descargado está catalogado y lo que no, sigue
        en cola para la próxima sync.
        """
        if not self._busy:
            return False
        if self._task is not None:
            return self._runtime.cancel_task(self._task)
        # Encolada pero sin arrancar: cancelar el future propaga la cancelación
        # a la task en cuanto el loop la cree.
        fut = self._future
        return bool(fut is not None and fut.cancel())

    def resolve_auth_result(self, token: int, ok: bool) -> bool:
        """Respuesta del diálogo modal. Se llama DESDE el hilo GUI (A1.3).

        `ok` significa "la sesión ya está resuelta": la descarga reintentará la
        fuente. Para el camino normal —el usuario acepta y hay que loguearse—
        va `run_site_login()`, que resuelve el token él mismo al terminar.
        """
        if token not in self._auth_futures:
            log.warning("cherry: token de auth desconocido (%s)", token)
            return False
        return self._runtime.call_soon(self._set_auth_result, token, ok)

    def run_site_login(self, site: str, token: int) -> bool:
        """Lanza el login guiado del sitio; su resultado resuelve el token (A3).

        El login abre el navegador REAL del usuario y le habla por CDP: es una
        corrutina larga y sin nada de Qt, así que corre en el loop, no en el
        hilo GUI. Mientras dura, la descarga sigue esperando en `_resolve_auth`
        y la vista recibe el avance por `log_line`.
        """
        if token not in self._auth_futures:
            log.warning("cherry: token de auth desconocido (%s)", token)
            return False
        try:
            self._runtime.submit(self._login_and_resolve(site, token))
        except RuntimeError as exc:
            log.error("cherry: no se pudo lanzar el login: %s", exc)
            self._runtime.call_soon(self._set_auth_result, token, False)
            return False
        return True

    # ── lo que corre en el hilo del loop ────────────────────────────────────

    async def _runner(self, profile: dict, **kwargs) -> None:
        # Import perezoso: arrastra httpx y todo el motor, no se paga al abrir
        # la app sino al primer uso del módulo.
        from third_party.cherry_dl.download_service import run_profile_download

        self._task = asyncio.current_task()
        try:
            summary = await run_profile_download(
                profile,
                emit=self._on_event,
                resolve_auth=self._resolve_auth,
                **kwargs,
            )
            self.finished_signal.emit(summary)
        except asyncio.CancelledError:
            self.cancelled_signal.emit()
            raise
        except Exception as exc:  # el motor ya loguea los errores por fuente
            log.exception("cherry: la descarga falló")
            self.failed_signal.emit(f"{type(exc).__name__}: {exc}")
        finally:
            # Se libera ANTES de que la GUI procese la señal de fin (que va en
            # cola), así el slot que reciba `finished_signal` ya puede lanzar
            # la siguiente descarga.
            self._release()

    def _release(self) -> None:
        """Deja el puente libre. Idempotente: lo llaman el runner y, si la task
        murió sin llegar a correr, el callback del future."""
        self._busy = False
        self._task = None
        self._future = None
        self._abort_pending_auth()

    def _on_future_done(self, fut) -> None:
        if not self._busy:
            return  # camino normal: el runner ya se liberó
        self._release()
        if fut.cancelled():
            self.cancelled_signal.emit()

    def _on_event(self, ev) -> None:
        """`emit` del motor. Corre en el hilo del loop: aquí NO se toca la GUI,
        solo se emiten señales (que Qt entrega en cola al hilo receptor)."""
        from third_party.cherry_dl.download_service import (
            BatchInfo, Cooldown, Counters, Log, ScanComplete, ScanProgress,
            SiteBlocked, SourceDone, SourceStarted, WorkerDone, WorkerIdle,
            WorkerProgress, WorkerStart, WorkersResolved,
        )

        cls = type(ev)
        if cls is Log:
            self.log_line.emit(ev.msg, ev.level)
        elif cls is WorkerProgress:
            # El único evento de alta frecuencia: se estrangula por slot, pero
            # el 100 % siempre pasa para que la barra no quede a medias.
            now = time.monotonic()
            last = self._last_progress.get(ev.slot, 0.0)
            if ev.done >= ev.total or now - last >= _PROGRESS_MIN_INTERVAL:
                self._last_progress[ev.slot] = now
                self.worker_progress.emit(ev.slot, ev.done, ev.total)
        elif cls is WorkerStart:
            self._last_progress.pop(ev.slot, None)
            self.worker_start.emit(ev.slot, ev.filename)
        elif cls is WorkerDone:
            self.worker_done.emit(ev.slot, ev.filename, ev.icon)
        elif cls is WorkerIdle:
            self.worker_idle.emit(ev.slot)
        elif cls is Counters:
            self.counters.emit(ev.downloaded, ev.skipped, ev.errors, ev.deferred)
        elif cls is ScanProgress:
            self.scan_progress.emit(ev.seen, ev.queued)
        elif cls is WorkersResolved:
            self.workers_resolved.emit(ev.count)
        elif cls is SourceStarted:
            self.source_started.emit(ev.url, ev.artist, ev.site)
        elif cls is SourceDone:
            self.source_done.emit(ev.url_id, ev.file_count)
        elif cls is BatchInfo:
            self.batch_info.emit(ev.total, ev.offset)
        elif cls is Cooldown:
            self.cooldown.emit(float(ev.seconds))
        elif cls is ScanComplete:
            self.scan_complete.emit(ev.pending)
        elif cls is SiteBlocked:
            self.site_blocked.emit(ev.site, ev.streak, ev.detail)
        else:
            # El motor es vendorizado: si upstream añade un evento, se ve aquí
            # en vez de desaparecer en silencio.
            log.warning("cherry: evento no mapeado %s", cls.__name__)
            self.unknown_event.emit(ev)

    async def _resolve_auth(self, site: str) -> bool:
        """Espera a que el hilo GUI resuelva la autenticación (A1.3).

        Corre en el hilo del loop; el Future nace en este loop, por eso solo se
        puede resolver con `call_soon_threadsafe`.
        """
        loop = asyncio.get_running_loop()
        self._auth_token += 1
        token = self._auth_token
        fut: asyncio.Future = loop.create_future()
        self._auth_futures[token] = fut
        self.auth_required.emit(site, token)
        try:
            return await fut
        except asyncio.CancelledError:
            # La descarga se canceló mientras el diálogo seguía abierto: hay
            # que avisar a la GUI para que lo cierre.
            self.auth_aborted.emit(token)
            raise
        finally:
            self._auth_futures.pop(token, None)

    async def _login_and_resolve(self, site: str, token: int) -> None:
        """Corre en el loop: hace el login guiado y resuelve el Future."""
        ok = False
        if site != "patreon":
            # Pixiv se autentica por cookie pegada a mano; su diálogo llega con
            # el resto de la vista de ajustes (A5).
            self.log_line.emit(f"✗ Auth de {site} todavía no soportada aquí", "error")
        else:
            from third_party.cherry_dl.auth.patreon import guided_login_patreon

            self.log_line.emit("⟳ Abriendo el navegador para iniciar sesión…", "info")
            try:
                cookies = await guided_login_patreon(
                    on_status=lambda m: self.log_line.emit(f"    {m}", "dim"),
                )
                ok = bool(cookies and cookies.get("session_id"))
            except asyncio.CancelledError:
                self._set_auth_result(token, False)
                raise
            except Exception as exc:
                log.exception("cherry: el login guiado falló")
                self.log_line.emit(f"✗ Error en el login: {exc}", "error")
            else:
                self.log_line.emit(
                    "✓ Sesión iniciada y guardada" if ok else "✗ Login incompleto",
                    "ok" if ok else "error",
                )
        self._set_auth_result(token, ok)

    def _set_auth_result(self, token: int, ok: bool) -> None:
        """Corre dentro del loop, encolado por `resolve_auth_result`."""
        fut = self._auth_futures.get(token)
        if fut is not None and not fut.done():
            fut.set_result(ok)

    def _abort_pending_auth(self) -> None:
        """Si la descarga murió con un diálogo abierto, no dejar Futures colgados."""
        for token, fut in list(self._auth_futures.items()):
            if not fut.done():
                fut.cancel()
            self._auth_futures.pop(token, None)
            self.auth_aborted.emit(token)
