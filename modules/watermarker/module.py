from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, 
                               QScrollArea, QFrame, QFileDialog, QMessageBox, QProgressBar,
                               QComboBox, QSlider, QSizePolicy, QGridLayout)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QPixmap, QImage, QPainter
from core.base_module import BaseModule
from core.theme import Theme
from .logic.watermarker import process_image as watermark_image, get_export_path as watermark_export_path
import os

class WatermarkerModule(BaseModule):
    def __init__(self):
        super().__init__()
        self.view = None
        self.source_image = None
        self.watermark_asset = None
        self.logo_asset = None
        self.last_dir = os.path.expanduser("~")
        
    @property
    def name(self):
        return "Watermarker"

    @property
    def description(self):
        return "Individual image watermarking and logo placement."

    @property
    def icon(self):
        return "🎨"

    @property
    def accent_color(self):
        return "#50fa7b"

    def get_view(self) -> QWidget:
        if self.view: return self.view
        
        content = self._create_content()
        sidebar = self._create_sidebar()
        
        from core.components.standard_layout import StandardToolLayout
        self.view = StandardToolLayout(
            content,
            sidebar_widget=sidebar,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        return self.view

    def _create_sidebar(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(15)

        # 1. Image
        lbl_img = QLabel("🖼️ 1. BASE IMAGE")
        lbl_img.setStyleSheet(f"color: {self.accent_color}; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_img)

        self.btn_load_image = QPushButton("📂 Load Image")
        self.btn_load_image.clicked.connect(self.load_source_image)
        layout.addWidget(self.btn_load_image)

        self.lbl_image_status = QLabel("No image loaded")
        self.lbl_image_status.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(self.lbl_image_status)

        layout.addSpacing(5)

        # 2. Marca de Agua (Watermark)
        lbl_wm_section = QLabel("🎨 2. MARCA DE AGUA")
        lbl_wm_section.setStyleSheet(f"color: {self.accent_color}; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_wm_section)

        self.btn_load_watermark = QPushButton("📂 Cargar Marca de Agua")
        self.btn_load_watermark.clicked.connect(self.load_watermark_asset)
        layout.addWidget(self.btn_load_watermark)

        self.lbl_watermark_status = QLabel("Ninguna")
        self.lbl_watermark_status.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(self.lbl_watermark_status)

        layout.addWidget(QLabel("Ángulo de Rotación:"))
        self.combo_angle = QComboBox()
        self.combo_angle.addItems(["0°", "45°", "90°", "135°", "180°", "225°", "270°", "315°"])
        self.combo_angle.setStyleSheet(Theme.get_input_style(self.accent_color))
        self.combo_angle.currentIndexChanged.connect(self.generate_preview)
        layout.addWidget(self.combo_angle)

        layout.addWidget(QLabel("Escala de la Marca:"))
        self.slider_scale = QSlider(Qt.Horizontal)
        self.slider_scale.setRange(10, 200)
        self.slider_scale.setValue(100)
        self.slider_scale.valueChanged.connect(self.generate_preview)
        layout.addWidget(self.slider_scale)

        layout.addWidget(QLabel("Opacidad:"))
        self.slider_opacity = QSlider(Qt.Horizontal)
        self.slider_opacity.setRange(0, 100)
        self.slider_opacity.setValue(30)
        self.slider_opacity.valueChanged.connect(self.generate_preview)
        layout.addWidget(self.slider_opacity)

        layout.addSpacing(15)

        # 3. Logo
        lbl_logo_section = QLabel("🏷️ 3. LOGOTIPO")
        lbl_logo_section.setStyleSheet(f"color: {self.accent_color}; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_logo_section)

        self.btn_load_logo = QPushButton("📂 Cargar Logo")
        self.btn_load_logo.clicked.connect(self.load_logo_asset)
        layout.addWidget(self.btn_load_logo)

        self.lbl_logo_status = QLabel("Ninguno")
        self.lbl_logo_status.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(self.lbl_logo_status)

        layout.addWidget(QLabel("Posición del Logo:"))
        self.combo_logo_pos = QComboBox()
        self.combo_logo_pos.addItems(["Top-Right", "Top-Left", "Bottom-Right", "Bottom-Left"])
        self.combo_logo_pos.setStyleSheet(Theme.get_input_style(self.accent_color))
        self.combo_logo_pos.currentIndexChanged.connect(self.generate_preview)
        layout.addWidget(self.combo_logo_pos)

        layout.addWidget(QLabel("Tamaño del Logo (px):"))
        self.slider_logo_size = QSlider(Qt.Horizontal)
        self.slider_logo_size.setRange(50, 500)
        self.slider_logo_size.setValue(150)
        self.slider_logo_size.valueChanged.connect(self.generate_preview)
        layout.addWidget(self.slider_logo_size)

        layout.addStretch()
        
        self.btn_save = QPushButton("💾 SAVE COPY")
        self.btn_save.clicked.connect(self.save_copy)
        self.btn_save.setMinimumHeight(45)
        self.btn_save.setStyleSheet(f"background-color: {self.accent_color}; color: black; font-weight: bold; border-radius: 6px;")
        layout.addWidget(self.btn_save)

        return container

    def _create_content(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        
        # Preview Area
        preview_frame = QFrame()
        preview_frame.setStyleSheet("background-color: #050505; border: 1px solid #222; border-radius: 10px;")
        preview_layout = QVBoxLayout(preview_frame)
        
        self.lbl_preview = QLabel("Drop or Load Image to Begin")
        self.lbl_preview.setAlignment(Qt.AlignCenter)
        self.lbl_preview.setStyleSheet("color: #444; font-size: 14px; font-weight: bold;")
        preview_layout.addWidget(self.lbl_preview)
        
        layout.addWidget(preview_frame)
        
        # Integration with drag and drop
        container.setAcceptDrops(True)
        container.dragEnterEvent = self.dragEnterEvent
        container.dropEvent = self.dropEvent
        
        return container

    def load_source_image(self):
        path, _ = QFileDialog.getOpenFileName(None, "Select Image", self.last_dir, "Images (*.png *.jpg *.jpeg *.webp)")
        if path:
            self.source_image = path
            self.lbl_image_status.setText(os.path.basename(path))
            self.last_dir = os.path.dirname(path)
            self.generate_preview()

    def load_watermark_asset(self):
        path, _ = QFileDialog.getOpenFileName(None, "Select Watermark", self.last_dir, "Images (*.png *.jpg *.svg)")
        if path:
            self.watermark_asset = path
            self.lbl_watermark_status.setText(os.path.basename(path))
            self.generate_preview()

    def load_logo_asset(self):
        path, _ = QFileDialog.getOpenFileName(None, "Select Logo", self.last_dir, "Images (*.png *.jpg *.svg)")
        if path:
            self.logo_asset = path
            self.lbl_logo_status.setText(os.path.basename(path))
            self.generate_preview()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        image_paths = [p for p in paths if p.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))]
        if image_paths:
            self.source_image = image_paths[0]
            self.lbl_image_status.setText(os.path.basename(self.source_image))
            self.generate_preview()

    def generate_preview(self):
        if not self.source_image:
            return
            
        if not self.watermark_asset and not self.logo_asset:
            pix = QPixmap(self.source_image)
            self.lbl_preview.setPixmap(pix.scaled(self.lbl_preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            return
            
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
            
        success, msg = watermark_image(
            self.source_image, tmp_path, 
            watermark_path=self.watermark_asset,
            logo_path=self.logo_asset,
            wm_angle=int(self.combo_angle.currentText().replace("°", "")),
            wm_scale=self.slider_scale.value() / 100.0,
            wm_opacity=self.slider_opacity.value() / 100.0,
            logo_position=self.combo_logo_pos.currentText().lower(),
            logo_size=self.slider_logo_size.value()
        )
        
        if success:
            pix = QPixmap(tmp_path)
            self.lbl_preview.setPixmap(pix.scaled(self.lbl_preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        
        try: os.unlink(tmp_path)
        except: pass

    def save_copy(self):
        if not self.source_image:
            QMessageBox.warning(None, "No Image", "Please load an image first.")
            return
            
        if not self.watermark_asset and not self.logo_asset:
            QMessageBox.warning(None, "No Assets", "Please load a watermark or logo.")
            return

        out_path, _ = QFileDialog.getSaveFileName(None, "Save Watermarked Image", self.last_dir, "Images (*.png *.jpg *.jpeg *.webp)")
        if not out_path: return
        
        success, msg = watermark_image(
            self.source_image, out_path,
            watermark_path=self.watermark_asset,
            logo_path=self.logo_asset,
            wm_angle=int(self.combo_angle.currentText().replace("°", "")),
            wm_scale=self.slider_scale.value() / 100.0,
            wm_opacity=self.slider_opacity.value() / 100.0,
            logo_position=self.combo_logo_pos.currentText().lower(),
            logo_size=self.slider_logo_size.value()
        )
        
        if success:
            QMessageBox.information(None, "Saved", f"Image saved successfully to:\n{out_path}")
        else:
            QMessageBox.critical(None, "Error", f"Failed to save image: {msg}")

    def on_load(self, context):
        super().on_load(context)
        
    def load_image_set(self, paths: list):
        if paths:
            self.source_image = paths[0]
            self.lbl_image_status.setText(os.path.basename(self.source_image))
            self.generate_preview()
