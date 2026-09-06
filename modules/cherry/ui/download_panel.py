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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QVBoxLayout, QWidget,
)

_MAX_LOG_LINES = 800


class _WorkerRow(QWidget):
    """Una ranura del pool: archivo en curso y su barra."""

    def __init__(self, slot: int, dim: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self._num = QLabel(f"{slot + 1}")
        self._num.setFixedWidth(18)
        self._num.setStyleSheet(f"color: {dim}; font-size: 11px;")
        lay.addWidget(self._num)

        self._name = QLabel("—")
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


class DownloadPanel(QWidget):
    """Estado en vivo de la descarga. `bind()` la engancha al puente y la cola."""

    def __init__(self, theme=None, locale=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._lm = locale
        self._rows: list[_WorkerRow] = []
        self._cooldown_left = 0

        self._build_ui()

        self._cooldown_timer = QTimer(self)
        self._cooldown_timer.setInterval(1000)
        self._cooldown_timer.timeout.connect(self._tick_cooldown)

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
        lay.addLayout(top)

        self._phase = QLabel(self.tr("cherry.dl_idle", "Idle"))
        self._phase.setStyleSheet(f"color: {self._color('text_secondary', '#ccc')}; font-size: 12px;")
        lay.addWidget(self._phase)

        self._counters = QLabel("")
        self._counters.setStyleSheet("font-size: 13px;")
        lay.addWidget(self._counters)

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
        bridge.scan_progress.connect(self._on_scan_progress)
        bridge.scan_complete.connect(self._on_scan_complete)
        bridge.worker_start.connect(self._on_worker_start)
        bridge.worker_progress.connect(self._on_worker_progress)
        bridge.worker_done.connect(self._on_worker_done)
        bridge.worker_idle.connect(self._on_worker_idle)
        bridge.counters.connect(self._on_counters)
        bridge.source_done.connect(self._on_source_done)
        bridge.site_blocked.connect(self._on_site_blocked)
        if queue is not None:
            queue.profile_started.connect(self._on_profile_started)

    # ── API de la vista ───────────────────────────────────────────────────
    def reset(self, title: str = "") -> None:
        self._title.setText(title or "—")
        self._position.setText("")
        self._log.clear()
        self._reset_counters()
        self._stop_cooldown()
        self.set_phase(self.tr("cherry.dl_starting", "Starting…"))
        for row in self._rows:
            row.idle()

    def set_phase(self, text: str) -> None:
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
        self._log.appendHtml(f'<span style="color:{color}">{safe}</span>')

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
        self._stop_cooldown()
        for row in self._rows:
            row.idle()

    def _on_source_started(self, url: str, artist: str, site: str) -> None:
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

    def _on_worker_start(self, slot: int, filename: str) -> None:
        self._ensure_slots(slot + 1)
        self._rows[slot].start(filename)

    def _on_worker_progress(self, slot: int, done: int, total: int) -> None:
        if slot < len(self._rows):
            self._rows[slot].progress(done, total)

    def _on_worker_done(self, slot: int, filename: str, icon: str) -> None:
        if slot < len(self._rows):
            self._rows[slot].done(filename, icon)

    def _on_worker_idle(self, slot: int) -> None:
        if slot < len(self._rows):
            self._rows[slot].idle()

    def _on_source_done(self, url_id: int, file_count: int) -> None:
        self.append_log(self.tr(
            "cherry.dl_source_done", "Source finished · {n} files in catalog"
        ).format(n=file_count), "dim")

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
        self._cooldown_left = int(round(seconds))
        self._tick_cooldown()
        self._cooldown_timer.start()

    def _tick_cooldown(self) -> None:
        if self._cooldown_left <= 0:
            self._stop_cooldown()
            return
        self.set_phase(self.tr(
            "cherry.dl_cooldown", "⏸ Cooling down · {s}s").format(s=self._cooldown_left))
        self._cooldown_left -= 1

    def _stop_cooldown(self) -> None:
        self._cooldown_timer.stop()
        self._cooldown_left = 0
