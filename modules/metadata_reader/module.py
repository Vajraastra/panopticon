from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QLabel, QFileDialog, QTextEdit, QFrame, QSizePolicy, QSplitter)
from PySide6.QtGui import QPixmap, QImage, QKeyEvent
from PySide6.QtCore import Qt, QSize, Signal
import os

from core.base_module import BaseModule
from .logic.parser import UniversalParser

class ResponsiveImageLabel(QLabel):
    """A QLabel that automatically scales its pixmap to fit its size and handles drops."""
    dropped_files = Signal(list)

    def __init__(self, text="Drop images or folders here\n(or click 'Open')"):
        super().__init__(text)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("""
            QLabel {
                border: 2px dashed #444; 
                color: #888; 
                background-color: #0a0a0a;
                font-size: 16px;
                border-radius: 10px;
            }
        """)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(300, 300)
        self.setAcceptDrops(True)
        self._pixmap = None

    def set_image(self, pixmap):
        self._pixmap = pixmap
        self.update_pixmap()

    def update_pixmap(self):
        if self._pixmap and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            super().setPixmap(scaled)
        else:
            self.setPixmap(QPixmap()) 

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_pixmap()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            self.setStyleSheet("""
                QLabel {
                    border: 2px dashed #00ffcc; 
                    color: #00ffcc; 
                    background-color: #111;
                    font-size: 16px;
                    border-radius: 10px;
                }
            """)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet("""
            QLabel {
                border: 2px dashed #444; 
                color: #888; 
                background-color: #0a0a0a;
                font-size: 16px;
                border-radius: 10px;
            }
        """)

    def dropEvent(self, event):
        self.dragLeaveEvent(None)
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        self.dropped_files.emit(files)

class MetadataView(QWidget):
    """Custom widget for the metadata module to handle keyboard events."""
    def __init__(self, module):
        super().__init__()
        self.module = module
        self.setFocusPolicy(Qt.StrongFocus)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Left:
            self.module.prev_image()
        elif event.key() == Qt.Key_Right:
            self.module.next_image()
        else:
            super().keyPressEvent(event)

