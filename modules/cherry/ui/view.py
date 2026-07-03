"""
Vista del módulo Cherry-DL (fase transicional del wrapper).

Lanza la GUI de cherry-dl vendorizada (`third_party/cherry_dl/`) como proceso
hijo sobre el MISMO venv de Panopticon (sys.executable -m cherry_dl gui, con
PYTHONPATH=third_party). No hay kill-button a propósito: matar un downloader a
media descarga puede dejar colas a medias; la ventana de cherry-dl se cierra
desde su propia UI. stdout/stderr del hijo van a logs/cherry_dl_gui.log.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

from core.paths import ProjectPaths

_LOG_NAME = "cherry_dl_gui.log"


class CherryView(QWidget):
    def __init__(self, context=None):
        super().__init__()
        self.context = context
        self.process: QProcess | None = None
        self._build_ui()

    # ── Locales ──────────────────────────────────────────────────────────
    def tr(self, key: str, default: str | None = None) -> str:
        if self.context and "locale_manager" in self.context:
            return self.context["locale_manager"].tr(key, default)
        return default if default else key

    # ── UI ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        card = QFrame()
        card.setObjectName("cherryCard")
        card.setMaximumWidth(520)
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(14)
        card_layout.setContentsMargins(32, 32, 32, 32)

        icon = QLabel("🍒")
        icon.setStyleSheet("font-size: 48px;")
        icon.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(icon)

        title = QLabel(self.tr("cherry.title", "Cherry-DL"))
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        title.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(title)

        subtitle = QLabel(self.tr(
            "cherry.subtitle",
            "Mass downloader for artist collections (Kemono / Patreon / Pixiv)."
        ))
        subtitle.setWordWrap(True)
        subtitle.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(subtitle)

        self.status_label = QLabel(self.tr("cherry.status_idle", "Not running"))
        self.status_label.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(self.status_label)

        self.launch_btn = QPushButton(self.tr("cherry.launch", "Launch Cherry-DL"))
        self.launch_btn.setMinimumHeight(40)
        self.launch_btn.clicked.connect(self._launch)
        card_layout.addWidget(self.launch_btn)

        note = QLabel(self.tr(
            "cherry.note",
            "Opens in its own window. Profiles and settings live in ~/.cherry-dl "
            "and are shared with the standalone cherry-dl."
        ))
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignCenter)
        note.setStyleSheet("font-size: 11px; opacity: 0.7;")
        card_layout.addWidget(note)

        layout.addWidget(card)

    # ── Proceso ──────────────────────────────────────────────────────────
    def _launch(self):
        if self.process is not None:
            return  # guard anti doble-inicio (patrón librarian:706)

        root = ProjectPaths.root()
        third_party = str(root / "third_party")

        env = QProcessEnvironment.systemEnvironment()
        prev = env.value("PYTHONPATH", "")
        env.insert("PYTHONPATH", third_party + (os.pathsep + prev if prev else ""))

        log_dir = root / "logs"
        log_dir.mkdir(exist_ok=True)

        proc = QProcess(self)
        proc.setProcessEnvironment(env)
        proc.setWorkingDirectory(str(root))
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.setStandardOutputFile(str(log_dir / _LOG_NAME))
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)

        proc.start(sys.executable, ["-m", "cherry_dl", "gui"])
        self.process = proc

        self.launch_btn.setEnabled(False)
        self.status_label.setText(self.tr("cherry.status_running", "Running in its own window…"))

    def _on_finished(self, exit_code: int, _status):
        self._reset()
        if exit_code == 0:
            self.status_label.setText(self.tr("cherry.status_idle", "Not running"))
        else:
            msg = self.tr("cherry.status_exit_error", "Exited with error (code {code}) — see logs/{log}")
            self.status_label.setText(msg.format(code=exit_code, log=_LOG_NAME))

    def _on_error(self, _error):
        # errorOccurred puede dispararse junto a finished; solo importa si aún
        # creemos que el proceso vive (p.ej. FailedToStart).
        if self.process is None:
            return
        self._reset()
        msg = self.tr("cherry.status_launch_failed", "Failed to start — see logs/{log}")
        self.status_label.setText(msg.format(log=_LOG_NAME))

    def _reset(self):
        if self.process is not None:
            self.process.deleteLater()
            self.process = None
        self.launch_btn.setEnabled(True)
