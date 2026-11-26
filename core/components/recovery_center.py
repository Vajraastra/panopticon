"""Centro de recuperación: una sola puerta a las operaciones interrumpidas (UI_DESIGN U2).

Solo reúne entradas; no contiene lógica de recuperación. Cada entrada abre el diálogo o
modo que ya existía en su dueño (core o módulo), con sus propias reglas de revisión y
confirmación (DECISIONES.md). Los módulos declaran sus entradas con
`BaseModule.recovery_entries()`; MainWindow las recoge sin importar ningún módulo.
"""
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from core.components.ui_roles import hint, set_role


@dataclass(frozen=True)
class RecoveryEntry:
    key: str
    title: str
    description: str
    open: Callable[[], None]
    owner: str = ""  # nombre visible de la herramienta dueña ("" = Panopticon)


class RecoveryCenterDialog(QDialog):
    def __init__(self, entries, tr, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("recovery_center.title", "Recovery center"))
        self.setModal(False)
        self.resize(640, 520)
        self.entries = list(entries)
        self.buttons = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        title = set_role(QLabel(tr("recovery_center.title", "Recovery center")), "tool_title")
        layout.addWidget(title)
        layout.addWidget(hint(tr(
            "recovery_center.intro",
            "Interrupted operations from every tool, in one place. Opening an entry only shows "
            "its review; nothing is restored, moved or deleted without your confirmation there.")))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        rows = QVBoxLayout(body)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(8)
        for entry in self.entries:
            rows.addWidget(self._row(entry, tr))
        rows.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        close = QPushButton(tr("common.close", "Close"))
        close.setToolTip(tr("recovery_center.close_tip", "Close this list. Reviews you opened stay open."))
        close.clicked.connect(self.close)
        footer = QHBoxLayout()
        footer.addStretch()
        footer.addWidget(close)
        layout.addLayout(footer)

    def _row(self, entry, tr):
        card = set_role(QFrame(), "card")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 10)
        text = QVBoxLayout()
        name = QLabel(f"{entry.title}" + (f"  ·  {entry.owner}" if entry.owner else ""))
        name.setStyleSheet("font-weight: bold; background: transparent; border: none;")
        text.addWidget(name)
        desc = hint(entry.description)
        desc.setStyleSheet("border: none;")
        text.addWidget(desc)
        lay.addLayout(text, 1)
        button = QPushButton(tr("recovery_center.open", "Review…"))
        button.setToolTip(tr("recovery_center.open_tip",
                             "Open this review. Nothing changes until you confirm an action there."))
        # `clicked` entrega un bool: no dejar que pise los parámetros del callback.
        button.clicked.connect(lambda _checked=False, entry=entry: entry.open())
        lay.addWidget(button, 0, Qt.AlignVCenter)
        self.buttons[entry.key] = button
        return card
