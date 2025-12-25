from PySide6.QtWidgets import (QWidget, QLabel, QPushButton, QHBoxLayout, QFrame, QLayout, QSizePolicy)
from PySide6.QtCore import Qt, QRect, QPoint, QSize, Signal
import random

class FlowLayout(QLayout):
    """Standard Qt FlowLayout implementation for wrapping widgets."""
    def __init__(self, parent=None, margin=0, h_spacing=5, v_spacing=5):
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        self._item_list = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing

    def __del__(self):
        item = self.takeAt(0)
        while item:
            item = self.takeAt(0)

    def addItem(self, item):
        self._item_list.append(item)

    def count(self):
        return len(self._item_list)

    def itemAt(self, index):
        if 0 <= index < len(self._item_list):
            return self._item_list[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._item_list):
            return self._item_list.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        height = self._do_layout(QRect(0, 0, width, 0), True)
        return height

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._item_list:
            size = size.expandedTo(item.minimumSize())
        size += QSize(2 * self.contentsMargins().top(), 2 * self.contentsMargins().top())
        return size

    def _do_layout(self, rect, test_only):
        x, y = rect.x(), rect.y()
        line_height = 0
        
        for item in self._item_list:
            wid = item.widget()
            space_x = self._h_spacing
            space_y = self._v_spacing
            
            next_x = x + item.sizeHint().width() + space_x
            
            if next_x - space_x > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + space_y
                next_x = x + item.sizeHint().width() + space_x
                line_height = 0
                
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))
                
            x = next_x
            line_height = max(line_height, item.sizeHint().height())
            
        return y + line_height - rect.y()

class TagChip(QFrame):
    """A visual chip representing a single tag with a delete button."""
    removed = Signal(str) # Emits label text on remove
    
    def __init__(self, text, color_Index=0):
        super().__init__()
        self.text = text
        self.setFrameShape(QFrame.StyledPanel)
        self.setFrameShadow(QFrame.Raised)
        
        # Generate a distinct color based on index or hash
        colors = ["#FF5555", "#BD93F9", "#FF79C6", "#8BE9FD", "#50FA7B", "#F1FA8C", "#FFB86C"]
        bg_color = colors[color_Index % len(colors)]
        
        self.setStyleSheet(f"""
            TagChip {{
                background-color: {bg_color};
                border-radius: 12px;
                padding: 2px;
            }}
            QLabel {{
                color: #282a36; 
                font-weight: bold;
                border: none;
                background: transparent;
            }}
            QPushButton {{
                background-color: transparent;
                color: #444;
                border: none;
                font-weight: bold;
                border-radius: 8px;
            }}
            QPushButton:hover {{
                background-color: rgba(255,255,255,0.4);
                color: #000;
            }}
        """)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 2, 5, 2)
        layout.setSpacing(5)
        
        lbl = QLabel(text)
        layout.addWidget(lbl)
        
        btn_close = QPushButton("✕")
        btn_close.setFixedSize(16, 16)
        btn_close.clicked.connect(self.on_remove)
        layout.addWidget(btn_close)
        
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def on_remove(self):
        self.removed.emit(self.text)
        self.deleteLater()