class MetadataModule(BaseModule):
    def __init__(self):
        super().__init__()
        self.view = None
        self.image_list = []
        self.current_index = -1

    @property
    def name(self):
        return "Metadata Reader"

    @property
    def icon(self):
        return "📋"

    @property
    def description(self):
        return "Batch read and analyze metadata from AI-generated images."

    def get_view(self) -> QWidget:
        if self.view: return self.view
        
        self.view = MetadataView(self)
        self.view.setAcceptDrops(True) # Allow drops on the whole view too
        main_layout = QHBoxLayout(self.view)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        self.splitter = QSplitter(Qt.Horizontal)
        
        # --- LEFT PANEL: Image and Selection ---
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(10, 10, 10, 10)
        
        # Action Buttons
        btn_layout = QHBoxLayout()
        self.btn_open = QPushButton("📂 Open Image(s)")
        self.btn_open.clicked.connect(self.open_images)
        self.btn_open.setStyleSheet("""
            QPushButton {
                background-color: #00ffcc; 
                color: black; 
                font-weight: bold; 
                padding: 12px; 
                font-size: 14px;
                border-radius: 8px;
            }
            QPushButton:hover {
                background-color: #00e6b8;
            }
        """)
        
        self.btn_folder = QPushButton("📁 Open Folder")
        self.btn_folder.clicked.connect(self.open_folder)
        self.btn_folder.setStyleSheet("""
            QPushButton {
                background-color: #333; 
                color: white; 
                padding: 12px; 
                font-size: 14px; 
                border: 1px solid #444;
                border-radius: 8px;
            }
            QPushButton:hover {
                background-color: #444;
            }
        """)
        
        btn_layout.addWidget(self.btn_open)
        btn_layout.addWidget(self.btn_folder)
        left_layout.addLayout(btn_layout)
        
        # Carousel Controls
        self.carousel_layout = QHBoxLayout()
        self.btn_prev = QPushButton("◀ Previous")
        self.btn_prev.clicked.connect(self.prev_image)
        self.btn_prev.setStyleSheet("padding: 8px; font-weight: bold; border-radius: 5px; background-color: #222; color: white;")
        
        self.index_label = QLabel("0 / 0")
        self.index_label.setAlignment(Qt.AlignCenter)
        self.index_label.setStyleSheet("color: #00ffcc; font-size: 15px; font-weight: bold; min-width: 100px;")
        
        self.btn_next = QPushButton("Next ▶")
        self.btn_next.clicked.connect(self.next_image)
        self.btn_next.setStyleSheet("padding: 8px; font-weight: bold; border-radius: 5px; background-color: #222; color: white;")
        
        self.carousel_layout.addWidget(self.btn_prev)
        self.carousel_layout.addWidget(self.index_label)
        self.carousel_layout.addWidget(self.btn_next)
        left_layout.addLayout(self.carousel_layout)
        
        self.stats_label = QLabel("ℹ File Info: -")
        self.stats_label.setStyleSheet("color: #888; font-size: 12px;")
        left_layout.addWidget(self.stats_label)
        
        self.image_label = ResponsiveImageLabel()
        self.image_label.dropped_files.connect(self.handle_dropped_files)
        left_layout.addWidget(self.image_label, 1) # Full stretch
        
        self.splitter.addWidget(left_panel)
        
        # --- RIGHT PANEL: Metadata Details ---
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(10, 10, 10, 10)
        
        label_pos = QLabel("✨ Positive Prompt:")
        label_pos.setStyleSheet("font-weight: bold; color: #eee; font-size: 14px;")
        right_layout.addWidget(label_pos)
        
        self.pos_prompt = QTextEdit()
        self.pos_prompt.setPlaceholderText("Positive prompt will appear here...")
        self.pos_prompt.setStyleSheet("""
            QTextEdit {
                background-color: #1a1a1a; 
                color: #aaffaa; 
                border: 1px solid #333; 
                font-size: 15px;
                border-radius: 10px;
                padding: 10px;
            }
        """)
        right_layout.addWidget(self.pos_prompt, 2)
        
        label_neg = QLabel("🚫 Negative Prompt:")
        label_neg.setStyleSheet("font-weight: bold; color: #eee; font-size: 14px;")
        right_layout.addWidget(label_neg)
        
        self.neg_prompt = QTextEdit()
        self.neg_prompt.setPlaceholderText("Negative prompt will appear here...")
        self.neg_prompt.setStyleSheet("""
            QTextEdit {
                background-color: #1a1a1a; 
                color: #ffaaaa; 
                border: 1px solid #333; 
                font-size: 15px;
                border-radius: 10px;
                padding: 10px;
            }
        """)
        right_layout.addWidget(self.neg_prompt, 1)
        
        label_tech = QLabel("⚙️ Technical Details & Tool:")
        label_tech.setStyleSheet("font-weight: bold; color: #eee; font-size: 14px;")
        right_layout.addWidget(label_tech)
        
        self.meta_info = QTextEdit()
        self.meta_info.setReadOnly(True)
        self.meta_info.setStyleSheet("""
            QTextEdit {
                background-color: #111; 
                color: #bbb; 
                font-size: 13px;
                border: 1px solid #222;
                border-radius: 10px;
                padding: 10px;
            }
        """)
        right_layout.addWidget(self.meta_info, 2)
        
        self.splitter.addWidget(right_panel)
        self.splitter.setStretchFactor(0, 3) 
        self.splitter.setStretchFactor(1, 2)
        
        main_layout.addWidget(self.splitter)
        return self.view

    def eventFilter(self, source, event):
        if event.type() == QKeyEvent.KeyPress:
            if event.key() == Qt.Key_Left:
                self.prev_image()
                return True
            elif event.key() == Qt.Key_Right:
                self.next_image()
                return True
        return super().eventFilter(source, event)

    def open_images(self):
        files, _ = QFileDialog.getOpenFileNames(None, "Select Images", "", "Images (*.png *.jpg *.webp)")
        if files:
            self.load_image_list(files)

    def open_folder(self):
        folder = QFileDialog.getExistingDirectory(None, "Select Folder")
        if folder:
            self.handle_dropped_files([folder])

    def handle_dropped_files(self, paths):
        all_images = []
        extensions = ('.png', '.jpg', '.jpeg', '.webp')
        for path in paths:
            if os.path.isdir(path):
                for root, _, files in os.walk(path):
                    for f in files:
                        if f.lower().endswith(extensions):
                            all_images.append(os.path.join(root, f))
            else:
                if path.lower().endswith(extensions):
                    all_images.append(path)
        
        if all_images:
            self.load_image_list(sorted(all_images))

    def load_image_list(self, files):
        self.image_list = files
        self.current_index = 0
        self.display_current()
        
    def load_paths(self, files):
        """Integration hook."""
        self.load_image_list(files)

    def prev_image(self):
        if self.image_list and self.current_index > 0:
            self.current_index -= 1
            self.display_current()

    def next_image(self):
        if self.image_list and self.current_index < len(self.image_list) - 1:
            self.current_index += 1
            self.display_current()

    def display_current(self):
        if 0 <= self.current_index < len(self.image_list):
            path = self.image_list[self.current_index]
            self.index_label.setText(f"🖼️ {self.current_index + 1} / {len(self.image_list)}")
            self.load_image_data(path)
            
            # Update button states
            self.btn_prev.setEnabled(self.current_index > 0)
            self.btn_next.setEnabled(self.current_index < len(self.image_list) - 1)

    def load_image_data(self, path):
        pixmap = QPixmap(path)
        self.image_label.set_image(pixmap)
        
        result = UniversalParser.parse_image(path)
        
        s = result.get("stats", {})
        fname = os.path.basename(path)
        stats_text = f"📄 {fname} | 📐 {s.get('format', '-')} | 💾 {s.get('size', '-')} | 📅 {s.get('created', '-')}"
        self.stats_label.setText(stats_text)
        
        if "error" in result:
            self.pos_prompt.setPlainText(f"Error: {result['error']}")
            self.neg_prompt.clear()
            self.meta_info.clear()
        else:
            self.pos_prompt.setPlainText(result.get("positive", ""))
            self.neg_prompt.setPlainText(result.get("negative", ""))
            
            tech_info = f"GENERATION TOOL: {result.get('tool', 'Unknown')}\n"
            tech_info += "-------------------\n"
            tech_info += f"Model: {result.get('model', '-')}\n"
            tech_info += f"VAE: {result.get('vae', '-')}\n"
            
            loras = result.get('loras', [])
            if loras:
                tech_info += f"LoRAs: {', '.join(loras)}\n"
            
            tech_info += f"Seed: {result.get('seed', '-')}\n"
            tech_info += f"Sampler: {result.get('sampler', '-')} | Steps: {result.get('steps', '-')} | CFG: {result.get('cfg', '-')}\n"
            tech_info += "-------------------\n\n"
            
            raw_str = tech_info
            for k, v in result.get("raw", {}).items():
                raw_str += f"[{k}]:\n{v}\n\n"
            self.meta_info.setPlainText(raw_str)
