from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout,
                               QFileDialog, QMessageBox, QTextEdit, QApplication,
                               QSplitter, QFrame, QSizePolicy)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QPainter
from shiboken6 import isValid
from core.base_module import BaseModule
from core.theme_manager import ThemeManager
from .logic.worker import MetadataWorker
import json
import os
import logging

log = logging.getLogger(__name__)

_SUPPORTED_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.avif'}
_LIVE_WORKERS = set()

class ResponsiveImageLabel(QLabel):
    """A QLabel que muestra imágenes escaladas y acepta drag & drop de archivos."""
    files_dropped = Signal(list)  # emite lista de rutas válidas al soltar

    def __init__(self, text="", bg=None, border=None, accent=None):
        super().__init__(text)
        colors = ThemeManager.active().get_color
        bg, border, accent = bg or colors('bg_main'), border or colors('border'), accent or colors('accent_main')
        self._bg = bg
        self._border = border
        self._accent = accent
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(f"background-color: {bg}; border: 1px solid {border}; border-radius: 8px;")
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self._pixmap = None
        self.setAcceptDrops(True)

    def set_image(self, image):
        if image is None:
            self._pixmap = None
            self.setText("")
            self.update()
            return
        self._pixmap = QPixmap.fromImage(image)
        self.setText("")
        self.update()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls()]
            if any(os.path.splitext(p)[1].lower() in _SUPPORTED_EXTS for p in paths):
                self.setStyleSheet(f"background-color: {self._bg}; border: 2px solid {self._accent}; border-radius: 8px;")
                event.acceptProposedAction()
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet(f"background-color: {self._bg}; border: 1px solid {self._border}; border-radius: 8px;")

    def dropEvent(self, event):
        self.setStyleSheet(f"background-color: {self._bg}; border: 1px solid {self._border}; border-radius: 8px;")
        paths = [
            u.toLocalFile() for u in event.mimeData().urls()
            if os.path.splitext(u.toLocalFile())[1].lower() in _SUPPORTED_EXTS
        ]
        if paths:
            self.files_dropped.emit(paths)
        event.acceptProposedAction()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._pixmap or self._pixmap.isNull():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        s = self._pixmap.size()
        s.scale(self.size(), Qt.KeepAspectRatio)

        x = (self.width() - s.width()) // 2
        y = (self.height() - s.height()) // 2

        painter.drawPixmap(x, y, s.width(), s.height(), self._pixmap)

