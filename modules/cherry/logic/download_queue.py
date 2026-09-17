"""Planificador batch de Cherry por pasadas globales de categoría.

Los trabajos se ordenan categoría → perfil: todos los oficiales de pago,
después los gratuitos, repositorios y, por compatibilidad, legado/desconocidos.
El puente sigue ejecutando un solo trabajo a la vez, por lo que nunca se
multiplican peticiones contra un sitio. Los resultados parciales se agregan y
la GUI recibe exactamente un cierre por perfil.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from .download_bridge import DownloadBridge

log = logging.getLogger(__name__)


@dataclass
class ProfileSummary:
    downloaded: int = 0
    skipped: int = 0
    errors: int = 0
    deferred: int = 0
    dropped: int = 0
    awaiting_archive: int = 0

    def add(self, summary: Any) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(summary, name, 0))


class QueueTotals(ProfileSummary):
    """Acumulado del lote, contado por perfiles y no por pasadas."""

    def __init__(self) -> None:
        super().__init__()
        self.done = 0
        self.failed: list[tuple[str, str]] = []


@dataclass
class _ProfileState:
    profile: dict
    position: int
    summary: ProfileSummary = field(default_factory=ProfileSummary)
    remaining: int = 0
    started: bool = False
    emitted: bool = False
    failed: bool = False


@dataclass(frozen=True)
class _Job:
    state: _ProfileState
    category: Any


class DownloadQueue(QObject):
    """Ejecuta perfiles detallados en pasadas globales y de forma secuencial."""

    profile_started = Signal(int, int, str)
    active_profile_changed = Signal(object)  # profile id while active, else None
    job_progress = Signal(int, int)  # trabajos terminados, trabajos totales
    profile_finished = Signal(object, object)
    queue_finished = Signal(object)
    queue_cancelled = Signal(object)

    def __init__(self, bridge: DownloadBridge, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._pending: list[_Job] = []
        self._states: list[_ProfileState] = []
        self._total = 0
        self._job_total = 0
        self._jobs_done = 0
        self._current: _Job | None = None
        self._totals = QueueTotals()
        self._params: dict = {}
        self._params_for: Callable[[dict], dict] | None = None
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

    def start(
        self,
        profiles: list[dict],
        *,
        params_for: Callable[[dict], dict] | None = None,
        **params,
    ) -> bool:
        if self.is_running or self._bridge.is_busy:
            log.warning("cherry: ya hay una cola en marcha")
            return False
        if not profiles:
            return False

        params = dict(params)
        params.setdefault(
            "validate_sources",
            len(profiles) > 1 or bool(params.get("force_full")),
        )

        # Import perezoso: abrir la vista no debe cargar templates/httpx.
        from third_party.cherry_dl.download_service import source_category

        self._states = [
            _ProfileState(profile, position)
            for position, profile in enumerate(profiles, 1)
        ]
        jobs_by_category: dict[Any, list[_Job]] = {}
        for state in self._states:
            categories = {
                source_category(source.get("url") or "")
                for source in state.profile.get("urls", [])
                if source.get("url") and (
                    source.get("enabled")
                    or (params.get("force_full") and source.get("auto_disabled"))
                )
            }
            for category in categories:
                jobs_by_category.setdefault(category, []).append(_Job(state, category))
                state.remaining += 1

        self._pending = [
            job
            for category in sorted(jobs_by_category, key=int)
            for job in jobs_by_category[category]
        ]
        self._job_total = len(self._pending)
        self._jobs_done = 0
        self.job_progress.emit(0, self._job_total)
        self._total = len(profiles)
        self._totals = QueueTotals()
        self._params = params
        self._params_for = params_for
        self._cancelling = False

        # Un perfil sin fuentes activas también termina exactamente una vez.
        for state in self._states:
            if state.remaining == 0:
                self._finish_profile(state)
        if not self._pending:
            self.queue_finished.emit(self._totals)
            return True
        return self._advance()

    def cancel(self) -> bool:
        if not self.is_running:
            return False
        self._cancelling = True
        self._pending.clear()
        if self._current is not None:
            return self._bridge.cancel()
        return True

    def _advance(self) -> bool:
        if self._cancelling or not self._pending:
            self._current = None
            self.active_profile_changed.emit(None)
            (self.queue_cancelled if self._cancelling else self.queue_finished).emit(
                self._totals
            )
            return False

        job = self._pending.pop(0)
        self._current = job
        state = job.state
        state.started = True
        profile = state.profile
        self.active_profile_changed.emit(profile["id"])
        self.profile_started.emit(state.position, self._total, profile["display_name"])
        params = dict(self._params)
        if self._params_for is not None:
            params.update(self._params_for(profile))
        params.update(
            source_categories={job.category},
            finalize_profile=(state.remaining == 1),
        )
        if self._bridge.start(profile, **params):
            return True

        self._current = None
        self.active_profile_changed.emit(None)
        self._totals.failed.append((profile["display_name"], "no se pudo iniciar"))
        self._pending.clear()
        self.queue_finished.emit(self._totals)
        return False

    def _finish_profile(self, state: _ProfileState, *, cancelled: bool = False) -> None:
        if state.emitted:
            return
        state.emitted = True
        if not cancelled and not state.failed:
            self._totals.done += 1
        self.profile_finished.emit(
            state.profile,
            None if cancelled or state.failed else state.summary,
        )

    def _complete_job(self, summary: Any | None, error: str | None = None) -> None:
        if self._current is None:
            return
        state = self._current.state
        if summary is not None:
            state.summary.add(summary)
            self._totals.add(summary)
        if error is not None:
            state.failed = True
            self._totals.failed.append((state.profile["display_name"], error))
        self._jobs_done += 1
        self.job_progress.emit(self._jobs_done, self._job_total)
        state.remaining -= 1
        if state.remaining == 0:
            self._finish_profile(state)
        self._current = None
        self._advance()

    def _on_finished(self, summary: Any) -> None:
        self._complete_job(summary)

    def _on_failed(self, message: str) -> None:
        self._complete_job(None, message)

    def _on_cancelled(self) -> None:
        if self._current is None:
            return
        self._current.state.failed = True
        self._current = None
        self.active_profile_changed.emit(None)
        self._cancelling = True
        self._pending.clear()
        # Una pasada previa pudo haber escrito archivos: cada perfil tocado
        # avisa una sola vez para que el Librarian reindexe su carpeta.
        for state in self._states:
            if state.started and not state.emitted:
                self._finish_profile(state, cancelled=True)
        self.queue_cancelled.emit(self._totals)
