import os
import logging
from pathlib import Path
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
                               QCheckBox, QSpinBox, QFileDialog, QProgressBar, QMessageBox,
                               QFrame, QSizePolicy, QStackedWidget, QListWidget, QListWidgetItem,
                               QListView)
from PySide6.QtCore import Qt, QThread, Signal, QSize
from PySide6.QtGui import QPixmap, QIcon, QPalette, QColor

from core.base_module import BaseModule
from core.theme import Theme
from core.components.standard_layout import StandardToolLayout
from .logic.optimizer import optimize_image, analyze_image, get_export_path

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.avif'}

# Resize combo: index → max_side px (None = keep original, missing key = custom spinbox)
RESIZE_SIDES = {0: None, 1: 1024, 2: 2048}


class DropFrame(QFrame):
    """QFrame con soporte drag & drop para imágenes sueltas y carpetas."""
    files_dropped = Signal(list)  # list of str paths

    def __init__(self, accent_color, border_color, text_color, bg_color="transparent", parent=None):
        super().__init__(parent)
        self.accent_color = accent_color
        self.border_color = border_color
        self.bg_color = bg_color
        self.setAcceptDrops(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._set_idle_style()

    def _set_idle_style(self):
        self.setStyleSheet(f"""
            QFrame {{
                border: 2px dashed {self.border_color};
                border-radius: 12px;
                background-color: {self.bg_color};
                margin: 20px;
            }}
        """)

    def _set_hover_style(self):
        self.setStyleSheet(f"""
            QFrame {{
                border: 2px solid {self.accent_color};
                border-radius: 12px;
                background-color: {self.bg_color};
                margin: 20px;
            }}
        """)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_hover_style()

    def dragLeaveEvent(self, event):
        self._set_idle_style()

    def dropEvent(self, event):
        self._set_idle_style()
        paths = []
        for url in event.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_dir():
                # collect all images in folder
                for ext in IMAGE_EXTENSIONS:
                    paths.extend(str(f) for f in p.rglob(f"*{ext}"))
            elif p.suffix.lower() in IMAGE_EXTENSIONS:
                paths.append(str(p))
        if paths:
            self.files_dropped.emit(paths)
        event.acceptProposedAction()


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
            if not self.running:
                break
            try:
                dest = get_export_path(path, export_dir=self.settings['export_path'])
                result = optimize_image(
                    path, dest,
                    format_override=self.settings['format'],
                    quality=self.settings['quality'],
                    max_side=self.settings['max_side'],
                    preserve_metadata=self.settings['preserve_meta'],
                    tags=self.settings.get('tags_map', {}).get(path, []),
                    rating=self.settings.get('rating_map', {}).get(path, 0)
                )
                if result['success']:
                    stats['success'] += 1
                    stats['saved_bytes'] += result['saved_bytes']
                else:
                    stats['failed'] += 1
            except Exception as e:
                logging.warning(f"[ImageOptimizer] Failed to process {path}: {e}")
                stats['failed'] += 1

            self.progress.emit(i + 1)

        self.finished.emit(stats)


class ImageOptimizerModule(BaseModule):
    def __init__(self):
        super().__init__()
        self._name = "Image Optimizer"
        self._description = "Batch compress, resize and convert images preserving metadata."
        self._icon = "🚀"
        self.accent_color = "#00ffcc"
        self.view = None
        self.queue = []

    def get_view(self) -> QWidget:
        if self.view:
            return self.view

        sidebar = self._create_sidebar()
        content = self._create_content()

        self.view = StandardToolLayout(
            content, sidebar,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        return self.view

    def _create_sidebar(self) -> QWidget:
        theme    = self.context.get('theme_manager') if hasattr(self, 'context') else None
        text_dim = theme.get_color('text_dim') if theme else Theme.TEXT_DIM

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        lbl_title = QLabel(self.tr("opt.title", "🚀 IMAGE OPTIMIZER"))
        lbl_title.setStyleSheet(f"color: {self.accent_color}; font-weight: bold; font-size: 14px;")
        layout.addWidget(lbl_title)

        lbl_desc = QLabel(self.tr("opt.desc", "Batch compress, resize and convert images."))
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"color: {text_dim}; font-size: 11px;")
        layout.addWidget(lbl_desc)

        layout.addSpacing(10)

        layout.addWidget(QLabel(self.tr("opt.format", "Output Format:")))
        self.combo_format = QComboBox()
        self.combo_format.addItems([
            self.tr("opt.format.original", "Original (no change)"),
            "PNG", "JPEG", "WebP"
        ])
        self.combo_format.setView(self._make_combo_view())
        layout.addWidget(self.combo_format)

        layout.addWidget(QLabel(self.tr("opt.resize", "Resize Strategy:")))
        self.combo_resize = QComboBox()
        self.combo_resize.addItems([
            self.tr("opt.resize.original", "Keep Original Size"),
            self.tr("opt.resize.1024",     "Longest Side: 1024px"),
            self.tr("opt.resize.2048",     "Longest Side: 2048px"),
            self.tr("opt.resize.custom",   "Custom Longest Side")
        ])
        self.combo_resize.setView(self._make_combo_view())
        self.combo_resize.currentIndexChanged.connect(self._on_resize_change)
        layout.addWidget(self.combo_resize)

        self.spin_max_side = QSpinBox()
        self.spin_max_side.setRange(64, 8192)
        self.spin_max_side.setValue(1024)
        self.spin_max_side.setEnabled(False)
        layout.addWidget(self.spin_max_side)

        self.chk_meta = QCheckBox(self.tr("opt.preserve_meta", "Preserve Metadata"))
        self.chk_meta.setChecked(True)
        layout.addWidget(self.chk_meta)

        self.lbl_suggestion = QLabel("")
        self.lbl_suggestion.setWordWrap(True)
        self.lbl_suggestion.setStyleSheet(f"color: {text_dim}; font-style: italic; font-size: 11px;")
        layout.addWidget(self.lbl_suggestion)

        layout.addStretch()

        # Action buttons — consolidated at sidebar bottom
        self.btn_load = QPushButton(self.tr("opt.load_images", "Load Images"))
        self.btn_load.setCursor(Qt.PointingHandCursor)
        self.btn_load.clicked.connect(self._load_images)
        layout.addWidget(self.btn_load)

        self.btn_analyze = QPushButton(self.tr("opt.analyze", "Analyze Suggestion"))
        self.btn_analyze.setCursor(Qt.PointingHandCursor)
        self.btn_analyze.clicked.connect(self._analyze_first)
        layout.addWidget(self.btn_analyze)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setStyleSheet("""
            QProgressBar { border: 1px solid #444; border-radius: 4px; text-align: center; background-color: #222; }
            QProgressBar::chunk { background-color: #00ffcc; }
        """)
        layout.addWidget(self.progress)

        self.btn_run = QPushButton(self.tr("opt.process", "Process Queue"))
        self.btn_run.setCursor(Qt.PointingHandCursor)
        self.btn_run.setStyleSheet(f"""
            QPushButton {{ background-color: {self.accent_color}; color: #000; font-weight: bold; padding: 8px 24px; border-radius: 4px; border: none; }}
            QPushButton:hover {{ background-color: white; }}
            QPushButton:disabled {{ background-color: #444; color: #888; }}
        """)
        self.btn_run.clicked.connect(self._run_all)
        layout.addWidget(self.btn_run)

        return container

    def _make_combo_view(self) -> QListView:
        view = QListView()
        pal = view.palette()
        pal.setColor(QPalette.Base, QColor("#050505"))
        pal.setColor(QPalette.Text, QColor("#ffffff"))
        view.setPalette(pal)
        view.setStyleSheet(f"""
            QListView {{
                background-color: #050505;
                border: 2px solid {self.accent_color};
                color: white;
                outline: none;
            }}
            QListView::item {{ padding: 5px; height: 25px; }}
            QListView::item:selected {{
                background-color: {self.accent_color};
                color: black;
            }}
        """)
        return view

    def _create_content(self) -> QWidget:
        theme    = self.context.get('theme_manager') if hasattr(self, 'context') else None
        bg_main  = theme.get_color('bg_main')    if theme else Theme.BG_MAIN
        border   = theme.get_color('border')      if theme else Theme.BORDER
        text_dim = theme.get_color('text_dim')    if theme else Theme.TEXT_DIM
        bg_panel = theme.get_color('bg_panel')    if theme else Theme.BG_PANEL
        accent   = theme.get_color('accent_main') if theme else self.accent_color

        self.stack = QStackedWidget()
        self.stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Page 0: drop zone
        self.page_empty = DropFrame(
            accent_color=self.accent_color,
            border_color=border,
            text_color=text_dim,
            bg_color=bg_main
        )
        self.page_empty.files_dropped.connect(self._on_files_dropped)
        empty_layout = QVBoxLayout(self.page_empty)
        empty_layout.setAlignment(Qt.AlignCenter)
        self.lbl_empty = QLabel(self.tr("opt.drop_zone", "📂\n\nDrop images here\nor use 'Load Images'"))
        self.lbl_empty.setStyleSheet(f"color: {text_dim}; font-size: 16px; font-weight: bold; border: none;")
        self.lbl_empty.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(self.lbl_empty)
        self.stack.addWidget(self.page_empty)

        # Page 1: preview list
        self.page_preview = QWidget()
        preview_layout = QVBoxLayout(self.page_preview)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        self.list_preview = QListWidget()
        self.list_preview.setViewMode(QListWidget.IconMode)
        self.list_preview.setIconSize(QSize(120, 120))
        self.list_preview.setResizeMode(QListWidget.Adjust)
        self.list_preview.setSpacing(10)
        self.list_preview.setMovement(QListWidget.Static)
        self.list_preview.setSelectionMode(QListWidget.ExtendedSelection)
        self.list_preview.setStyleSheet(f"""
            QListWidget {{ background-color: transparent; border: none; outline: none; }}
            QListWidget::item {{ background-color: {bg_panel}; color: white; border-radius: 8px; padding: 10px; }}
            QListWidget::item:selected {{ background-color: {accent}; color: black; }}
        """)
        preview_layout.addWidget(self.list_preview)
        self.stack.addWidget(self.page_preview)

        return self.stack

    # ------------------------------------------------------------------ #

    def _on_resize_change(self, index: int):
        self.spin_max_side.setEnabled(index == 3)

    def _load_images(self):
        files, _ = QFileDialog.getOpenFileNames(
            self.view,
            self.tr("common.select_image", "Select Images"),
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.avif)"
        )
        if files:
            self.queue.extend(files)
            self._update_ui()

    def _on_files_dropped(self, paths: list):
        new = [p for p in paths if p not in self.queue]
        self.queue.extend(new)
        self._update_ui()

    def _analyze_first(self):
        if not self.queue:
            return
        res = analyze_image(self.queue[0])
        if "error" not in res:
            self.lbl_suggestion.setText(
                self.tr("opt.suggestion", "Suggestion: {format} ({reason})").format(
                    format=res['suggested_format'],
                    reason=res['suggestion_reason']
                )
            )

    def _update_ui(self):
        if not self.queue:
            self.stack.setCurrentIndex(0)
            return

        self.stack.setCurrentIndex(1)

        if self.list_preview.count() == len(self.queue):
            return

        self.list_preview.clear()
        preview_limit = 50

        for i, path in enumerate(self.queue):
            if i >= preview_limit:
                break
            item = QListWidgetItem(os.path.basename(path))
            pix = QPixmap(path)
            if not pix.isNull():
                item.setIcon(QIcon(pix.scaled(120, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
            self.list_preview.addItem(item)

        if len(self.queue) > preview_limit:
            self.list_preview.addItem(QListWidgetItem(f"+ {len(self.queue) - preview_limit} more..."))

    def _run_all(self):
        if not self.queue:
            return

        export_path = QFileDialog.getExistingDirectory(
            self.view,
            self.tr("common.select_folder", "Select Export Directory")
        )
        if not export_path:
            return

        # Format: index 0 = no override, 1=PNG, 2=JPEG, 3=WebP
        fmt_names = {1: "PNG", 2: "JPEG", 3: "WebP"}
        format_override = fmt_names.get(self.combo_format.currentIndex())  # None if 0

        # Resize: index 0=original, 1=1024, 2=2048, 3=custom spinbox
        resize_idx = self.combo_resize.currentIndex()
        max_side = RESIZE_SIDES.get(resize_idx)  # None for 0
        if resize_idx == 3:
            max_side = self.spin_max_side.value()

        settings = {
            "format": format_override,
            "quality": 90,
            "max_side": max_side,
            "preserve_meta": self.chk_meta.isChecked(),
            "export_path": export_path
        }

        from modules.librarian.logic.db_manager import DatabaseManager
        db = DatabaseManager()
        settings['tags_map']   = {p: db.get_tags_for_file(p) for p in self.queue}
        settings['rating_map'] = {p: db.get_file_rating(p)   for p in self.queue}

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
        QMessageBox.information(
            self.view,
            self.tr("opt.done", "Done"),
            self.tr("opt.stats", "Processed {total} images.\nSaved: {saved} MB").format(
                total=stats['success'] + stats['failed'],
                saved=f"{stats['saved_bytes'] / 1024 / 1024:.2f}"
            )
        )
        self.queue = []
        self._update_ui()

    def load_image_set(self, paths: list):
        """Standard interface for receiving image sets from Librarian."""
        if paths:
            self.queue = list(paths)
            self._update_ui()

    def run_headless(self, params: dict, input_data) -> None:
        pass
