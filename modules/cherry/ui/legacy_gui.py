"""
Salida de emergencia transicional: lanza la GUI qasync de cherry-dl aparte.

Era la vista entera del módulo antes de A2; sobrevive como un botón mientras la
UI nativa no cubra la descarga (A3). Corre sobre el MISMO venv de Panopticon
(`sys.executable -m cherry_dl gui`, con `PYTHONPATH=third_party`) y escribe su
salida a `logs/cherry_dl_gui.log`.

Sin botón de matar, a propósito: cortar un downloader a media descarga puede
dejar colas a medias. La ventana de cherry-dl se cierra desde su propia UI.
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from core.paths import ProjectPaths

LOG_NAME = "cherry_dl_gui.log"


class LegacyGuiLauncher(QObject):
    """Un proceso hijo a la vez; informa su estado por `state_changed`."""

    # (corriendo, clave de locale, valor a formatear en la clave)
    state_changed = Signal(bool, str, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process: QProcess | None = None

    @property
    def is_running(self) -> bool:
        return self._process is not None

    def launch(self) -> bool:
        if self._process is not None:
            return False  # guard anti doble-inicio

        root = ProjectPaths.root()
        env = QProcessEnvironment.systemEnvironment()
        prev = env.value("PYTHONPATH", "")
        third_party = str(root / "third_party")
        env.insert("PYTHONPATH", third_party + (os.pathsep + prev if prev else ""))

        log_dir = root / "logs"
        log_dir.mkdir(exist_ok=True)

        proc = QProcess(self)
        proc.setProcessEnvironment(env)
        proc.setWorkingDirectory(str(root))
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.setStandardOutputFile(str(log_dir / LOG_NAME))
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        proc.start(sys.executable, ["-m", "cherry_dl", "gui"])
        self._process = proc

        self.state_changed.emit(True, "cherry.status_running", None)
        return True

    def _on_finished(self, exit_code: int, _status) -> None:
        self._reset()
        if exit_code == 0:
            self.state_changed.emit(False, "cherry.status_idle", None)
        else:
            self.state_changed.emit(False, "cherry.status_exit_error", exit_code)

    def _on_error(self, _error) -> None:
        # `errorOccurred` puede llegar junto a `finished`; solo importa si aún
        # creemos que el proceso vive (p.ej. FailedToStart).
        if self._process is None:
            return
        self._reset()
        self.state_changed.emit(False, "cherry.status_launch_failed", None)

    def _reset(self) -> None:
        if self._process is not None:
            self._process.deleteLater()
            self._process = None