class MetadataModule(BaseModule):
    """
    Metadata Hub.
    Motor unificado para leer, editar y limpiar metadatos de imágenes.
    Permite visualizar los Prompts originales de la IA y modificarlos o eliminarlos.
    Incluye un visor (Viewer) y un editor (Editor) integrados.
    """
    MODE_VIEWER = 0
    MODE_EDITOR = 1

    def __init__(self):
        super().__init__()
        self._name = "Metadata Hub"
        self._description = "Motor unificado para leer, editar y limpiar metadatos de imágenes."
        self._icon = "🔍"
        
        self.view = None
        self.image_list = [] # Lista de imágenes en el carrusel
        self.current_index = -1
        self.active_path = None
        self.current_mode = self.MODE_VIEWER
        self._closed = self._busy = self._confirming = False
        self._worker = None
        self._workers = set()
        self._token = 0
        self._capture = None
        self._pending_result = None
        self._history_after = 0
        self._history_more = False

    def on_load(self, context=None):
        super().on_load(context or {})
        self._closed = False

    def on_unload(self):
        self._closed = True
        self._token += 1
        for worker in self._workers:
            worker.stop()

    def shutdown_ready(self):
        return all(not worker.isRunning() for worker in self._workers)

    def _color(self, key):
        manager = self.context.get('theme_manager') or ThemeManager.active()
        return manager.get_color(key)

    def get_view(self) -> QWidget:
        """Configura el Hub con una vista de carrusel y paneles de edición lateral."""
        if self.view: return self.view
        
        sidebar = self._create_sidebar()
        content = self._create_content()
        
        from core.components.standard_layout import StandardToolLayout
        self.view = StandardToolLayout(
            content,
            sidebar_widget=sidebar,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        
        # Establece el modo inicial (solo lectura por defecto)
        self.set_mode(self.MODE_VIEWER)
        self.view.destroyed.connect(self.on_unload)
        
        return self.view

    def _create_sidebar(self) -> QWidget:
        tm = self.context.get('theme_manager')
        c = self._color

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        # Title
        lbl_title = QLabel(self.tr("meta.title", "🔍 METADATA HUB"))
        lbl_title.setStyleSheet(f"color: {c('accent_main')}; font-weight: bold; font-size: 14px;")
        layout.addWidget(lbl_title)

        lbl_desc = QLabel(self.tr("meta.desc", "Read, edit and clean image metadata."))
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"color: {c('text_dim')}; font-size: 11px;")
        layout.addWidget(lbl_desc)

        layout.addSpacing(10)

        lbl = QLabel(self.tr("meta.mode", "🛠️ MODE"))
        lbl.setStyleSheet(f"color: {c('text_secondary')}; font-weight: bold; font-size: 12px;")
        layout.addWidget(lbl)

        # Mode Selector (Toggle)
        self.btn_mode_view = QPushButton(self.tr("meta.viewer", "🔍 Viewer Mode"))
        self.btn_mode_view.setToolTip(self.tr(
            "meta.viewer_tip",
            "Read-only: shows the prompts and generation data without letting you change "
            "them."))
        self.btn_mode_view.setCheckable(True)
        self.btn_mode_view.setChecked(True)
        self.btn_mode_view.setFixedHeight(38)
        self.btn_mode_view.clicked.connect(lambda: self.set_mode(self.MODE_VIEWER))
        layout.addWidget(self.btn_mode_view)

        self.btn_mode_edit = QPushButton(self.tr("meta.editor", "✍️ Editor Mode"))
        self.btn_mode_edit.setToolTip(self.tr(
            "meta.editor_tip",
            "Lets you edit the prompts. This is the only module that can write over the "
            "original file — that is what it is for."))
        self.btn_mode_edit.setCheckable(True)
        self.btn_mode_edit.setFixedHeight(38)
        self.btn_mode_edit.clicked.connect(lambda: self.set_mode(self.MODE_EDITOR))
        layout.addWidget(self.btn_mode_edit)

        # Style for modes (called after buttons exist)
        self._update_mode_styles()

        layout.addSpacing(10)
        lbl_act = QLabel(self.tr("meta.actions", "⚡ ACTIONS"))
        lbl_act.setStyleSheet(f"color: {c('text_secondary')}; font-weight: bold; font-size: 12px;")
        layout.addWidget(lbl_act)

        self.btn_save = QPushButton(self.tr("meta.save", "💾 Save Changes"))
        self.btn_save.setToolTip(self.tr(
            "meta.save_tip",
            "Updates the displayed indexed copy after confirmation and verification. "
            "Previous generation and a recovery backup remain available; Cherry originals are protected."))
        self.btn_save.setFixedHeight(40)
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.clicked.connect(self.action_save_current)
        self.btn_save.setStyleSheet(f"""
            QPushButton {{
                background-color: {c('accent_main')}; color: {c('bg_main')};
                border-radius: 8px; font-size: 14px; font-weight: bold; padding: 10px;
            }}
            QPushButton:hover {{ background-color: {c('accent_hover')}; }}
            QPushButton:disabled {{ background-color: {c('border')}; color: {c('text_dim')}; }}
        """)
        layout.addWidget(self.btn_save)

        self.btn_export = QPushButton(self.tr("meta.export", "📂 Export Copy"))
        self.btn_export.setToolTip(self.tr(
            "meta.export_tip",
            "Saves a NEW file with the edited metadata and leaves the original untouched. "
            "It is the safe option next to «Save changes»."))
        self.btn_export.setFixedHeight(36)
        self.btn_export.setCursor(Qt.PointingHandCursor)
        self.btn_export.clicked.connect(self.action_export_copy)
        self.btn_export.setStyleSheet(f"""
            QPushButton {{
                background-color: {c('bg_panel')}; color: {c('text_secondary')};
                border-radius: 6px; font-weight: bold; font-size: 11px;
                padding: 5px 10px; border: 1px solid transparent;
            }}
            QPushButton:hover {{ background-color: {c('border')}; border: 1px solid {c('accent_main')}; }}
        """)
        layout.addWidget(self.btn_export)

        self.btn_history = QPushButton(self.tr('tool.metadata.history', 'Generation history'))
        self.btn_history.setToolTip(self.tr('tool.metadata.history_tip', 'Read the first two previous generations of this copy ID without changing the image or database.'))
        self.btn_history.clicked.connect(lambda: self._read_history(False))
        layout.addWidget(self.btn_history)
        self.btn_history_next = QPushButton(self.tr('tool.metadata.history_next', 'Next history page'))
        self.btn_history_next.setToolTip(self.tr('tool.metadata.history_next_tip', 'Replace the displayed history with up to two more records; no accumulated pages.'))
        self.btn_history_next.clicked.connect(lambda: self._read_history(True))
        layout.addWidget(self.btn_history_next)
        self.btn_reload = QPushButton(self.tr('tool.metadata.reload', 'Reload current image'))
        self.btn_reload.setToolTip(self.tr('tool.metadata.reload_tip', 'Read the file and native identity again; discard unsaved edits and old review evidence.'))
        self.btn_reload.clicked.connect(self.display_current)
        layout.addWidget(self.btn_reload)
        self.btn_cancel = QPushButton(self.tr('tool.metadata.cancel', 'Cancel'))
        self.btn_cancel.setToolTip(self.tr('tool.metadata.cancel_tip', 'Stop before publication. A file already published and an admitted database write finish; closing waits without blocking the interface.'))
        self.btn_cancel.clicked.connect(self._cancel_job)
        layout.addWidget(self.btn_cancel)
        self.lbl_operation = QLabel()
        self.lbl_operation.setTextFormat(Qt.PlainText)
        self.lbl_operation.setWordWrap(True)
        layout.addWidget(self.lbl_operation)

        layout.addStretch()

        lbl_info = QLabel(self.tr("meta.list", "IMAGE LIST"))
        lbl_info.setStyleSheet(f"color: {c('text_dim')}; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_info)

        self.lbl_carousel_stats = QLabel(self.tr("meta.stats", "{current} / {total} images").format(current=0, total=0))
        self.lbl_carousel_stats.setStyleSheet(f"color: {c('text_dim')};")
        layout.addWidget(self.lbl_carousel_stats)

        return container

    def _create_content(self) -> QWidget:
        tm = self.context.get('theme_manager')
        c = self._color

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 20, 20, 20)

        self.splitter = QSplitter(Qt.Horizontal)

        # Left: Viewer
        viewer_container = QWidget()
        viewer_layout = QVBoxLayout(viewer_container)
        viewer_layout.setContentsMargins(0, 0, 0, 0)

        # Action Bar (Top)
        top_bar = QHBoxLayout()
        self.btn_open = QPushButton(self.tr("opt.load_images", "📂 Open Image(s)"))
        self.btn_open.setToolTip(self.tr(
            "meta.open_tip", "Loads one or more images to view or edit their metadata."))
        self.btn_open.clicked.connect(self.open_files_dialog)
        self.btn_open.setStyleSheet(f"background: {c('bg_panel')}; color: {c('text_primary')}; padding: 8px 15px; border-radius: 5px; border: 1px solid {c('border_strong')};")
        top_bar.addWidget(self.btn_open)
        top_bar.addStretch()

        self.lbl_file_name = QLabel(self.tr("meta.no_file", "No file selected"))
        self.lbl_file_name.setTextFormat(Qt.PlainText)
        self.lbl_file_name.setStyleSheet(f"color: {c('text_dim')}; font-family: Consolas;")
        top_bar.addWidget(self.lbl_file_name)
        viewer_layout.addLayout(top_bar)

        self.image_label = ResponsiveImageLabel(
            self.tr("meta.drop_hint", "📂 Drop images here\nor click Open Image(s)"),
            bg=c('bg_main'), border=c('border'), accent=c('accent_main')
        )
        self.image_label.files_dropped.connect(self.load_image_set)
        viewer_layout.addWidget(self.image_label, 1)

        # Carousel Controls
        carousel_layout = QHBoxLayout()
        nav_style = f"padding: 10px; background: {c('bg_input')}; color: {c('text_secondary')}; border-radius: 5px; border: 1px solid {c('border_strong')};"
        self.btn_prev = QPushButton(self.tr("meta.prev", "◀ Previous"))
        self.btn_prev.setToolTip(self.tr(
            "meta.prev_tip", "Previous of the loaded images. Unsaved changes are lost."))
        self.btn_prev.clicked.connect(self.go_prev)
        self.btn_prev.setStyleSheet(nav_style)

        self.btn_next = QPushButton(self.tr("meta.next", "Next ▶"))
        self.btn_next.setToolTip(self.tr(
            "meta.next_tip", "Next of the loaded images. Unsaved changes are lost."))
        self.btn_next.clicked.connect(self.go_next)
        self.btn_next.setStyleSheet(nav_style)

        carousel_layout.addWidget(self.btn_prev)
        carousel_layout.addStretch()
        carousel_layout.addWidget(self.btn_next)
        viewer_layout.addLayout(carousel_layout)

        self.splitter.addWidget(viewer_container)

        # Right: Editor
        editor_container = QFrame()
        editor_container.setStyleSheet(f"background-color: {c('bg_panel')}; border-radius: 10px;")
        editor_layout = QVBoxLayout(editor_container)
        editor_layout.setSpacing(10)

        lbl_pos = QLabel(self.tr("meta.positive", "✨ Positive Prompt:"))
        lbl_pos.setStyleSheet(f"font-weight: bold; color: {c('accent_success')}; font-size: 13px;")
        editor_layout.addWidget(lbl_pos)

        self.txt_pos = QTextEdit()
        self.txt_pos.setToolTip(self.tr('tool.metadata.positive_tip', 'Edit the positive prompt, up to 65,536 characters. Save affects the reviewed indexed copy; export requires a fresh path.'))
        self.txt_pos.setPlaceholderText(self.tr("meta.placeholder.pos", "Enter positive prompt..."))
        self.txt_pos.setStyleSheet(f"background: {c('bg_main')}; color: {c('text_primary')}; border: 1px solid {c('border')}; border-radius: 5px; font-family: Consolas;")
        editor_layout.addWidget(self.txt_pos, 2)

        lbl_neg = QLabel(self.tr("meta.negative", "🚫 Negative Prompt:"))
        lbl_neg.setStyleSheet(f"font-weight: bold; color: {c('accent_warning')}; font-size: 13px;")
        editor_layout.addWidget(lbl_neg)

        self.txt_neg = QTextEdit()
        self.txt_neg.setToolTip(self.tr('tool.metadata.negative_tip', 'Edit the negative prompt. Leaving both prompts blank requests metadata cleaning after confirmation.'))
        self.txt_neg.setPlaceholderText(self.tr("meta.placeholder.neg", "Enter negative prompt..."))
        self.txt_neg.setStyleSheet(f"background: {c('bg_main')}; color: {c('text_primary')}; border: 1px solid {c('border')}; border-radius: 5px; font-family: Consolas;")
        editor_layout.addWidget(self.txt_neg, 1)

        lbl_tech = QLabel(self.tr("meta.tech_meta", "⚙️ Technical Metadata:"))
        lbl_tech.setStyleSheet(f"font-weight: bold; color: {c('text_dim')}; font-size: 12px;")
        editor_layout.addWidget(lbl_tech)

        self.txt_tech = QTextEdit()
        self.txt_tech.setToolTip(self.tr('tool.metadata.details_tip', 'Current file data or a bounded page of generation history. Native tags and manual rating are preserved separately.'))
        self.txt_tech.setReadOnly(True)
        self.txt_tech.setStyleSheet(f"background: {c('bg_main')}; color: {c('text_dim')}; border: none; font-size: 11px; font-family: Consolas;")
        editor_layout.addWidget(self.txt_tech, 2)

        self.splitter.addWidget(editor_container)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)

        layout.addWidget(self.splitter)

        return container

    def set_mode(self, mode):
        self.current_mode = mode
        is_edit = (mode == self.MODE_EDITOR)
        tm = self.context.get('theme_manager')
        c = self._color

        # Toggle read-only
        self.txt_pos.setReadOnly(not is_edit)
        self.txt_neg.setReadOnly(not is_edit)

        # Toggle button states
        self.btn_save.setEnabled(is_edit)

        # Update UI feedback
        self._update_mode_styles()

        # Visual cues on text fields
        base_style = f"border-radius: 5px; font-family: Consolas; background: {c('bg_main')};"
        if is_edit:
            self.txt_pos.setStyleSheet(f"{base_style} color: {c('accent_main')}; border: 1px solid {c('accent_main')};")
            self.txt_neg.setStyleSheet(f"{base_style} color: {c('accent_warning')}; border: 1px solid {c('accent_warning')};")
        else:
            self.txt_pos.setStyleSheet(f"{base_style} color: {c('text_primary')}; border: 1px solid {c('border_strong')};")
            self.txt_neg.setStyleSheet(f"{base_style} color: {c('text_primary')}; border: 1px solid {c('border_strong')};")
        self._controls()

    def _update_mode_styles(self):
        view_active = (self.current_mode == self.MODE_VIEWER)
        tm = self.context.get('theme_manager')
        c = self._color

        self.btn_mode_view.setChecked(view_active)
        self.btn_mode_edit.setChecked(not view_active)

        active_style = f"background-color: {c('bg_panel')}; color: {c('accent_main')}; border: 1px solid {c('accent_main')}; font-weight: bold; border-radius: 6px; text-align: left; padding: 5px;"
        inactive_style = f"background-color: {c('bg_main')}; color: {c('text_dim')}; border: 1px solid {c('border')}; border-radius: 6px; text-align: left; padding: 5px;"

        self.btn_mode_view.setStyleSheet(active_style if view_active else inactive_style)
        self.btn_mode_edit.setStyleSheet(inactive_style if view_active else active_style)

    def load_image_set(self, paths: list):
        """Standard integration from Librarian."""
        if not paths or self._closed or self._busy: return
        if self.view is None: self.get_view()
        if len(paths) > 100000:
            self._notice('selection_limit', 'Select at most 100,000 image paths.', danger=True)
            return
        size = 0
        for path in paths:
            if not isinstance(path, str) or not path or len(path) > 4096:
                self._notice('selection_limit', 'Select at most 100,000 image paths.', danger=True)
                return
            size += len(path.encode('utf-8'))+4
            if size > 8*1024*1024:
                self._notice('selection_bytes', 'Selected paths exceed the 8 MiB review limit.', danger=True)
                return
        self.image_list = sorted(paths)
        self.current_index = 0
        self.display_current()

    def load_images(self, paths: list):
        """Alias for compatibility."""
        self.load_image_set(paths)

    def display_current(self):
        if self._closed or self._busy: return
        self._capture = None
        self._history_after, self._history_more = 0, False
        if not (0 <= self.current_index < len(self.image_list)):
            self.active_path = None
            self.image_label.set_image(None)
            self.lbl_file_name.setText(self.tr("meta.no_file", "No file selected"))
            self.clear_fields()
            self._controls()
            return

        self.active_path = self.image_list[self.current_index]
        self.image_label.set_image(None)
        self.clear_fields()
        self.lbl_file_name.setText(os.path.basename(self.active_path))
        self.lbl_carousel_stats.setText(self.tr("meta.stats", "{current} / {total} images").format(current=self.current_index + 1, total=len(self.image_list)))
        
        self._start('load', path=self.active_path)

    def clear_fields(self):
        self.txt_pos.clear()
        self.txt_neg.clear()
        self.txt_tech.clear()

    def go_prev(self):
        if self._busy or self._confirming or self._closed: return
        if self.current_index > 0:
            self.current_index -= 1
            self.display_current()

    def go_next(self):
        if self._busy or self._confirming or self._closed: return
        if self.current_index < len(self.image_list) - 1:
            self.current_index += 1
            self.display_current()

    def open_files_dialog(self):
        if self._busy or self._confirming or self._closed: return
        files, _ = QFileDialog.getOpenFileNames(self.view, self.tr("opt.load_images", "Select Images"), "", "Images (*.png *.jpg *.jpeg *.webp *.avif)")
        if files:
            self.load_image_set(files)


    def action_save_current(self):
        captured = self._action_capture(require_native=True)
        if captured is None: return
        token, path, mode, capture, pos, neg = captured
        self._confirming = True
        self._controls()
        box = QMessageBox(self.view)
        box.setWindowTitle(self.tr('tool.metadata.confirm_title', 'Update indexed original?'))
        box.setIcon(QMessageBox.Warning)
        box.setTextFormat(Qt.PlainText)
        box.setText(self.tr('tool.metadata.confirm_body',
            'File: {path}\nCopy ID: {id}\n\n{action}\nPrevious generation and a recovery backup will be retained. '
            'Native tags, rating and Cherry receipts are preserved. AVIF is re-encoded and may change pixels.\n\nUpdate this original?').format(
                path=path, id=capture['native']['state']['file']['id'], action=self.tr(
                    'tool.metadata.clean_action' if not pos.strip() and not neg.strip() else 'tool.metadata.edit_action',
                    'Remove generation metadata from the file.' if not pos.strip() and not neg.strip() else 'Replace the file prompts with these edits.')))
        box.setStandardButtons(QMessageBox.Save | QMessageBox.Cancel)
        box.button(QMessageBox.Save).setText(self.tr('tool.metadata.confirm_save', 'Update original'))
        box.button(QMessageBox.Save).setToolTip(self.tr('tool.metadata.confirm_save_tip', 'Verify and update this reviewed indexed copy, keeping generation history and a recovery backup.'))
        box.button(QMessageBox.Cancel).setText(self.tr('tool.metadata.cancel', 'Cancel'))
        box.button(QMessageBox.Cancel).setToolTip(self.tr('tool.metadata.confirm_cancel_tip', 'Close this confirmation without saving or changing the file.'))
        box.setDefaultButton(QMessageBox.Cancel)
        box.setEscapeButton(QMessageBox.Cancel)
        reply = box.exec()
        self._confirming = False
        self._controls()
        if reply == QMessageBox.Save and self._capture_unchanged(captured):
            self._start('edit', captured=capture, positive=pos, negative=neg)

    def action_export_copy(self):
        captured = self._action_capture()
        if captured is None: return
        token, path, mode, capture, pos, neg = captured
        stem, extension = os.path.splitext(path)
        self._confirming = True
        self._controls()
        dest = QFileDialog.getSaveFileName(self.view, self.tr('meta.export.title', 'Export Copy'),
            stem+'_metadata'+extension, self.tr('tool.metadata.same_format', 'Source format (*{ext})').format(ext=extension))[0]
        self._confirming = False
        self._controls()
        if dest and self._capture_unchanged(captured):
            self._start('edit', captured=capture, positive=pos, negative=neg, destination=dest)

    def _action_capture(self, require_native=False):
        if self._closed or self._busy or self._confirming or not self.active_path or self._capture is None:
            return None
        if self.current_mode != self.MODE_EDITOR or self.context.get('data_service') is None:
            return None
        if require_native and not self._capture.get('editable_original'):
            return None
        pos, neg = self.txt_pos.toPlainText(), self.txt_neg.toPlainText()
        if len(pos) > 65536 or len(neg) > 65536:
            self._notice('prompt_limit', 'Each prompt is limited to 65,536 characters.', danger=True)
            return None
        return self._token, self.active_path, self.current_mode, self._capture, pos, neg

    def _capture_unchanged(self, captured):
        return not self._closed and not self._busy and self.view is not None and isValid(self.view) and captured == (
            self._token, self.active_path, self.current_mode, self._capture,
            self.txt_pos.toPlainText(), self.txt_neg.toPlainText())

    def _cancel_job(self):
        if self._worker is not None: self._worker.stop()

    def _start(self, operation, **arguments):
        if self._closed or self._busy or self._worker is not None: return
        self._token += 1
        self._busy = True
        self._pending_result = None
        worker = MetadataWorker(self.context.get('data_service'), self.context.get('metadata_journal_root'),
            operation, self._token, **arguments)
        self._worker = worker
        self._workers.add(worker)
        _LIVE_WORKERS.add(worker)
        worker.finished_signal.connect(self._receive)
        worker.finished.connect(self._worker_finished)
        worker.finished.connect(lambda worker=worker: (_LIVE_WORKERS.discard(worker), worker.deleteLater()))
        self._notice('working', 'Reading or verifying metadata…')
        self._controls()
        worker.start()

    def _receive(self, token, operation, result):
        if token == self._token and not self._closed:
            self._pending_result = token, operation, result

    def _worker_finished(self):
        worker = self.sender()
        self._workers.discard(worker)
        if worker is not self._worker: return
        self._worker = None
        self._busy = False
        pending, self._pending_result = self._pending_result, None
        if self._closed or self.view is None or not isValid(self.view): return
        if pending is not None and pending[0] == self._token:
            _, operation, result = pending
            if result['status'] == 'complete':
                if operation == 'load':
                    self._capture = {key: value for key, value in result.items() if key != 'image'}
                    self.image_label.set_image(result['image'])
                    meta = result['metadata']
                    self.txt_pos.setPlainText(str(meta.get('positive', '')))
                    self.txt_neg.setPlainText(str(meta.get('negative', '')))
                    fields = {key: meta.get(key, '') for key in ('tool', 'model', 'sampler', 'steps', 'cfg', 'seed')}
                    fields.update(meta.get('stats', {}))
                    self.txt_tech.setPlainText(json.dumps(fields, ensure_ascii=False, indent=2))
                    if result.get('native'):
                        if result.get('editable_original'):
                            self._notice('ready', 'File and indexed copy loaded. Unsaved edits remain local.')
                        else:
                            self._notice('protected', 'This original requires review or is protected. You can read history or export to a fresh path.', danger=True)
                    else:
                        self._notice('external', 'This path has no reviewed active native identity. Export to a fresh path; index or review the copy before editing the original.', danger=True)
                elif operation == 'history':
                    self._history_after, self._history_more = result['next_after_id'], result['has_more']
                    self.txt_tech.setPlainText(result['text'])
                    self._notice('history_ready', 'Previous generations for this copy ID; current curation remains separate.')
                elif operation == 'edit':
                    self._capture = None  # No reusar evidencia anterior al cambio físico.
                    self._history_more = False
                    self._notice('saved', 'Verified file saved. Reload the current image before another edit.')
                    self.txt_tech.setPlainText(self.tr('tool.metadata.receipt', 'Output: {dest}\nRecovery journal: {journal}').format(
                        dest=result['destination'], journal=result['journal']))
            else:
                if operation in ('load', 'edit'):
                    self._capture = None
                    self._history_more = False
                if result['status'] == 'partial':
                    self._notice('partial', 'The file was published, but the operation needs review. Do not repeat the save; inspect the recovery journal.', danger=True)
                else:
                    self._notice('failed', 'The operation could not complete. Review the details or reload the image.', danger=True)
                self.txt_tech.setPlainText(self.tr('tool.metadata.failure_details', 'Reason: {reason}\nRecovery journal: {journal}').format(
                    reason=result.get('error', ''), journal=result.get('journal', '')))
                if operation == 'edit':
                    self.txt_tech.append(self.tr('tool.metadata.effect_details', 'File published: {published}\nNative commit confirmed: {committed}').format(
                        published=self.tr('tool.metadata.yes', 'Yes') if result.get('published') else self.tr('tool.metadata.no', 'No'),
                        committed=self.tr('tool.metadata.yes', 'Yes') if result.get('native_committed') else self.tr('tool.metadata.no', 'No')))
        self._controls()

    def _read_history(self, next_page=False):
        if self._closed or self._busy or self._confirming or not self._capture or not self._capture.get('native'): return
        if next_page and not self._history_more: return
        self._start('history', copy_id=self._capture['native']['state']['file']['id'],
            after_id=self._history_after if next_page else 0, labels={key: self.tr('tool.metadata.'+key, default) for key, default in (
                ('history_record', 'Record {id} — {date}'), ('history_native', 'Previous generation in the database'),
                ('history_observed', 'Generation read from the previous file'), ('history_empty', 'No generation history recorded.'),
                ('field_tool', 'Tool'), ('field_model', 'Model'), ('field_positive', 'Positive prompt'),
                ('field_negative', 'Negative prompt'), ('field_seed', 'Seed'), ('field_width', 'Width'),
                ('field_height', 'Height'), ('field_sampler', 'Sampler'), ('field_steps', 'Steps'),
                ('field_cfg', 'CFG'), ('field_vae', 'VAE'), ('field_loras', 'LoRAs'))})

    def _notice(self, key, default, *, danger=False):
        if self._closed or not hasattr(self, 'lbl_operation') or not isValid(self.lbl_operation): return
        self.lbl_operation.setText(self.tr('tool.metadata.'+key, default))
        self.lbl_operation.setStyleSheet('color: '+self._color('accent_warning' if danger else 'text_secondary')+';')

    def _controls(self):
        if self._closed or self.view is None or not isValid(self.view): return
        idle = not self._busy and not self._confirming
        loaded = self._capture is not None
        indexed = loaded and self._capture.get('native') is not None
        editing = self.current_mode == self.MODE_EDITOR
        service = self.context.get('data_service') is not None
        for control in (self.btn_mode_view, self.btn_mode_edit, self.btn_open, self.image_label):
            control.setEnabled(idle)
        self.btn_prev.setEnabled(idle and self.current_index > 0)
        self.btn_next.setEnabled(idle and self.current_index < len(self.image_list)-1)
        self.btn_save.setEnabled(idle and editing and indexed and service and self._capture.get('editable_original', False))
        self.btn_export.setEnabled(idle and editing and loaded and service)
        self.btn_reload.setEnabled(idle and self.active_path is not None)
        self.btn_history.setEnabled(idle and indexed and service)
        self.btn_history_next.setEnabled(idle and indexed and service and self._history_more)
        self.btn_cancel.setEnabled(self._busy)
        self.txt_pos.setReadOnly(not (idle and editing and loaded))
        self.txt_neg.setReadOnly(not (idle and editing and loaded))

    def run_headless(self, paths: list = None):
        """Carga una lista de imágenes en modo programático (desde Librarian u otros módulos)."""
        if paths:
            self.load_image_set(paths)
