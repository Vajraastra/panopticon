from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, 
                               QFileDialog, QMessageBox, QProgressBar, QTextEdit, QApplication, 
                               QSplitter, QFrame, QSizePolicy)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPixmap, QIcon, QPainter
from core.base_module import BaseModule
from .logic.reader import UniversalParser
from .logic.modifier import modify_metadata, get_export_path
import os

class ResponsiveImageLabel(QLabel):
    """A QLabel that paints its pixmap scaled to fit, avoiding layout loops."""
    def __init__(self, text="No Image Loaded"):
        super().__init__(text)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background-color: #050505; border: 1px solid #222; border-radius: 8px;")
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self._pixmap = None

    def set_image(self, path):
        if not path or not os.path.exists(path):
            self._pixmap = None
            self.setText(LocaleManager().tr("meta.no_file", "No Image Loaded"))
            self.update() # Trigger repaint
            return
            
        self._pixmap = QPixmap(path)
        self.setText("") # Clear text if image loaded
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._pixmap or self._pixmap.isNull():
            return
            
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        
        # Calculate scaling to fit while keeping aspect ratio
        s = self._pixmap.size()
        s.scale(self.size(), Qt.KeepAspectRatio)
        
        # Center the image
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
        
        return self.view

    def _create_sidebar(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)
        
        from core.theme import Theme
        accent = "#00ffcc"

        # Title
        lbl_title = QLabel(self.tr("meta.title", "🔍 METADATA HUB"))
        lbl_title.setStyleSheet(f"color: {accent}; font-weight: bold; font-size: 14px;")
        layout.addWidget(lbl_title)
        
        lbl_desc = QLabel(self.tr("meta.desc", "Read, edit and clean image metadata."))
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(lbl_desc)
        
        layout.addSpacing(10)

        lbl = QLabel(self.tr("meta.mode", "🛠️ MODE"))
        lbl.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-weight: bold; font-size: 12px;")
        layout.addWidget(lbl)

        # Mode Selector (Toggle)
        self.btn_mode_view = QPushButton(self.tr("meta.viewer", "🔍 Viewer Mode"))
        self.btn_mode_view.setCheckable(True)
        self.btn_mode_view.setChecked(True)
        self.btn_mode_view.setFixedHeight(38)
        self.btn_mode_view.clicked.connect(lambda: self.set_mode(self.MODE_VIEWER))
        layout.addWidget(self.btn_mode_view)

        self.btn_mode_edit = QPushButton(self.tr("meta.editor", "✍️ Editor Mode"))
        self.btn_mode_edit.setCheckable(True)
        self.btn_mode_edit.setFixedHeight(38)
        self.btn_mode_edit.clicked.connect(lambda: self.set_mode(self.MODE_EDITOR))
        layout.addWidget(self.btn_mode_edit)
        
        # Style for modes
        self._update_mode_styles()

        layout.addSpacing(10)
        lbl_act = QLabel(self.tr("meta.actions", "⚡ ACTIONS"))
        lbl_act.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-weight: bold; font-size: 12px;")
        layout.addWidget(lbl_act)

        self.btn_save = QPushButton(self.tr("meta.save", "💾 Save Changes"))
        self.btn_save.setFixedHeight(40)
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.clicked.connect(self.action_save_current)
        self.btn_save.setStyleSheet(Theme.get_action_button_style(accent, "#000000"))
        layout.addWidget(self.btn_save)

        self.btn_export = QPushButton(self.tr("meta.export", "📂 Export Copy"))
        self.btn_export.setFixedHeight(36)
        self.btn_export.setCursor(Qt.PointingHandCursor)
        self.btn_export.clicked.connect(self.action_export_copy)
        self.btn_export.setStyleSheet(Theme.get_button_style("#555"))
        layout.addWidget(self.btn_export)

        layout.addStretch()
        
        lbl_info = QLabel(self.tr("meta.list", "IMAGE LIST"))
        lbl_info.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_info)
        
        self.lbl_carousel_stats = QLabel(self.tr("meta.stats", "{current} / {total} images").format(current=0, total=0))
        self.lbl_carousel_stats.setStyleSheet(f"color: {Theme.TEXT_DIM};")
        layout.addWidget(self.lbl_carousel_stats)
        
        return container

    def _create_content(self) -> QWidget:
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
        self.btn_open.clicked.connect(self.open_files_dialog)
        self.btn_open.setStyleSheet("background: #333; color: white; padding: 8px 15px; border-radius: 5px;")
        top_bar.addWidget(self.btn_open)
        top_bar.addStretch()
        
        self.lbl_file_name = QLabel(self.tr("meta.no_file", "No file selected"))
        self.lbl_file_name.setStyleSheet("color: #888; font-family: Consolas;")
        top_bar.addWidget(self.lbl_file_name)
        viewer_layout.addLayout(top_bar)
        
        self.image_label = ResponsiveImageLabel()
        viewer_layout.addWidget(self.image_label, 1)
        
        # Carousel Controls
        carousel_layout = QHBoxLayout()
        self.btn_prev = QPushButton(self.tr("meta.prev", "◀ Previous"))
        self.btn_prev.clicked.connect(self.go_prev)
        self.btn_prev.setStyleSheet("padding: 10px; background: #222; color: #ddd; border-radius: 5px;")
        
        self.btn_next = QPushButton(self.tr("meta.next", "Next ▶"))
        self.btn_next.clicked.connect(self.go_next)
        self.btn_next.setStyleSheet("padding: 10px; background: #222; color: #ddd; border-radius: 5px;")
        
        carousel_layout.addWidget(self.btn_prev)
        carousel_layout.addStretch()
        carousel_layout.addWidget(self.btn_next)
        viewer_layout.addLayout(carousel_layout)
        
        self.splitter.addWidget(viewer_container)
        
        # Right: Editor
        editor_container = QFrame()
        editor_container.setStyleSheet("background-color: #111; border-radius: 10px;")
        editor_layout = QVBoxLayout(editor_container)
        editor_layout.setSpacing(10)
        
        lbl_pos = QLabel(self.tr("meta.positive", "✨ Positive Prompt:"))
        lbl_pos.setStyleSheet("font-weight: bold; color: #aaffaa; font-size: 13px;")
        editor_layout.addWidget(lbl_pos)
        
        self.txt_pos = QTextEdit()
        self.txt_pos.setPlaceholderText(self.tr("meta.placeholder.pos", "Enter positive prompt..."))
        self.txt_pos.setStyleSheet("background: #0a0a0a; color: #eee; border: 1px solid #333; border-radius: 5px; font-family: Consolas;")
        editor_layout.addWidget(self.txt_pos, 2)
        
        lbl_neg = QLabel(self.tr("meta.negative", "🚫 Negative Prompt:"))
        lbl_neg.setStyleSheet("font-weight: bold; color: #ffaaaa; font-size: 13px;")
        editor_layout.addWidget(lbl_neg)
        
        self.txt_neg = QTextEdit()
        self.txt_neg.setPlaceholderText(self.tr("meta.placeholder.neg", "Enter negative prompt..."))
        self.txt_neg.setStyleSheet("background: #0a0a0a; color: #eee; border: 1px solid #333; border-radius: 5px; font-family: Consolas;")
        editor_layout.addWidget(self.txt_neg, 1)
        
        lbl_tech = QLabel(self.tr("meta.tech_meta", "⚙️ Technical Metadata:"))
        lbl_tech.setStyleSheet("font-weight: bold; color: #888; font-size: 12px;")
        editor_layout.addWidget(lbl_tech)
        
        self.txt_tech = QTextEdit()
        self.txt_tech.setReadOnly(True)
        self.txt_tech.setStyleSheet("background: #050505; color: #666; border: none; font-size: 11px; font-family: Consolas;")
        editor_layout.addWidget(self.txt_tech, 2)
        
        self.splitter.addWidget(editor_container)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        
        layout.addWidget(self.splitter)
        
        return container

    def set_mode(self, mode):
        self.current_mode = mode
        is_edit = (mode == self.MODE_EDITOR)
        
        # Toggle read-only
        self.txt_pos.setReadOnly(not is_edit)
        self.txt_neg.setReadOnly(not is_edit)
        
        # Toggle button states
        self.btn_save.setEnabled(is_edit)
        
        # Update UI feedback
        self._update_mode_styles()
        
        # Visual cues on text fields
        if is_edit:
            self.txt_pos.setStyleSheet("background: #0a0a0a; color: #00ffcc; border: 1px solid #00ffcc; border-radius: 5px; font-family: Consolas;")
            self.txt_neg.setStyleSheet("background: #0a0a0a; color: #ff8888; border: 1px solid #ff8888; border-radius: 5px; font-family: Consolas;")
        else:
            self.txt_pos.setStyleSheet("background: #0a0a0a; color: #eee; border: 1px solid #333; border-radius: 5px; font-family: Consolas;")
            self.txt_neg.setStyleSheet("background: #0a0a0a; color: #eee; border: 1px solid #333; border-radius: 5px; font-family: Consolas;")

    def _update_mode_styles(self):
        view_active = (self.current_mode == self.MODE_VIEWER)
        
        self.btn_mode_view.setChecked(view_active)
        self.btn_mode_edit.setChecked(not view_active)
        
        active_style = "background-color: #333; color: #00ffcc; border: 1px solid #00ffcc; font-weight: bold; border-radius: 6px; text-align: left; padding: 5px;"
        inactive_style = "background-color: #1a1a1a; color: #666; border: 1px solid #222; border-radius: 6px; text-align: left; padding: 5px;"
        
        self.btn_mode_view.setStyleSheet(active_style if view_active else inactive_style)
        self.btn_mode_edit.setStyleSheet(inactive_style if view_active else active_style)

    def load_image_set(self, paths: list):
        """Standard integration from Librarian."""
        if not paths: return
        self.image_list = sorted(paths)
        self.current_index = 0
        self.display_current()

    def load_images(self, paths: list):
        """Alias for compatibility."""
        self.load_image_set(paths)

    def display_current(self):
        if not (0 <= self.current_index < len(self.image_list)):
            self.active_path = None
            self.image_label.set_image(None)
            self.lbl_file_name.setText("No file selected")
            self.clear_fields()
            return

        self.active_path = self.image_list[self.current_index]
        self.image_label.set_image(self.active_path)
        self.lbl_file_name.setText(os.path.basename(self.active_path))
        self.lbl_carousel_stats.setText(self.tr("meta.stats", "{current} / {total} images").format(current=self.current_index + 1, total=len(self.image_list)))
        
        # Parse Metadata
        result = UniversalParser.parse_image(self.active_path)
        self.txt_pos.setPlainText(result.get("positive", ""))
        self.txt_neg.setPlainText(result.get("negative", ""))
        
        # Tech details
        tech = f"Tool: {result.get('tool', 'Unknown')}\n"
        tech += f"Model: {result.get('model', '-')}\n"
        tech += f"Sampler: {result.get('sampler', '-')} | Steps: {result.get('steps', '-')} | CFG: {result.get('cfg', '-')}\n"
        tech += f"Size: {result.get('stats', {}).get('format', '-')} | {result.get('stats', {}).get('size', '-')}"
        self.txt_tech.setPlainText(tech)

        # Update Nav buttons
        self.btn_prev.setEnabled(self.current_index > 0)
        self.btn_next.setEnabled(self.current_index < len(self.image_list) - 1)

    def clear_fields(self):
        self.txt_pos.clear()
        self.txt_neg.clear()
        self.txt_tech.clear()

    def go_prev(self):
        if self.current_index > 0:
            self.current_index -= 1
            self.display_current()

    def go_next(self):
        if self.current_index < len(self.image_list) - 1:
            self.current_index += 1
            self.display_current()

    def open_files_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(None, self.tr("opt.load_images", "Select Images"), "", "Images (*.png *.jpg *.jpeg *.webp)")
        if files:
            self.load_image_set(files)


    def action_save_current(self):
        if not self.active_path: return
        pos = self.txt_pos.toPlainText().strip()
        neg = self.txt_neg.toPlainText().strip()
        
        # Safety Check: If fields are empty, are they sure they want to strip?
        if not pos and not neg:
            msg = self.tr("meta.warn.empty", "⚠️ IRREVERSIBLE ACTION DETECTED\n\nYou are about to save this image with ZERO metadata. This will permanently erase the prompts (the 'recipe') and AI generation data from the original file.\n\nThis cannot be undone. Are you absolutely sure?")
            reply = QMessageBox.critical(
                None, 
                self.tr("common.error", "DANGEROUS OPERATION"), 
                msg, 
                QMessageBox.Yes | QMessageBox.No, 
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return

        # For simplicity, we merge pos/neg if needed or follow SD standard (parameters)
        combined = f"{pos}\nNegative prompt: {neg}" if (pos or neg) else None
        
        success, msg = modify_metadata(self.active_path, self.active_path, metadata_text=combined)
        if success:
            QMessageBox.information(None, self.tr("common.success", "Success"), self.tr("meta.save.success", "Metadata updated successfully."))
            self.display_current()
        else:
            QMessageBox.critical(None, self.tr("common.error", "Error"), msg)

    def action_export_copy(self):
        if not self.active_path: return
        dest = QFileDialog.getSaveFileName(None, self.tr("meta.export.title", "Export Copy"), self.active_path, "Same as source (*.*)")[0]
        if dest:
            pos = self.txt_pos.toPlainText().strip()
            neg = self.txt_neg.toPlainText().strip()
            combined = f"{pos}\nNegative prompt: {neg}"
            
            success, msg = modify_metadata(self.active_path, dest, metadata_text=combined)
            if success:
                QMessageBox.information(None, self.tr("common.success", "Success"), self.tr("meta.export.success", "Exported to:\n{dest}").format(dest=dest))
            else:
                QMessageBox.critical(None, self.tr("common.error", "Error"), msg)
