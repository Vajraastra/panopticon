"""
Consola de descarga: lo que el puente emite, hecho pantalla (A3).

No decide nada — se limita a pintar los eventos de `DownloadBridge` y los saltos
de perfil de `DownloadQueue`. Toda la lógica de arranque, cola y cancelación
vive fuera.

Dos detalles que existen por una razón, no por adorno:
  • el enfriamiento se muestra como cuenta atrás, porque un cliente que espera
    30 s en silencio es indistinguible de uno colgado;
  • el log tiene tope de líneas: una descarga larga emite miles y un
    `QPlainTextEdit` sin límite se come la memoria y ralentiza el repintado.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar,
    QVBoxLayout, QWidget,
)

_MAX_LOG_LINES = 800


class _WorkerRow(QWidget):
    """Una ranura del pool: archivo en curso y su barra."""

    def __init__(self, slot: int, dim: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._filename = "—"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self._num = QLabel(f"{slot + 1}")
        self._num.setFixedWidth(18)
        self._num.setStyleSheet(f"color: {dim}; font-size: 11px;")
        lay.addWidget(self._num)

        self._name = QLabel("—")
        self._name.setWordWrap(True)
        self._name.setMinimumWidth(0)
        self._name.setStyleSheet(f"color: {dim}; font-size: 11px;")
        lay.addWidget(self._name, stretch=1)

        self._bar = QProgressBar()
        self._bar.setFixedWidth(220)
        self._bar.setFixedHeight(14)
        self._bar.setTextVisible(False)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        lay.addWidget(self._bar)

    def start(self, filename: str) -> None:
        self._filename = filename
        self._name.setText(filename)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)

    def progress(self, done: int, total: int) -> None:
        if total > 0:
            self._bar.setRange(0, 100)
            self._bar.setValue(min(100, int(done * 100 / total)))
        else:
            # Sin Content-Length: barra indeterminada en vez de una fija en 0.
            self._bar.setRange(0, 0)

    def done(self, filename: str, icon: str) -> None:
        self._name.setText(f"{icon} {filename}")
        self._bar.setRange(0, 100)
        self._bar.setValue(100)

    def idle(self) -> None:
        self._name.setText("—")
        self._bar.setRange(0, 100)
        self._bar.setValue(0)

    def status(self, text: str) -> None:
        self._name.setText(f"{self._filename} · {text}" if text else self._filename)


class DownloadPanel(QWidget):
    """Estado en vivo de la descarga. `bind()` la engancha al puente y la cola."""

    def __init__(self, theme=None, locale=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._lm = locale
        self._rows: list[_WorkerRow] = []
        self._cooldown_left = 0
        self._cooldown_until = 0.0
        self._worker_states = {}
        self._worker_activity = {}
        self._quota_state = None
        self._quota_in_flight = 0
        self._quota_next_release = 0
        self._quota_release_at = 0.0

        self._build_ui()

        self._cooldown_timer = QTimer(self)
        self._cooldown_timer.setInterval(1000)
        self._cooldown_timer.timeout.connect(self._tick_cooldown)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._render_worker_states)
        self._status_timer.start()

        self._quota_timer = QTimer(self)
        self._quota_timer.setInterval(1000)
        self._quota_timer.timeout.connect(self._tick_quota)
        self._quota_timer.start()
        self.refresh_pawchive_quota()

    # ── i18n / tema ───────────────────────────────────────────────────────
    def tr(self, key: str, default: str) -> str:
        return self._lm.tr(key, default) if self._lm else default

    def _color(self, key: str, fallback: str) -> str:
        return self._theme.get_color(key) if self._theme else fallback

    # ── Construcción ──────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        dim = self._color("text_dim", "#888")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)

        top = QHBoxLayout()
        self._title = QLabel("—")
        self._title.setStyleSheet(
            f"color: {self._color('accent_main', '#00ffcc')}; "
            "font-size: 15px; font-weight: bold;")
        top.addWidget(self._title)
        top.addStretch(1)
        self._position = QLabel("")
        self._position.setStyleSheet(f"color: {dim}; font-size: 12px;")
        top.addWidget(self._position)
        self._follow_log = QCheckBox(self.tr(
            "cherry.dl_follow_events", "Follow events",
        ))
        self._follow_log.setChecked(True)
        self._follow_log.setToolTip(self.tr(
            "cherry.dl_follow_events_tip",
            "Keep the newest event visible. Turn it off to inspect earlier lines "
            "while downloads continue.",
        ))
        self._follow_log.toggled.connect(self._on_follow_toggled)
        top.addWidget(self._follow_log)
        lay.addLayout(top)

        self._phase = QLabel(self.tr("cherry.dl_idle", "Idle"))
        self._phase.setStyleSheet(f"color: {self._color('text_secondary', '#ccc')}; font-size: 12px;")
        lay.addWidget(self._phase)

        self._counters = QLabel("")
        self._counters.setStyleSheet("font-size: 13px;")
        lay.addWidget(self._counters)

        self._quota = QLabel("")
        self._quota.setWordWrap(True)
        self._quota.setStyleSheet(
            f"color: {self._color('accent_info', '#55aaff')}; font-size: 12px;"
        )
        self._quota.setToolTip(self.tr(
            "cherry.dl_pawchive_quota_tip",
            "Raw HTTP bodies count during receipt, including retries, failed and "
            "cancelled attempts; files count successful completions only. File "
            "and volume timers show their separate releases and recovery targets.",
        ))
        lay.addWidget(self._quota)

        self._file_bar = QProgressBar()
        self._file_bar.setFormat(self.tr(
            "cherry.dl_file_progress", "Files: %v / %m",
        ))
        self._file_bar.setVisible(False)
        lay.addWidget(self._file_bar)

        self._batch_bar = QProgressBar()
        self._batch_bar.setFormat(self.tr(
            "cherry.dl_job_progress", "Batch: %v / %m",
        ))
        self._batch_bar.setVisible(False)
        lay.addWidget(self._batch_bar)

        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet(f"background-color: {self._color('border', '#333')};")
        lay.addWidget(div)

        self._slots_box = QWidget()
        self._slots_layout = QVBoxLayout(self._slots_box)
        self._slots_layout.setContentsMargins(0, 0, 0, 0)
        self._slots_layout.setSpacing(4)
        lay.addWidget(self._slots_box)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(_MAX_LOG_LINES)
        self._log.setStyleSheet(
            f"background-color: {self._color('bg_input', '#000')}; "
            f"color: {self._color('text_secondary', '#ccc')}; "
            f"border: 1px solid {self._color('border', '#333')}; font-size: 11px;")
        lay.addWidget(self._log, stretch=1)

        self._reset_counters()

    # ── Enganche ──────────────────────────────────────────────────────────
    def bind(self, bridge, queue=None) -> None:
        bridge.log_line.connect(self.append_log)
        bridge.workers_resolved.connect(self._on_workers)
        bridge.source_started.connect(self._on_source_started)
        bridge.cooldown.connect(self._on_cooldown)
        bridge.batch_info.connect(self._on_batch_info)
        bridge.file_progress.connect(self._on_file_progress)
        bridge.scan_progress.connect(self._on_scan_progress)
        bridge.scan_complete.connect(self._on_scan_complete)
        bridge.worker_start.connect(self._on_worker_start)
        bridge.worker_progress.connect(self._on_worker_progress)
        bridge.worker_done.connect(self._on_worker_done)
        bridge.worker_idle.connect(self._on_worker_idle)
        bridge.worker_state.connect(self._on_worker_state)
        bridge.counters.connect(self._on_counters)
        bridge.source_done.connect(self._on_source_done)
        bridge.site_blocked.connect(self._on_site_blocked)
        bridge.quota_status.connect(self._on_quota_status)
        if queue is not None:
            queue.profile_started.connect(self._on_profile_started)
            queue.job_progress.connect(self._on_job_progress)

    # ── API de la vista ───────────────────────────────────────────────────
    def reset(self, title: str = "") -> None:
        self._title.setText(title or "—")
        self._position.setText("")
        self._log.clear()
        self._reset_counters()
        self._stop_cooldown()
        self._file_bar.setVisible(False)
        self._batch_bar.setVisible(False)
        self.set_phase(self.tr("cherry.dl_starting", "Starting…"))
        for row in self._rows:
            row.idle()
        self.refresh_pawchive_quota()

    def set_phase(self, text: str) -> None:
        self._stop_cooldown()
        for state in self._worker_states.values():
            if state.slot < len(self._rows):
                self._rows[state.slot].idle()
        self._worker_states.clear()
        self._worker_activity.clear()
        self._phase.setText(text)

    def append_log(self, message: str, level: str = "info") -> None:
        colors = {
            "ok": self._color("accent_success", "#00ff66"),
            "warn": self._color("accent_warning", "#ff5555"),
            "error": self._color("accent_warning", "#ff5555"),
            "dim": self._color("text_dim", "#888"),
        }
        color = colors.get(level, self._color("text_primary", "#fff"))
        safe = (message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        bar = self._log.verticalScrollBar()
        previous_position = bar.value()
        self._log.appendHtml(f'<span style="color:{color}">{safe}</span>')
        if self._follow_log.isChecked():
            # El layout del documento se completa después de appendHtml. Hacer
            # el salto en el siguiente turno evita que el máximo aún sea el de
            # la línea anterior y el seguimiento parezca quedarse congelado.
            QTimer.singleShot(0, self._scroll_log_to_end)
        else:
            # appendHtml mueve el cursor al final incluso cuando el usuario
            # pausó el seguimiento. Restaurar la posición mantiene legible la
            # zona que está analizando mientras siguen entrando eventos.
            bar.setValue(min(previous_position, bar.maximum()))

    def _on_follow_toggled(self, enabled: bool) -> None:
        if enabled:
            self._scroll_log_to_end()

    def _scroll_log_to_end(self) -> None:
        bar = self._log.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ── Slots ─────────────────────────────────────────────────────────────
    def _reset_counters(self) -> None:
        self._on_counters(0, 0, 0, 0)

    def _on_counters(self, downloaded: int, skipped: int, errors: int, deferred: int) -> None:
        ok = self._color("accent_success", "#00ff66")
        dim = self._color("text_dim", "#888")
        warn = self._color("accent_warning", "#ff5555")
        self._counters.setText(
            f'<span style="color:{ok}">↓ {downloaded}</span>&nbsp;&nbsp;'
            f'<span style="color:{dim}">⏭ {skipped}</span>&nbsp;&nbsp;'
            f'<span style="color:{warn if errors else dim}">✗ {errors}</span>&nbsp;&nbsp;'
            f'<span style="color:{dim}">⏸ {deferred}</span>'
        )

    def refresh_pawchive_quota(self) -> None:
        """Lee el mismo estado persistente que hace cumplir el motor."""
        try:
            from third_party.cherry_dl import config as cherry_config
            from third_party.cherry_dl.site_throttle import SiteThrottle

            cfg = cherry_config.load_config().pawchive
            gate = SiteThrottle(
                "pawchive",
                limit=cfg.downloads_per_window,
                byte_limit=int(cfg.gigabytes_per_window * 1024**3),
                raw_traffic=True,
                adaptive=cfg.adaptive_quota,
                resume_headroom=cfg.resume_headroom,
                window_seconds=cfg.window_hours * 60 * 60,
                failure_threshold=1,
                failure_window_seconds=0,
                cooldowns=(0,),
                path=cherry_config.CHERRY_DIR / "site_throttle.json",
            )
            self._set_quota(gate.snapshot(), 0)
        except Exception:
            # El medidor nunca debe impedir abrir Cherry si el config quedó
            # temporalmente ilegible; el diálogo de ajustes dará el error útil.
            self._quota.setVisible(False)

    def _on_quota_status(self, event) -> None:
        self._set_quota(event.snapshot, event.in_flight_bytes)

    def _set_quota(self, snapshot, in_flight_bytes: int) -> None:
        if snapshot.site != "pawchive":
            return
        self._quota_state = snapshot
        self._quota_received_at = time.time()
        self._quota_in_flight = max(0, int(in_flight_bytes))
        self._quota_next_release = max(
            0, int(round(snapshot.next_release_seconds)),
        )
        self._quota_release_at = time.time() + snapshot.next_release_seconds
        self._render_quota()
        self._quota.setVisible(True)

    def _render_quota(self) -> None:
        state = self._quota_state
        if state is None:
            self._quota.setVisible(False)
            return
        if state.raw_bytes:
            self._render_raw_quota(state)
            return
        projected = state.used_bytes + self._quota_in_flight
        remaining = max(0, state.byte_limit - projected)
        files_left = max(
            0, state.file_limit - state.used_files - state.active_files,
        )
        release = (
            self._format_duration(self._quota_next_release)
            if self._quota_next_release > 0
            else self.tr("cherry.dl_quota_no_release", "no usage yet")
        )
        recovery = ""
        if state.recovery_active:
            recovery = self.tr(
                "cherry.dl_quota_recovery",
                " · recovery: resumes at {resume_at}/{file_limit}",
            ).format(
                resume_at=state.resume_at_files,
                file_limit=state.file_limit,
            )
        active = ""
        if self._quota_in_flight:
            active = self.tr(
                "cherry.dl_quota_active", " · {gb:.3f} GB downloading",
            ).format(gb=self._quota_in_flight / 2**30)
        self._quota.setText(self.tr(
            "cherry.dl_pawchive_quota",
            "Pawchive · {remaining:.3f} GB remaining of {limit:g} · "
            "{files} files remaining of {file_limit} · {hours:g} h rolling "
            "window · next release: {release}{recovery}{active}",
        ).format(
            remaining=remaining / 2**30,
            limit=state.byte_limit / 2**30,
            files=files_left,
            file_limit=state.file_limit,
            hours=state.window_seconds / 3600,
            release=release,
            recovery=recovery,
            active=active,
        ))

    def _render_raw_quota(self, state) -> None:
        def deadline(seconds):
            if seconds <= 0:
                return self.tr("cherry.dl_quota_ready", "available")
            remaining = max(0, math.ceil(self._quota_received_at + seconds - time.time()))
            return self._format_duration(remaining) if remaining else self.tr(
                "cherry.dl_quota_ready", "available")

        mode_key = state.mode
        if mode_key == "normal" and state.recovery_active:
            mode_key = "count_wait"
        modes = {
            "normal": ("cherry.dl_mode_normal", "normal · up to 4 MiB/s"),
            "economic": ("cherry.dl_mode_economic", "economic · up to 2 MiB/s"),
            "standby": ("cherry.dl_mode_standby", "waiting to recover volume"),
            "count_wait": ("cherry.dl_mode_count_wait", "waiting to recover file slots"),
        }
        mode = self.tr(*modes[mode_key])
        recovery = ""
        if state.adaptive and (state.mode != "normal" or state.recovery_active):
            recovery = self.tr("cherry.dl_quota_targets",
                " · normal at {target} files: {files_time} · 50% volume available: {bytes_time}").format(
                    target=state.resume_at_files,
                    files_time=deadline(state.file_recovery_seconds),
                    bytes_time=deadline(state.byte_recovery_seconds))
        self._quota.setText(self.tr("cherry.dl_raw_quota",
            "Pawchive · {mode} · {used:.3f}/{limit:g} GiB raw ({remaining:.3f} available, {reserved:.3f} reserved) · "
            "{files}/{file_limit} successful files · {active} active · {hours:g} h rolling window · "
            "file release: {file_time} · volume release: {byte_time}{recovery}").format(
                mode=mode, used=state.used_bytes / 2**30, limit=state.byte_limit / 2**30,
                remaining=max(0, state.byte_limit - state.used_bytes) / 2**30,
                reserved=state.reserved_bytes / 2**30,
                files=state.used_files, file_limit=state.file_limit, active=state.active_files,
                hours=state.window_seconds / 3600,
                file_time=deadline(state.file_release_seconds),
                byte_time=deadline(state.byte_release_seconds), recovery=recovery))

    def _tick_quota(self) -> None:
        if self._quota_state is None or self._quota_next_release <= 0:
            return
        self._quota_next_release = max(0, math.ceil(self._quota_release_at - time.time()))
        if self._quota_next_release <= 0:
            self.refresh_pawchive_quota()
        else:
            self._render_quota()

    def _format_duration(self, seconds: int) -> str:
        hours, remainder = divmod(max(0, int(seconds)), 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return self.tr(
                "cherry.dl_quota_time_hm", "{h} h {m} min",
            ).format(h=hours, m=minutes)
        if minutes:
            return self.tr(
                "cherry.dl_quota_time_ms", "{m} min {s} s",
            ).format(m=minutes, s=secs)
        return self.tr(
            "cherry.dl_quota_time_s", "{s} s",
        ).format(s=secs)

    def _ensure_slots(self, count: int) -> None:
        dim = self._color("text_dim", "#888")
        while len(self._rows) < count:
            row = _WorkerRow(len(self._rows), dim)
            self._rows.append(row)
            self._slots_layout.addWidget(row)
        for i, row in enumerate(self._rows):
            row.setVisible(i < count)

    def _on_workers(self, count: int) -> None:
        self._ensure_slots(count)
        self.append_log(
            self.tr("cherry.dl_workers", "Workers: {n}").format(n=count), "dim")

    def _on_profile_started(self, position: int, total: int, name: str) -> None:
        self._title.setText(name)
        if total > 1:
            self._position.setText(
                self.tr("cherry.dl_position", "{i} of {n}").format(i=position, n=total))
        # El log NO se limpia entre perfiles: en un lote de horas, lo que pasó
        # con los anteriores es justo lo que uno vuelve a mirar. Solo `reset()`
        # —una corrida nueva— empieza en blanco.
        if position > 1:
            self.append_log("", "dim")
        self.append_log(f"── {name} ──", "info")
        self._reset_counters()
        self._file_bar.setVisible(False)
        self._stop_cooldown()
        for row in self._rows:
            row.idle()

    def _on_source_started(self, url: str, artist: str, site: str) -> None:
        self._file_bar.setVisible(False)
        self.set_phase(self.tr("cherry.dl_source", "Source: {a} ({s})").format(a=artist, s=site))

    def _on_scan_progress(self, seen: int, queued: int) -> None:
        self.set_phase(self.tr(
            "cherry.dl_scanning", "Scanning: {seen} seen · {queued} queued"
        ).format(seen=seen, queued=queued))

    def _on_scan_complete(self, pending: int) -> None:
        self.set_phase(self.tr(
            "cherry.dl_scan_done", "Scan complete: {n} pending").format(n=pending))

    def _on_batch_info(self, total: int, offset: int) -> None:
        self.set_phase(self.tr(
            "cherry.dl_batch", "Downloading {total} files (resuming at {offset})"
        ).format(total=total, offset=offset))

    def _on_file_progress(self, done: int, total: int) -> None:
        self._file_bar.setRange(0, max(1, total))
        self._file_bar.setValue(max(0, min(done, total)))
        self._file_bar.setVisible(total > 0)

    def _on_job_progress(self, done: int, total: int) -> None:
        self._batch_bar.setRange(0, max(1, total))
        self._batch_bar.setValue(max(0, min(done, total)))
        self._batch_bar.setVisible(total > 1)

    def _on_worker_start(self, slot: int, filename: str) -> None:
        self._stop_cooldown()
        self._ensure_slots(slot + 1)
        self._rows[slot].start(filename)

    def _on_worker_progress(self, slot: int, done: int, total: int) -> None:
        self._worker_activity[slot] = time.time()
        if slot < len(self._rows):
            self._rows[slot].progress(done, total)
        self._render_worker_states()

    def _on_worker_state(self, state) -> None:
        self._stop_cooldown()
        self._worker_states[state.slot] = state
        if state.phase == "downloading":
            self._worker_activity[state.slot] = time.time()
        self._render_worker_states()

    def _wait_description(self, state) -> str:
        key, default = {
            "quota_count": ("cherry.dl_wait_count", "Waiting for file quota"),
            "quota_count_recovery": ("cherry.dl_wait_recovery", "Waiting to recover file slots"),
            "quota_bytes": ("cherry.dl_wait_bytes", "Waiting for volume quota"),
            "quota_bytes_recovery": ("cherry.dl_wait_bytes_recovery", "Waiting to recover at least 50% of volume quota"),
            "quota_economic": ("cherry.dl_wait_economic", "Waiting for volume to enter economic mode"),
            "circuit": ("cherry.dl_wait_server", "Waiting to probe the server"),
        }.get(state.reason, ("cherry.dl_wait_reservation", "Waiting for another transfer to release capacity"))
        reason = self.tr(key, default)
        reservation_wait = state.reason == "quota_bytes" and any(
            s.phase == "downloading" for s in self._worker_states.values()
        )
        if state.until is None or reservation_wait:
            return self.tr(
                "cherry.dl_wait_no_time", "{reason} · rechecks when another transfer finishes",
            ).format(reason=reason)
        remaining = max(0, math.ceil(state.until - time.time()))
        if not remaining:
            return self.tr("cherry.dl_wait_rechecking", "{reason} · rechecking capacity…").format(reason=reason)
        return self.tr(
            "cherry.dl_wait_timed", "{reason} · rechecks in {duration} (at {at})",
        ).format(reason=reason, duration=self._format_duration(remaining),
                 at=time.strftime("%H:%M:%S", time.localtime(state.until)))

    def _render_worker_states(self) -> None:
        if not self._worker_states:
            return
        active = [s for s in self._worker_states.values() if s.phase == "downloading"]
        waiting = [s for s in self._worker_states.values() if s.phase == "waiting"]
        for state in active + waiting:
            if state.slot >= len(self._rows):
                continue
            if state.phase == "waiting":
                text = self._wait_description(state)
            else:
                age = max(0, int(time.time() - self._worker_activity.get(state.slot, time.time())))
                text = self.tr("cherry.dl_transfer_active", "Transfer active")
                if age >= 10:
                    text += self.tr("cherry.dl_transfer_silent", " · no new bytes for {duration}").format(
                        duration=self._format_duration(age))
            self._rows[state.slot].status(text)
        if active:
            text = self.tr(
                "cherry.dl_transfer_summary", "Transferring: {active} active · {waiting} waiting for capacity",
            ).format(active=len(active), waiting=len(waiting))
            silent = max(0, int(time.time() - max(
                self._worker_activity.get(s.slot, time.time()) for s in active
            )))
            if silent >= 10:
                text += self.tr("cherry.dl_transfer_silent", " · no new bytes for {duration}").format(
                    duration=self._format_duration(silent))
        elif waiting and not any(s.phase == "preparing" for s in self._worker_states.values()):
            timed = [s for s in waiting if s.until is not None]
            state = min(timed, key=lambda s: s.until) if timed else waiting[0]
            text = "⏸ " + self._wait_description(state)
        else:
            text = self.tr("cherry.dl_transfer_preparing", "Preparing the next transfers…")
        self._phase.setText(text)

    def _on_worker_done(self, slot: int, filename: str, icon: str) -> None:
        if slot < len(self._rows):
            self._rows[slot].done(filename, icon)

    def _on_worker_idle(self, slot: int) -> None:
        if slot < len(self._rows):
            self._rows[slot].idle()

    def _on_source_done(self, url_id: int, file_count: int) -> None:
        text = self.tr(
            "cherry.dl_source_done", "Source finished · {n} files in catalog"
        ).format(n=file_count)
        self.set_phase(text)
        self.append_log(text, "dim")

    def _on_site_blocked(self, site: str, streak: int, detail: str) -> None:
        """El sitio cerró la puerta. Se dice fuerte y se dice entero: lo que el
        servidor haya escrito en el cuerpo del rechazo es la única pista de por
        qué, y antes se descartaba sin leerlo."""
        if streak:
            self.set_phase(self.tr(
                "cherry.dl_site_blocked", "⛔ {site} refused {n} times — abandoned"
            ).format(site=site, n=streak))
            self.append_log(self.tr(
                "cherry.dl_site_blocked_log",
                "⛔ {site} refused {n} downloads in a row. The site is abandoned "
                "for this run; the files stay queued for another day."
            ).format(site=site, n=streak), "error")
        else:
            # streak 0 = no nos rechazaron: paramos nosotros (presupuesto).
            self.set_phase(self.tr(
                "cherry.dl_site_budget", "⏸ {site} — daily budget spent"
            ).format(site=site))
        if detail:
            self.append_log(detail, "warn")

    # ── Enfriamiento ──────────────────────────────────────────────────────
    def _on_cooldown(self, seconds: float) -> None:
        self._stop_cooldown()
        if seconds <= 0:
            self._phase.setText(self.tr("cherry.dl_transfer_preparing", "Preparing the next transfers…"))
            return
        self._cooldown_until = time.time() + seconds
        self._tick_cooldown()
        self._cooldown_timer.start()

    def _tick_cooldown(self) -> None:
        self._cooldown_left = max(0, math.ceil(self._cooldown_until - time.time()))
        if self._cooldown_left <= 0:
            self._stop_cooldown()
            self._phase.setText(self.tr("cherry.dl_transfer_preparing", "Preparing the next transfers…"))
            return
        self._phase.setText(self.tr(
            "cherry.dl_cooldown", "⏸ Pause after scanning · {s}s").format(s=self._cooldown_left))

    def _stop_cooldown(self) -> None:
        self._cooldown_timer.stop()
        self._cooldown_left = 0
        self._cooldown_until = 0.0
