"""
Cola de descargas: varios perfiles, uno detrás de otro (A3).

Vive en el hilo GUI y no sabe nada de asyncio — solo enlaza el final de una
descarga con el principio de la siguiente. Todo el trabajo pasa por un único
`DownloadBridge`, así que nunca hay dos descargas a la vez: el motor abre su
propio pool de workers por perfil y solaparlos multiplicaría las peticiones
contra el mismo sitio, que es justo lo que los `max_workers` del template
intentan evitar.

Un perfil que falla no mata el lote: se anota y se sigue con el siguiente.
Cancelar, en cambio, aborta la cola entera — cancelar "solo este" no tendría
forma de expresarse en un botón.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QObject, Signal

from .download_bridge import DownloadBridge

log = logging.getLogger(__name__)


class QueueTotals:
    """Acumulado del lote. Mismos campos que `DownloadSummary`, más los fallos."""

    def __init__(self) -> None:
        self.downloaded = 0
        self.skipped = 0
        self.errors = 0
        self.deferred = 0
        self.done = 0
        self.failed: list[tuple[str, str]] = []  # (perfil, mensaje)

    def add(self, summary: Any) -> None:
        self.downloaded += getattr(summary, "downloaded", 0)
        self.skipped += getattr(summary, "skipped", 0)
        self.errors += getattr(summary, "errors", 0)
        self.deferred += getattr(summary, "deferred", 0)
        self.done += 1


class DownloadQueue(QObject):
    """Ejecuta una lista de perfiles ya detallados (con su clave `urls`)."""

    profile_started  = Signal(int, int, str)   # posición (1-based), total, nombre
    profile_finished = Signal(str, object)     # nombre, DownloadSummary | None
    queue_finished   = Signal(object)          # QueueTotals
    queue_cancelled  = Signal(object)          # QueueTotals parcial

    def __init__(self, bridge: DownloadBridge, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._pending: list[dict] = []
        self._total = 0
        self._current: dict | None = None
        self._totals = QueueTotals()
        self._params: dict = {}
        self._cancelling = False

        bridge.finished_signal.connect(self._on_finished)
        bridge.failed_signal.connect(self._on_failed)
        bridge.cancelled_signal.connect(self._on_cancelled)

    @property
    def is_running(self) -> bool:
        return self._current is not None or bool(self._pending)

    @property
    def totals(self) -> QueueTotals:
        return self._totals

    def start(self, profiles: list[dict], **params) -> bool:
        """Arranca el lote. False si ya hay uno corriendo o la lista está vacía."""
        if self.is_running or self._bridge.is_busy:
            log.warning("cherry: ya hay una cola en marcha")
            return False
        if not profiles:
            return False
        self._pending = list(profiles)
        self._total = len(profiles)
        self._totals = QueueTotals()
        self._params = params
        self._cancelling = False
        return self._advance()

    def cancel(self) -> bool:
        """Aborta la cola: la descarga en curso y todo lo que quede detrás."""
        if not self.is_running:
            return False
        self._cancelling = True
        self._pending.clear()
        if self._current is not None:
            return self._bridge.cancel()
        return True

    # ── interno ───────────────────────────────────────────────────────────
    def _advance(self) -> bool:
        if self._cancelling or not self._pending:
            self._current = None
            (self.queue_cancelled if self._cancelling else self.queue_finished).emit(self._totals)
            return False

        profile = self._pending.pop(0)
        self._current = profile
        position = self._total - len(self._pending)
        self.profile_started.emit(position, self._total, profile["display_name"])
        if self._bridge.start(profile, **self._params):
            return True

        # No arrancó (runtime caído o puente ocupado): no se puede seguir.
        self._current = None
        self._totals.failed.append((profile["display_name"], "no se pudo iniciar"))
        self._pending.clear()
        self.queue_finished.emit(self._totals)
        return False

    def _name(self) -> str:
        return self._current["display_name"] if self._current else "?"

    def _on_finished(self, summary) -> None:
        if self._current is None:
            return  # descarga suelta, no de esta cola
        name = self._name()
        self._totals.add(summary)
        self.profile_finished.emit(name, summary)
        self._advance()

    def _on_failed(self, message: str) -> None:
        if self._current is None:
            return
        name = self._name()
        self._totals.failed.append((name, message))
        self.profile_finished.emit(name, None)
        self._advance()

    def _on_cancelled(self) -> None:
        if self._current is None:
            return
        self._cancelling = True
        self.profile_finished.emit(self._name(), None)
        self._current = None
        self._pending.clear()
        self.queue_cancelled.emit(self._totals)
