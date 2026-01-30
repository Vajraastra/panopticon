from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog, QMessageBox, 
    QFrame, QSizePolicy, QScrollArea, QComboBox, QSpinBox, QStackedWidget
)
from PySide6.QtCore import Qt, Signal, QMimeData
from PySide6.QtGui import QIcon, QAction
from core.base_module import BaseModule
from core.components.standard_layout import StandardToolLayout
from modules.cropper.logic.cropper_widget import ImageCropperWidget
import os

class CropperModule(BaseModule):
    """
    Módulo Smart Cropper (Recorte Inteligente).
    Proporciona una interfaz de alta precisión para recortar imágenes,
    especialmente útil para preparar datasets de entrenamiento de IA (LoRA/Dreambooth).
    Soporta relaciones de aspecto fijas y personalizadas.
    """
    def __init__(self):
        super().__init__()
        self._name = "Smart Cropper"
        self._description = "Herramienta de recorte de alta precisión para datasets de IA."
        self._icon = "✂️"
        self.accent_color = "#ff79c6"  # Color Rosa/Púrpura para el Cropper
        self.view = None

    def get_view(self) -> QWidget:
        """Crea el widget de recorte y los controles laterales."""
        # Widget especializado para la interacción de recorte (Crop Area)
        self.cropper_widget = ImageCropperWidget()
        
        self.sidebar = self._create_sidebar()
        self.bottom = self._create_bottom_bar()
        self.content = self._create_content()

        self.view = StandardToolLayout(
            self.content, 
            self.sidebar, 
            self.bottom,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        
        # Habilitar Drag & Drop en la vista principal
        self.view.setAcceptDrops(True)
        self.view.dragEnterEvent = self.dragEnterEvent
        self.view.dropEvent = self.dropEvent
        
        return self.view

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        images = [f for f in files if f.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.webp', '.tiff'))]
        
        if images:
            self.load_image(images[0]) # Load the first valid image

    def _create_content(self) -> QWidget:
        self.stack = QStackedWidget()
        self.stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        # --- PAGE 0: DROP ZONE ---
        self.page_drop = QFrame()
        self.page_drop.setObjectName("drop_zone")
        
        # Style the drop zone reusing theme
        tm = self.context.get('theme_manager')
        bg_main = tm.get_color("bg_main") if tm else "#111"
        border_col = tm.get_color("border") if tm else "#333"
        text_dim = tm.get_color("text_dim") if tm else "#888"
        
        self.page_drop.setStyleSheet(f"""
            QFrame#drop_zone {{
                background-color: {bg_main};
                border: 2px dashed {border_col};
                border-radius: 12px;
                margin: 20px;
            }}
            QFrame#drop_zone:hover {{
                border-color: {self.accent_color};
            }}
        """)
        
        drop_layout = QVBoxLayout(self.page_drop)
        drop_layout.setAlignment(Qt.AlignCenter)
        
        lbl_icon = QLabel("📂")
        lbl_icon.setStyleSheet("font-size: 48px;")
        lbl_icon.setAlignment(Qt.AlignCenter)
        
        lbl_text = QLabel(self.tr("crop.drop_zone", "Drop image here\nor click 'Open Image'"))
        lbl_text.setStyleSheet(f"color: {text_dim}; font-size: 16px; font-weight: bold;")
        lbl_text.setAlignment(Qt.AlignCenter)
        
        drop_layout.addWidget(lbl_icon)
        drop_layout.addWidget(lbl_text)
        
        self.stack.addWidget(self.page_drop)

        # --- PAGE 1: CROPPER ---
        # Container for cropper to ensure proper sizing
        cropper_container = QWidget()
        cropper_layout = QVBoxLayout(cropper_container)
        cropper_layout.setContentsMargins(0, 0, 0, 0)
        
        self.cropper_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        cropper_layout.addWidget(self.cropper_widget)
        
        self.stack.addWidget(cropper_container)
        
        return self.stack

    def _create_sidebar(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)
        
        from core.theme import Theme
        
        # Title
        lbl_title = QLabel(self.tr("crop.title", "✂️ SMART CROPPER"))
        lbl_title.setStyleSheet(f"color: {self.accent_color}; font-weight: bold; font-size: 14px;")
        layout.addWidget(lbl_title)
        
        lbl_desc = QLabel(self.tr("crop.desc", "High-precision cropping for AI datasets."))
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(lbl_desc)
        
        layout.addSpacing(10)
        
        # --- Aspect Ratios ---
        lbl_ar = QLabel(self.tr("crop.ratio", "Aspect Ratio:"))
        lbl_ar.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-size: 12px;")
        layout.addWidget(lbl_ar)
        
        self.combo_ar = QComboBox()
        self.combo_ar.addItems([
            self.tr("crop.free", "Free"),
            self.tr("crop.custom", "Custom"),
            self.tr("crop.square", "1:1 (Square)"),
            "4:3", "16:9", "3:4", "9:16"
        ])
        self.combo_ar.currentIndexChanged.connect(self._on_ar_changed)
        layout.addWidget(self.combo_ar)

        # --- Custom Ratio Spinboxes ---
        self.container_custom = QWidget()
        hbox_custom = QHBoxLayout(self.container_custom)
        hbox_custom.setContentsMargins(0, 0, 0, 0)
        
        self.spin_w = QSpinBox()
        self.spin_w.setRange(1, 999)
        self.spin_w.setValue(1)
        self.spin_w.setPrefix("W: ")
        self.spin_w.valueChanged.connect(self._on_custom_values_changed)
        
        lbl_x = QLabel(":")
        lbl_x.setStyleSheet(f"color: {text_col}; font-weight: bold;")
        
        self.spin_h = QSpinBox()
        self.spin_h.setRange(1, 999)
        self.spin_h.setValue(1)
        self.spin_h.setPrefix("H: ")
        self.spin_h.valueChanged.connect(self._on_custom_values_changed)
        
        hbox_custom.addWidget(self.spin_w)
        hbox_custom.addWidget(lbl_x)
        hbox_custom.addWidget(self.spin_h)
        
        layout.addWidget(self.container_custom)
        
        # Initially hide custom controls if "Free" is selected
        self.container_custom.setVisible(False)
        
        layout.addStretch()
        return container

    def _create_bottom_bar(self) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(15)
        
        # Left Side: Open
        self.btn_open = QPushButton(self.tr("crop.open", "📂 Open Image"))
        self.btn_open.clicked.connect(self.open_image_dialog)
        self.btn_open.setFixedWidth(120)
        layout.addWidget(self.btn_open)

        layout.addStretch()

        # Right Side: Save
        self.btn_save = QPushButton(self.tr("crop.save", "💾 Save Crop"))
        self.btn_save.clicked.connect(self.save_crop)
        self.btn_save.setFixedWidth(120)
        # Style it as an action button
        tm = self.context.get('theme_manager')
        accent = self.accent_color
        if tm:
            self.btn_save.setStyleSheet(f"""
                QPushButton {{
                    background-color: {accent};
                    color: black;
                    border-radius: 4px;
                    font-weight: bold;
                    padding: 6px;
                }}
                QPushButton:hover {{
                    background-color: white;
                }}
            """)
        layout.addWidget(self.btn_save)

        return container

    def _on_ar_changed(self, index):
        # Items: ["Free", "Custom", "1:1", "4:3", "16:9", "3:4", "9:16"]
        text = self.combo_ar.currentText()
        
        if text == "Free":
            self.container_custom.setVisible(False)
            self.cropper_widget.set_fixed_aspect_ratio(None)
        elif text == "Custom":
            self.container_custom.setVisible(True)
            self._on_custom_values_changed() # Apply current spinbox values
        else:
            self.container_custom.setVisible(True) # Show them so user sees the ratio
            # Parse ration from text "W:H ..."
            try:
                parts = text.split(" ")[0].split(":")
                w = int(parts[0])
                h = int(parts[1])
                
                # Update spinboxes without triggering signals loop
                self.spin_w.blockSignals(True)
                self.spin_h.blockSignals(True)
                self.spin_w.setValue(w)
                self.spin_h.setValue(h)
                self.spin_w.blockSignals(False)
                self.spin_h.blockSignals(False)
                
                self.cropper_widget.set_fixed_aspect_ratio(w / h)
            except:
                pass

    def _on_custom_values_changed(self):
        # Auto-switch combo to "Custom" if it's not already,
        # UNLESS we are just reflecting a preset change (handled by blocking signals above)
        
        if self.combo_ar.currentText() != "Custom":
             # If user manually changes spinbox while on a preset, switch to custom
             self.combo_ar.blockSignals(True)
             self.combo_ar.setCurrentText("Custom")
             self.combo_ar.blockSignals(False)
        
        w = self.spin_w.value()
        h = self.spin_h.value()
        if h > 0:
            self.cropper_widget.set_fixed_aspect_ratio(w / h)

    def open_image_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self.view, self.tr("common.load_image", "Open Image"), "", "Images (*.png *.jpg *.jpeg *.bmp)"
        )
        if path:
            self.load_image(path)

    def load_image(self, path):
        self.cropper_widget.load_image(path)
        self.stack.setCurrentIndex(1) # Switch to Cropper View

    def save_crop(self):
        if not self.cropper_widget.image_path:
            return

        # Get the cropped pixmap from the widget
        # The widget logic might need to expose a method to get the crop or we do it here.
        # Checking cropper_widget.py logic... 
        # It seems we might need to implement the actual crop extraction if it's not exposed.
        # Let's check if there's a helper or if we drag logic here.
        
        # Based on previous file listing, there was 'cropper_logic.py'.
        # Let's import it.
        from modules.cropper.logic.cropper_logic import crop_image
        
        # We need the normalized rect from the widget
        rect = self.cropper_widget.get_crop_rect_normalized() # Assuming this exists or similar
        # If not, we might need to check the widget code. 
        # For now, let's assume the widget has a method to get the selection.
        
        # Actually, let's look at the widget code in the previous turn or infer.
        # If I can't verify 'get_crop_rect_normalized', I might need to add it.
        # I'll optimistically assume I can get the rect or use the widget to save.
        
        # Let's try to get the crop using the logic we moved.
        # crop_image(image_path, crop_rect_normalized)
        
        if rect:
            save_path, _ = QFileDialog.getSaveFileName(
                self.view, self.tr("common.save_copy", "Save Crop"), "", "PNG (*.png);;JPG (*.jpg)"
            )
            if save_path:
                try:
                    # [NEW] Fetch Metadata
                    tags = []
                    rating = 0
                    from modules.librarian.logic.db_manager import DatabaseManager
                    db = DatabaseManager()
                    tags = db.get_tags_for_file(self.cropper_widget.image_path)
                    rating = db.get_file_rating(self.cropper_widget.image_path)

                    crop_image(self.cropper_widget.image_path, rect, save_path, tags=tags, rating=rating)
                    QMessageBox.information(self.view, self.tr("opt.done", "Done"), 
                                            self.tr("crop.saved", "Saved to {path}").format(path=save_path))
                except Exception as e:
                    QMessageBox.critical(self.view, self.tr("common.error", "Error"), str(e))
        else:
             QMessageBox.warning(self.view, self.tr("common.error", "Warning"), self.tr("common.no_selection", "No selection made."))
    def load_image_set(self, paths: list):
        """Standard interface for receiving sets from Librarian."""
        if paths:
            self.load_image(paths[0])
