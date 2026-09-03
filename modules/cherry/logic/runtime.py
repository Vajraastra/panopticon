"""
Runtime asyncio de cherry-dl dentro de Panopticon (fase A1.1 / A1.4).

`third_party/cherry_dl/` es asíncrono de arriba a abajo: incluso listar los
perfiles (`index.list_profiles`) es una corrutina. Panopticon, en cambio, es
Qt síncrono y su `main.py` NO adopta qasync (decisión cerrada, §4 de
CHERRY_FUSION_DESIGN.md: sería invasivo para los 13 módulos). El puente es
este: un QThread propietario de un event loop asyncio persistente al que el
hilo GUI le entrega corrutinas.

    runtime = CherryRuntime()
    runtime.start()
    fut = runtime.submit(list_profiles(INDEX_DB))   # concurrent.futures.Future
    perfiles = fut.result(timeout=10)               # bloquea el hilo GUI

Reglas de uso:

* **Nunca** bloquear el hilo GUI con `fut.result()` sobre algo que toca la red
  (una descarga dura minutos). Para eso está el callback: `fut.add_done_callback`
  corre en el hilo del loop, así que dentro solo se emiten señales, jamás se
  tocan widgets. `submit()` es para lecturas cortas de `index.db`.
* El loop es único y persistente: sobrevive a varias descargas y mantiene vivo
  el pool de conexiones de `DownloadEngine` entre llamadas.
* La cancelación se hace desde el hilo GUI con `call_soon_threadsafe`; ver
  `CherryRuntime.cancel_task`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Coroutine, Optional

from PySide6.QtCore import QObject, QThread, Signal

log = logging.getLogger(__name__)

# Margen para que el loop arranque; si no lo hace, algo está muy roto.
_START_TIMEOUT = 10.0
# Margen para que el loop drene sus tasks al parar.
_STOP_TIMEOUT = 15.0


class _LoopThread(QThread):
    """Hilo cuyo único trabajo es correr un event loop asyncio hasta que se
    le pida parar. No emite nada: quien habla con la GUI es `CherryRuntime`."""

    def __init__(self) -> None:
        super().__init__()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()

    def wait_ready(self, timeout: float = _START_TIMEOUT) -> bool:
        return self._ready.wait(timeout)

    def run(self) -> None:  # corre YA en el hilo nuevo
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                self._drain(loop)
            finally:
                asyncio.set_event_loop(None)
                loop.close()
                self.loop = None

    @staticmethod
    def _drain(loop: asyncio.AbstractEventLoop) -> None:
        """Cancela lo que quede vivo y deja cerrar a los async generators.

        Sin esto, cerrar el loop con tasks pendientes deja sockets abiertos y
        `DownloadEngine` (que es un async context manager) nunca ejecuta su
        `__aexit__`.
        """
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        loop.run_until_complete(loop.shutdown_asyncgens())


class CherryRuntime(QObject):
    """Dueño del hilo/loop asyncio. Vive en el hilo GUI.

    Se crea uno por módulo (no un singleton global): si el módulo se descarga,
    `stop()` se lleva el loop con él.
    """

    # Se emite cuando el loop quedó listo / se apagó. Útil para habilitar UI.
    started_signal = Signal()
    stopped_signal = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: Optional[_LoopThread] = None

    # ── ciclo de vida ───────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        th = self._thread
        return bool(th and th.isRunning() and th.loop is not None)

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._thread.loop if self._thread else None

    def start(self) -> bool:
        """Arranca el loop. Idempotente: llamarlo dos veces no crea dos hilos
        (guard anti doble-inicio del contrato de módulos, regla #3)."""
        if self.is_running:
            return True
        if self._thread is not None and self._thread.isRunning():
            return True  # arrancando ahora mismo en otro punto del código

        thread = _LoopThread()
        thread.start()
        if not thread.wait_ready():
            log.error("cherry: el event loop no arrancó en %.0fs", _START_TIMEOUT)
            thread.quit()
            return False
        self._thread = thread
        log.info("cherry: event loop asyncio listo en %s", thread)
        self.started_signal.emit()
        return True

    def stop(self, timeout: float = _STOP_TIMEOUT) -> None:
        """Para el loop y espera al hilo. Seguro de llamar sin haber arrancado.

        Es lo que se invoca al cerrar la app: `run_forever` retorna, el `finally`
        del hilo cancela lo pendiente y cierra el loop.
        """
        thread = self._thread
        if thread is None:
            return
        loop = thread.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        if not thread.wait(int(timeout * 1000)):
            log.warning("cherry: el hilo del loop no terminó en %.0fs", timeout)
            thread.terminate()
            thread.wait(2000)
        self._thread = None
        log.info("cherry: event loop asyncio detenido")
        self.stopped_signal.emit()

    # ── entrega de trabajo ──────────────────────────────────────────────────

    def submit(self, coro: Coroutine[Any, Any, Any]):
        """Encola una corrutina en el loop y devuelve un `concurrent.futures.Future`.

        Levanta `RuntimeError` si el loop no está corriendo — y cierra la
        corrutina para no dejar el clásico "coroutine was never awaited".
        """
        loop = self.loop
        if loop is None or loop.is_closed():
            coro.close()
            raise RuntimeError("El runtime de cherry-dl no está corriendo.")
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def run_sync(self, coro: Coroutine[Any, Any, Any], timeout: float = 30.0):
        """Atajo para lecturas cortas de `index.db`: encola y espera.

        BLOQUEA el hilo llamante. Válido para una query de SQLite local; nunca
        para algo que toque la red.
        """
        return self.submit(coro).result(timeout)

    def call_soon(self, fn, *args) -> bool:
        """Ejecuta `fn(*args)` dentro del loop desde cualquier hilo.

        Es la única vía legítima para tocar objetos asyncio (Future, Task) que
        nacieron en el loop: `Future.set_result` y `Task.cancel` NO son
        thread-safe.
        """
        loop = self.loop
        if loop is None or loop.is_closed():
            return False
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # el loop se cerró entre el check y la llamada
            return False
        return True

    def cancel_task(self, task: Optional[asyncio.Task]) -> bool:
        """Cancela una task del loop desde el hilo GUI (A1.4)."""
        if task is None or task.done():
            return False
        return self.call_soon(task.cancel)
