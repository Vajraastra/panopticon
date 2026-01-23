from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox, 
                               QCheckBox, QSpinBox, QFileDialog, QProgressBar, QMessageBox, 
                               QGridLayout, QFrame, QSizePolicy, QStackedWidget)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
import os

from core.base_module import BaseModule
from core.components.standard_layout import StandardToolLayout
from .logic import optimize_image, analyze_image, get_export_path

class OptimizerWorker(QThread):
    progress = Signal(int)
    finished = Signal(dict)
    
    def __init__(self, queue, settings):
        super().__init__()
        self.queue = queue
        self.settings = settings
        self.running = True

    def run(self):
        stats = {"success": 0, "failed": 0, "saved_bytes": 0}
        
        for i, path in enumerate(self.queue):
            if not self.running: break
            
            try:
                dest = get_export_path(path, export_dir=self.settings['export_path'])
                
                result = optimize_image(
                    path, dest,
                    format_override=self.settings['format'],
                    quality=self.settings['quality'],
                    max_side=self.settings['max_side'],
                    preserve_metadata=self.settings['preserve_meta']
                )
                
                if result['success']:
                    stats['success'] += 1
                    stats['saved_bytes'] += result['saved_bytes']
                else:
                    stats['failed'] += 1
                    
            except Exception:
                stats['failed'] += 1
                
            self.progress.emit(i + 1)
            
        self.finished.emit(stats)

