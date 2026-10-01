"""
Runtime asyncio de cherry-dl dentro de Panopticon (fase A1.1 / A1.4).

`third_party/cherry_dl/` es asíncrono de arriba a abajo: incluso listar los
perfiles (`index.list_profiles`) es una corrutina. Panopticon, en cambio, es
Qt síncrono y su `main.py` mantiene asyncio fuera del hilo GUI (decisión cerrada, §4 de
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
from concurrent.futures import Future, InvalidStateError
import logging
import threading
from typing import Any, Coroutine, Optional

from PySide6.QtCore import QObject, QThread, Signal

log = logging.getLogger(__name__)

# Margen para que el loop arranque; si no lo hace, algo está muy roto.
_START_TIMEOUT = 10.0
# Margen para que el loop drene sus tasks al parar.
_STOP_TIMEOUT = 6.0
_LIVE_THREADS = set()


class _LoopThread(QThread):
    """Hilo cuyo único trabajo es correr un event loop asyncio hasta que se
    le pida parar. No emite nada: quien habla con la GUI es `CherryRuntime`."""

    def __init__(self) -> None:
        super().__init__()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self.jobs = set()
        self._shutdown_task = None

    def wait_ready(self, timeout: float = _START_TIMEOUT) -> bool:
        return self._ready.wait(timeout)

    def run(self) -> None:  # corre YA en el hilo nuevo
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self._ready.set()
        if self._stop_requested.is_set(): loop.call_soon(self.begin_stop)
        try:
            loop.run_forever()
        finally:
            try:
                self._drain(loop)
            finally:
                asyncio.set_event_loop(None)
                loop.close()
                self.loop = None

    def begin_stop(self):
        if self._shutdown_task is None:
            self._shutdown_task = self.loop.create_task(self._finish_admitted())

    async def _finish_admitted(self):
        # Cancellation acknowledgement of concurrent Future is not task cleanup.
        # Track actual admitted tasks, including cancellation handlers and hashes.
        if self.jobs:
            await asyncio.gather(*tuple(self.jobs), return_exceptions=True)
        self.loop.stop()

    @staticmethod
    def _drain(loop: asyncio.AbstractEventLoop) -> None:
        """Finish residual cleanup and executor work; never kill/abandon writes.

        Normal shutdown first waits for admitted jobs. Only their residual
        background tasks are cancelled here, once, and fully awaited.
        """
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending: task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending,return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())


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
        self._closing = False
        self._lifecycle = threading.Lock()

    # ── ciclo de vida ───────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        th = self._thread
        return bool(th and th.isRunning() and th.loop is not None)

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._thread.loop if self._thread else None

    def start(self) -> bool:
        """Start once; permanent shutdown cannot be reversed by late UI callbacks."""
        with self._lifecycle:
            if self._closing: return False
            if self._thread is not None and self._thread.isRunning(): return True
            thread = _LoopThread()
            self._thread = thread
            _LIVE_THREADS.add(thread)
            thread.finished.connect(lambda: _LIVE_THREADS.discard(thread))
            thread.finished.connect(lambda: self._stopped(thread))
            thread.start()
        if not thread.wait_ready():
            log.error("cherry: el event loop no arrancó en %.0fs",_START_TIMEOUT)
            self.request_stop()  # Keep the owner retained; never quit/lose a live thread.
            return False
        with self._lifecycle:
            if self._closing: return False
        log.info("cherry: event loop asyncio listo en %s",thread)
        self.started_signal.emit()
        return True

    def _stopped(self,thread):
        _LIVE_THREADS.discard(thread)
        if self._thread is thread:
            self._thread = None
            log.info("cherry: event loop asyncio detenido")
            self.stopped_signal.emit()

    def request_stop(self):
        """Close submission without waiting; caller cancels its download/maintenance.

        Actual tasks finish cleanup before loop/executor shutdown. Accepted short
        mutations and queries drain naturally. No timeout means forced termination.
        """
        with self._lifecycle:
            if self._closing: return
            self._closing = True
            thread = self._thread
            if thread is None: return
            thread._stop_requested.set()
            loop = thread.loop
            if loop is not None and not loop.is_closed():
                try: loop.call_soon_threadsafe(thread.begin_stop)
                except RuntimeError: pass  # Native cleanup is already in progress.

    def shutdown_ready(self):
        thread = self._thread
        return thread is None or not thread.isRunning()

    def stop(self,timeout: float = _STOP_TIMEOUT) -> None:
        """Blocking compatibility API; GUI uses request_stop/readiness instead.

        An expired wait preserves the live owner and raises. It never calls
        terminate or claims that tasks/connections have finished.
        """
        thread = self._thread
        if thread is not None and QThread.currentThread() is thread:
            raise RuntimeError('Cannot wait for Cherry runtime from its loop')
        self.request_stop()
        if thread is not None and not thread.wait(int(timeout*1000)):
            raise TimeoutError('Cherry runtime is still finishing admitted work')
        if thread is not None: self._stopped(thread)

    # ── entrega de trabajo ──────────────────────────────────────────────────

    def submit(self,coro: Coroutine[Any,Any,Any]):
        """Admit an owned task and return its concurrent Future.

        Unlike a cancelled Future, the actual task remains tracked until its
        finally blocks finish. Schedule admission and shutdown under one lock.
        """
        with self._lifecycle:
            thread,loop = self._thread,self.loop
            if self._closing or loop is None or loop.is_closed():
                coro.close()
                raise RuntimeError("El runtime de cherry-dl no admite trabajo.")
            future = Future()
            def admit():
                if future.cancelled():
                    coro.close()
                    return
                try: task = loop.create_task(coro)
                except BaseException as error:
                    coro.close()
                    if not future.done(): future.set_exception(error)
                    return
                thread.jobs.add(task)
                def cancel(result):
                    if result.cancelled() and not task.done():
                        try: loop.call_soon_threadsafe(task.cancel)
                        except RuntimeError: pass
                future.add_done_callback(cancel)
                def completed(task):
                    thread.jobs.discard(task)
                    if task.cancelled():
                        future.cancel()
                    else:
                        error = task.exception()
                        try:
                            if not future.done():
                                if error is not None: future.set_exception(error)
                                else: future.set_result(task.result())
                        except InvalidStateError:
                            if not future.cancelled(): raise
                task.add_done_callback(completed)
            try: loop.call_soon_threadsafe(admit)
            except RuntimeError:
                coro.close()
                raise
            return future

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
