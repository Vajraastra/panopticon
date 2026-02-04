from PySide6.QtWidgets import QFrame, QVBoxLayout, QLabel
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QMouseEvent, QPixmap

# Reuse the robust thumbnail from Librarian
from modules.librarian.module import ClickableThumbnail

class FolderCard(QFrame):
    """
    Visual representation of an Album/Folder.
    """
    clicked = Signal(str)
    
    def __init__(self, path, name, cover_path=None, count=0):
        super().__init__()
        self.path = path
        self.cover_path = cover_path
        
        # Styling
        self.setFixedSize(160, 210)
        self.setStyleSheet("""
            FolderCard {
                background-color: #222;
                border-radius: 12px;
                border: 1px solid #333;
            }
            FolderCard:hover {
                background-color: #2a2a2a;
                border: 1px solid #00ffcc;
            }
        """)
        self.setCursor(Qt.PointingHandCursor)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Cover Image
        self.lbl_cover = QLabel()
        self.lbl_cover.setFixedSize(140, 130)
        self.lbl_cover.setStyleSheet("background-color: #111; border-radius: 6px;")
        self.lbl_cover.setAlignment(Qt.AlignCenter)
        
        if not cover_path:
            self.lbl_cover.setText("📂")
        else:
            self.lbl_cover.setText("⏳")
            
        layout.addWidget(self.lbl_cover)
        
        # Name
        self.lbl_name = QLabel(name)
        self.lbl_name.setStyleSheet("color: white; font-weight: bold; font-size: 13px; border: none; background: transparent;")
        self.lbl_name.setAlignment(Qt.AlignCenter)
        # Elide text manually if needed or rely on layout
        layout.addWidget(self.lbl_name)
        
        # Count
        self.lbl_count = QLabel(f"{count} items")
        self.lbl_count.setStyleSheet("color: #777; font-size: 11px; border: none; background: transparent;")
        self.lbl_count.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_count)
    
    def set_cover_image(self, pixmap):
        """Sets the pixmap for the cover."""
        if pixmap and not pixmap.isNull():
            self.lbl_cover.setPixmap(pixmap.scaled(140, 130, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.lbl_cover.setText("🚫")

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.path)
