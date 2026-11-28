"""Distintivo del tipo de archivo: «🖼 PNG», «📦 ZIP»… con color por categoría.

El color lo pone el tema (``QLabel[role="badge"][kind="image|animated|archive|other"]``),
así que el módulo no pinta nada a mano (AGENTS regla 9).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from core.components.ui_roles import set_role
from core.file_kinds import ANIMATED, ARCHIVE, IMAGE, file_kind

_TIPS = {
    IMAGE: ('badge.image', '{label} image'),
    ANIMATED: ('badge.animated', '{label} image (may be animated; only the first frame is shown)'),
    ARCHIVE: ('badge.archive', '{label} archive: the preview shows its first image'),
}


class FileBadge(QLabel):
    def __init__(self, tr=None, parent=None):
        super().__init__(parent)
        self._tr = tr or (lambda key, default: default)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setTextFormat(Qt.TextFormat.PlainText)
        set_role(self, 'badge')
        self.set_path(None)

    def set_path(self, path) -> None:
        if path is None:
            self.setText('')
            self.setToolTip('')
            self.hide()
            return
        icon, label, kind = file_kind(path)
        self.setText(f'{icon} {label}')
        key, default = _TIPS.get(kind, ('badge.other', '{label} file'))
        self.setToolTip(self._tr(key, default).format(label=label))
        if self.property('kind') != kind:
            self.setProperty('kind', kind)
            set_role(self, 'badge')  # re-pulir para que el QSS tome el nuevo kind
        self.show()