class ImageOptimizerModule(BaseModule):
    def __init__(self):
        super().__init__()
        self._name = "Image Optimizer"
        self._description = "Compress, Resize, and Convert images efficiently."
        self._icon = "🚀"
        self.accent_color = "#00ffcc"
        
        self.queue = []

    def get_view(self) -> QWidget:
        # 1. Create Internal Widgets
        self.sidebar = self._create_sidebar()
        self.content = self._create_content()
        self.bottom = self._create_bottom_bar()
        
        return StandardToolLayout(self.content, self.sidebar, self.bottom, 
                                  theme_manager=self.context.get('theme_manager'),
                                  event_bus=self.context.get('event_bus'))

    def _create_sidebar(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setAlignment(Qt.AlignTop)
        
        # Title
        lbl_title = QLabel("Settings")
        lbl_title.setStyleSheet("font-size: 16px; font-weight: bold; margin-bottom: 10px;")
        layout.addWidget(lbl_title)
        
        # Format
        layout.addWidget(QLabel("Output Format:"))
        self.combo_format = QComboBox()
        self.combo_format.addItems(["Original", "PNG", "JPEG", "WebP"])
        
        # FIX: Force opaque view using Palette AND Style
        from PySide6.QtWidgets import QListView
        from PySide6.QtGui import QPalette, QColor
        
        fmt_view = QListView()
        # 1. Palette fix (Force Base color)
        pal = fmt_view.palette()
        pal.setColor(QPalette.Base, QColor("#050505"))
        pal.setColor(QPalette.Text, QColor("#ffffff"))
        fmt_view.setPalette(pal)
        # 2. Direct Stylesheet override
        fmt_view.setStyleSheet("""
            QListView { 
                background-color: #050505; 
                border: 2px solid #00ffcc; 
                color: white; 
                outline: none;
            }
            QListView::item {
                padding: 5px;
                height: 25px;
            }
            QListView::item:selected {
                background-color: #00ffcc;
                color: black;
            }
        """)
        self.combo_format.setView(fmt_view)
        
        layout.addWidget(self.combo_format)
        
        # Resize
        layout.addWidget(QLabel("Resize Strategy:"))
        self.combo_resize = QComboBox()
        self.combo_resize.addItems(["Keep Original Size", "Longest Side: 1024px", "Longest Side: 2048px", "Custom Longest Side"])
        
        rsz_view = QListView()
        # Apply same fix to resize combo
        pal_rsz = rsz_view.palette()
        pal_rsz.setColor(QPalette.Base, QColor("#050505"))
        pal_rsz.setColor(QPalette.Text, QColor("#ffffff"))
        rsz_view.setPalette(pal_rsz)
        rsz_view.setStyleSheet("""
            QListView { 
                background-color: #050505; 
                border: 2px solid #00ffcc; 
                color: white; 
                outline: none;
            }
            QListView::item {
                padding: 5px;
                height: 25px;
            }
            QListView::item:selected {
                background-color: #00ffcc;
                color: black;
            }
        """)
        self.combo_resize.setView(rsz_view)
        
        self.combo_resize.currentIndexChanged.connect(self._on_resize_change)
        layout.addWidget(self.combo_resize)
        
        self.spin_max_side = QSpinBox()
        self.spin_max_side.setRange(64, 8192)
        self.spin_max_side.setValue(1024)
        self.spin_max_side.setEnabled(False)
        layout.addWidget(self.spin_max_side)
        
        # Checkboxes
        self.chk_meta = QCheckBox("Preserve Metadata")
        self.chk_meta.setChecked(True)
        layout.addWidget(self.chk_meta)
        
        # Analysis
        layout.addSpacing(20)
        btn_analyze = QPushButton("Analyze Suggestion")
        btn_analyze.clicked.connect(self._analyze_first)
        layout.addWidget(btn_analyze)
        
        self.lbl_suggestion = QLabel("")
        self.lbl_suggestion.setWordWrap(True)
        self.lbl_suggestion.setStyleSheet("color: #888; font-style: italic;")
        layout.addWidget(self.lbl_suggestion)
        
        layout.addStretch()
        return container

    def _create_content(self) -> QWidget:
        self.stack = QStackedWidget() # Switch between Empty and Grid
        self.stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        # --- PAGE 0: EMPTY STATE ---
        self.page_empty = QFrame()
        self.page_empty.setObjectName("drop_zone")
        self.page_empty.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        # Style the drop zone
        bg_main = self.context.get('theme_manager').get_color("bg_main")
        border_col = self.context.get('theme_manager').get_color("border")
        text_dim = self.context.get('theme_manager').get_color("text_dim")
        
        self.page_empty.setStyleSheet(f"""
            QFrame#drop_zone {{
                background-color: {bg_main};
                border: 2px dashed {border_col};
                border-radius: 12px;
                margin: 20px;
            }}
        """)
        
        empty_layout = QVBoxLayout(self.page_empty)
        empty_layout.setAlignment(Qt.AlignCenter)
        
        self.lbl_empty = QLabel("📂\n\nDrop images here\nor use 'Load Images'")
        self.lbl_empty.setStyleSheet(f"color: {text_dim}; font-size: 16px; font-weight: bold;")
        self.lbl_empty.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(self.lbl_empty)
        
        self.stack.addWidget(self.page_empty)
        
        # --- PAGE 1: PREVIEW LIST ---
        self.page_preview = QWidget()
        preview_layout = QVBoxLayout(self.page_preview)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        
        from PySide6.QtWidgets import QListWidget, QListWidgetItem
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QIcon
        
        self.list_preview = QListWidget()
        self.list_preview.setViewMode(QListWidget.IconMode)
        self.list_preview.setIconSize(QSize(120, 120))
        self.list_preview.setResizeMode(QListWidget.Adjust)
        self.list_preview.setSpacing(10)
        self.list_preview.setMovement(QListWidget.Static)
        self.list_preview.setSelectionMode(QListWidget.ExtendedSelection)
        self.list_preview.setStyleSheet(f"""
            QListWidget {{
                background-color: transparent;
                border: none;
                outline: none;
            }}
            QListWidget::item {{
                background-color: {self.context.get('theme_manager').get_color("bg_panel")};
                color: white;
                border-radius: 8px;
                padding: 10px;
            }}
            QListWidget::item:selected {{
                background-color: {self.context.get('theme_manager').get_color("accent_main")};
                color: black;
            }}
        """)
        
        preview_layout.addWidget(self.list_preview)
        self.stack.addWidget(self.page_preview)
        
        return self.stack

    def _create_bottom_bar(self) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0,0,0,0)
        
        self.btn_load = QPushButton("Load Images")
        self.btn_load.setCursor(Qt.PointingHandCursor)
        self.btn_load.clicked.connect(self._load_images)
        layout.addWidget(self.btn_load)
        
        layout.addStretch()
        
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setFixedWidth(300)
        self.progress.setStyleSheet(f"""
            QProgressBar {{
                border: 1px solid #444;
                border-radius: 4px;
                text-align: center;
                background-color: #222;
            }}
            QProgressBar::chunk {{
                background-color: {self.accent_color};
            }}
        """)
        layout.addWidget(self.progress)
        
        self.btn_run = QPushButton("Process Queue")
        self.btn_run.setCursor(Qt.PointingHandCursor)
        self.btn_run.setObjectName("action_btn") 
        
        # Primary Action Button Style
        text_col = "#000000" # Black text on bright accent usually looks best
        self.btn_run.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.accent_color}; 
                color: {text_col}; 
                font-weight: bold; 
                padding: 8px 24px; 
                border-radius: 4px;
                border: none;
            }}
            QPushButton:hover {{
                background-color: white;
            }}
            QPushButton:disabled {{
                background-color: #444;
                color: #888;
            }}
        """)
        self.btn_run.clicked.connect(self._run_all)
        layout.addWidget(self.btn_run)
        
        return container

    def _on_resize_change(self):
        txt = self.combo_resize.currentText()
        is_custom = "Custom" in txt
        self.spin_max_side.setEnabled(is_custom)

    def _load_images(self):
        files, _ = QFileDialog.getOpenFileNames(None, "Select Images", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if files:
            self.queue.extend(files)
            self._update_ui()

    def _analyze_first(self):
        if not self.queue: return
        res = analyze_image(self.queue[0])
        if "error" not in res:
            self.lbl_suggestion.setText(f"Suggestion: {res['suggested_format']} ({res['suggestion_reason']})")

    def _update_ui(self):
        # 1. Update State
        if not self.queue:
            self.stack.setCurrentIndex(0) # Empty State
            return
            
        self.stack.setCurrentIndex(1) # Preview State
        
        # 2. Populate List if needed (Optimization: Only add new items)
        # For simplicity in this iteration, we clear and rebuild. 
        # In production, we should only append.
        
        from PySide6.QtWidgets import QListWidgetItem
        from PySide6.QtGui import QIcon, QPixmap
        from PySide6.QtCore import QSize
        
        # If the list count matches the queue count, we assume it's up to date (fragile but ok for now)
        if self.list_preview.count() == len(self.queue):
             return

        self.list_preview.clear()
        
        # Limit preview to first 50 to prevent freezing on massive drops
        preview_limit = 50 
        
        for i, path in enumerate(self.queue):
            if i >= preview_limit:
                break
                
            name = os.path.basename(path)
            item = QListWidgetItem(name)
            
            # Create thumbnail
            # Note: Doing this on main thread for 50 images might be slightly laggy.
            # Ideally use a thread, but for "Basic" tier, this is fine.
            pix = QPixmap(path)
            if not pix.isNull():
                icon = QIcon(pix.scaled(120, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                item.setIcon(icon)
            
            self.list_preview.addItem(item)
            
        if len(self.queue) > preview_limit:
             item = QListWidgetItem(f"+ {len(self.queue) - preview_limit} more...")
             self.list_preview.addItem(item)

        # self.lbl_empty.setText(f"Queue: {len(self.queue)} images ready.")

    def _run_all(self):
        if not self.queue: return
        
        export_path = QFileDialog.getExistingDirectory(None, "Select Export Directory")
        if not export_path: return
        
        settings = {
            "format": None if "Original" in self.combo_format.currentText() else self.combo_format.currentText(),
            "quality": 90,
            "max_side": self.spin_max_side.value() if self.spin_max_side.isEnabled() else None,
            "preserve_meta": self.chk_meta.isChecked(),
            "export_path": export_path
        }
        
        # Parse predefined presets
        preset = self.combo_resize.currentText()
        if "1024" in preset: settings['max_side'] = 1024
        elif "2048" in preset: settings['max_side'] = 2048
        
        self.progress.setMaximum(len(self.queue))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.btn_run.setEnabled(False)
        
        self.worker = OptimizerWorker(self.queue, settings)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _on_finished(self, stats):
        self.btn_run.setEnabled(True)
        self.progress.setVisible(False)
        QMessageBox.information(None, "Done", f"Processed {stats['success'] + stats['failed']} images.\nSaved: {stats['saved_bytes']/1024/1024:.2f} MB")
        self.queue = []
        self._update_ui()

    def run_headless(self, params: dict, input_data: any) -> any:
        # Implement for future automation
        pass
