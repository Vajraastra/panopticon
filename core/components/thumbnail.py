"""
Miniatura clicable reutilizable (rating overlay, selección, click).

Widget compartido en `core/` para que cualquier módulo lo use sin importar a
otro módulo (Regla de Oro #3). Originalmente vivía en Librarian; lo usan
Librarian y Gallery.
"""
import os

from PySide6.QtWidgets import QLabel
from PySide6.QtGui import QPixmap, QMouseEvent
from PySide6.QtCore import Qt, Signal


class ClickableThumbnail(QLabel):
    clicked = Signal(str)
    selection_changed = Signal(bool)
    rating_changed = Signal(str, int)

    def __init__(self, path, parent=None, auto_load=True, rating=0,
                 border_accent='#00ffcc', border_default='#555', bg='#000000'):
        super().__init__(parent)
        # Ensure path matches DB/Loader format (forward slashes)
        self.path = os.path.normpath(path).replace('\\', '/')
        self.selected = False
        self.rating = rating
        self.border_accent = border_accent
        self.border_default = border_default
        self.bg = bg
        self.setFixedSize(100, 100)
        self.setStyleSheet(f"border: 1px solid {border_default}; border-radius: 5px; background-color: {bg};")
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)

        # Rating Label (Overlay)
        self.lbl_rating = QLabel(self)
        self.lbl_rating.setFixedSize(30, 20)
        self.lbl_rating.setAlignment(Qt.AlignCenter)
        self.lbl_rating.setStyleSheet("""
            background-color: rgba(0, 0, 0, 150);
            color: #ffcc00;
            font-weight: bold;
            font-size: 10px;
            border-bottom-left-radius: 5px;
        """)
        # Position in top-right
        self.lbl_rating.move(70, 0)
        self.update_rating_display()

        if auto_load:
            pix = QPixmap(path)
            if not pix.isNull():
                self.setPixmap(pix.scaled(100, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self.setText("❌")
        else:
            self.setText("⏳") # Placeholder

    def update_rating_display(self):
        if self.rating > 0:
            self.lbl_rating.setText(f"⭐{self.rating}")
            self.lbl_rating.show()
        else:
            self.lbl_rating.hide()

    def setRating(self, rating):
        self.rating = rating
        self.update_rating_display()

    def cycleRating(self):
        self.rating = (self.rating + 1) % 6 # 0,1,2,3,4,5 then back to 0
        self.update_rating_display()
        self.rating_changed.emit(self.path, self.rating)

    def setSelected(self, selected):
        self.selected = selected
        if self.selected:
            self.setStyleSheet(f"border: 3px solid {self.border_accent}; border-radius: 5px; background-color: {self.bg};")
        else:
            self.setStyleSheet(f"border: 1px solid {self.border_default}; border-radius: 5px; background-color: {self.bg};")
        self.selection_changed.emit(self.selected)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            if event.modifiers() & Qt.ControlModifier:
                self.cycleRating()
            else:
                self.clicked.emit(self.path)
