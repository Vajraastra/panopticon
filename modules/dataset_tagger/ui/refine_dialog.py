"""
Diálogo de propuesta de la sugerencia de prosa (Fase 2).

Muestra el caption original (solo lectura) y la versión sugerida por el LLM
(editable, por si el usuario quiere afinarla antes de aceptar). La sugerencia
NUNCA se aplica sola: el usuario decide con Aceptar / Rechazar.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton)


class RefineProposalDialog(QDialog):
    def __init__(self, original, suggested, tr=None, parent=None):
        super().__init__(parent)
        self._tr = tr or (lambda k, d=None: d if d is not None else k)
        self.setWindowTitle(self._tr("review.suggest.title", "Suggested caption"))
        self.resize(620, 460)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(self._tr("review.suggest.original", "Original:")))
        orig = QPlainTextEdit(original)
        orig.setReadOnly(True)
        orig.setMaximumHeight(140)
        lay.addWidget(orig)

        lay.addWidget(QLabel(self._tr("review.suggest.proposed",
                                      "Suggested (editable):")))
        self.edit = QPlainTextEdit(suggested)
        lay.addWidget(self.edit, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        btn_reject = QPushButton(self._tr("review.suggest.reject", "Reject"))
        btn_reject.clicked.connect(self.reject)
        btn_accept = QPushButton(self._tr("review.suggest.accept", "Accept"))
        btn_accept.setDefault(True)
        btn_accept.clicked.connect(self.accept)
        btns.addWidget(btn_reject, 0)
        btns.addWidget(btn_accept, 0)
        lay.addLayout(btns)

    def result_text(self):
        """Texto final (puede haber sido editado por el usuario)."""
        return self.edit.toPlainText().strip()
