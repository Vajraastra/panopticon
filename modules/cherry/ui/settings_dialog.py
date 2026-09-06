"""
Ajustes de cherry-dl (A5).

Incluye carpeta, workers, timeout y un límite diario global opcional. Los frenos
de red (`delay_min`, reintentos, `stall_timeout`) y los workers por template
siguen en el TOML a mano, porque son lo que evita que el motor martillee un
sitio hasta que lo bloqueen.

Lee y escribe `~/.cherry-dl/config.toml`, compartido con el standalone. Cambiar
`download_dir` **no mueve nada**: los proyectos que ya existen conservan la ruta
que se les grabó al crearlos, y el ajuste solo decide dónde nacen los nuevos.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from ..logic import settings as settings_logic


class SettingsDialog(QDialog):
    """Editor de los ajustes del motor. Guarda al aceptar, nunca antes."""

    def __init__(self, theme=None, locale=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._lm = locale
        self.setWindowTitle(self.tr("cherry.settings_title", "Cherry-DL settings"))
        self.setMinimumWidth(560)

        current = settings_logic.load()

        lay = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        dir_row = QHBoxLayout()
        self._dir = QLineEdit(current["download_dir"])
        dir_row.addWidget(self._dir, 1)
        btn_browse = QPushButton(self.tr("cherry.settings_browse", "Browse…"))
        btn_browse.clicked.connect(self._browse)
        dir_row.addWidget(btn_browse)
        holder = QWidget()
        holder.setLayout(dir_row)
        form.addRow(self.tr("cherry.settings_dir", "Download folder"), holder)

        self._workers = QSpinBox()
        self._workers.setRange(1, 16)
        self._workers.setValue(current["workers"])
        self._workers.setToolTip(self.tr(
            "cherry.settings_workers_tip",
            "Default only: each site's template caps it, and the download view "
            "can ask for a different number.",
        ))
        form.addRow(self.tr("cherry.settings_workers", "Workers"), self._workers)

        self._timeout = QSpinBox()
        self._timeout.setRange(5, 600)
        self._timeout.setSuffix(" s")
        self._timeout.setValue(current["timeout"])
        form.addRow(self.tr("cherry.settings_timeout", "Timeout"), self._timeout)

        limit_row = QHBoxLayout()
        self._daily_limit_enabled = QCheckBox(self.tr(
            "cherry.settings_daily_limit_enabled", "Enable",
        ))
        self._daily_limit_enabled.setChecked(current["daily_limit_enabled"])
        self._daily_limit_gb = QDoubleSpinBox()
        self._daily_limit_gb.setRange(0.1, 100000.0)
        self._daily_limit_gb.setDecimals(1)
        self._daily_limit_gb.setSingleStep(10.0)
        self._daily_limit_gb.setSuffix(" GB")
        self._daily_limit_gb.setValue(current["daily_limit_gb"])
        self._daily_limit_gb.setEnabled(current["daily_limit_enabled"])
        self._daily_limit_enabled.toggled.connect(self._daily_limit_gb.setEnabled)
        limit_tip = self.tr(
            "cherry.settings_daily_limit_tip",
            "Combined total across all servers. Files already downloading finish, "
            "so the final total may be slightly higher.",
        )
        self._daily_limit_enabled.setToolTip(limit_tip)
        self._daily_limit_gb.setToolTip(limit_tip)
        limit_row.addWidget(self._daily_limit_enabled)
        limit_row.addWidget(self._daily_limit_gb)
        limit_row.addStretch()
        limit_holder = QWidget()
        limit_holder.setLayout(limit_row)
        form.addRow(self.tr(
            "cherry.settings_daily_limit", "Daily download limit",
        ), limit_holder)

        lay.addLayout(form)

        note = QLabel(self.tr(
            "cherry.settings_note",
            "Changing the download folder does not move anything: existing projects "
            "keep the path they were created with, and only new ones land here. "
            "Saved to {file}, shared with the standalone cherry-dl.",
        ).format(file=settings_logic.config_file()))
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {self._color('text_dim', '#888')}; font-size: 11px;")
        lay.addWidget(note)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet(
            f"color: {self._color('accent_warning', '#ff5555')}; font-size: 11px;")
        lay.addWidget(self._error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    # ── helpers ───────────────────────────────────────────────────────────
    def tr(self, key: str, default: str) -> str:
        return self._lm.tr(key, default) if self._lm else default

    def _color(self, key: str, fallback: str) -> str:
        return self._theme.get_color(key) if self._theme else fallback

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, self.tr("cherry.settings_dir", "Download folder"), self._dir.text())
        if chosen:
            self._dir.setText(chosen)

    def _save(self) -> None:
        """Guarda si la carpeta es usable. No la crea: una ruta mal escrita
        crearía un árbol nuevo en silencio y los proyectos caerían ahí."""
        folder = self._dir.text().strip()
        if not folder:
            self._error.setText(self.tr(
                "cherry.settings_dir_empty", "The download folder cannot be empty."))
            return
        if not Path(folder).is_dir():
            self._error.setText(self.tr(
                "cherry.settings_dir_missing",
                "“{f}” does not exist. Create it first, or pick another one.",
            ).format(f=folder))
            return
        try:
            settings_logic.save(
                folder,
                self._workers.value(),
                self._timeout.value(),
                self._daily_limit_enabled.isChecked(),
                self._daily_limit_gb.value(),
            )
        except Exception as exc:
            self._error.setText(self.tr(
                "cherry.settings_failed", "Could not save: {msg}",
            ).format(msg=f"{type(exc).__name__}: {exc}"))
            return
        self.accept()
