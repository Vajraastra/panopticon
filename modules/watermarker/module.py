import os
import logging
import tempfile
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout,
                               QScrollArea, QFrame, QFileDialog, QMessageBox, QProgressBar,
                               QComboBox, QSlider, QSizePolicy, QGridLayout)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QPixmap, QImage, QPainter

from core.base_module import BaseModule
from core.theme import Theme
from core.components.standard_layout import StandardToolLayout
from .logic.watermarker import process_image as watermark_image, get_export_path as watermark_export_path


class WatermarkerModule(BaseModule):
    def __init__(self):
        super().__init__()
        self._name = "Watermarker"
        self._description = "Protect images with repeating watermark patterns and logos."
        self._icon = "🎨"
        self.accent_color = "#50fa7b"

        self.view = None
        self.source_image = None
        self.watermark_asset = None
        self.logo_asset = None
        self.last_dir = os.path.expanduser("~")

    def get_view(self) -> QWidget:
        if self.view:
            return self.view

        content = self._create_content()
        sidebar = self._create_sidebar()

        self.view = StandardToolLayout(
            content,
            sidebar_widget=sidebar,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        return self.view

    def _create_sidebar(self) -> QWidget:
        theme    = self.context.get('theme_manager') if hasattr(self, 'context') else None
        text_dim = theme.get_color('text_dim')       if theme else Theme.TEXT_DIM
        text_sec = theme.get_color('text_secondary') if theme else Theme.TEXT_SECONDARY

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        lbl_title = QLabel(self.tr("wm.title", "🎨 WATERMARKER"))
        lbl_title.setStyleSheet(f"color: {self.accent_color}; font-weight: bold; font-size: 14px;")
        layout.addWidget(lbl_title)

        lbl_desc = QLabel(self.tr("wm.desc", "Protect images with watermarks and logos."))
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"color: {text_dim}; font-size: 11px;")
        layout.addWidget(lbl_desc)

        layout.addSpacing(10)

        # 1. Base image
        lbl_img = QLabel(self.tr("wm.section.base", "🖼️ 1. BASE IMAGE"))
        lbl_img.setStyleSheet(f"color: {text_sec}; font-weight: bold; font-size: 12px;")
        layout.addWidget(lbl_img)

        self.btn_load_image = QPushButton(self.tr("common.load_image", "📂 Load Image"))
        self.btn_load_image.setStyleSheet(Theme.get_action_button_style(self.accent_color, "#000000"))
        self.btn_load_image.setFixedHeight(36)
        self.btn_load_image.clicked.connect(self.load_source_image)
        layout.addWidget(self.btn_load_image)

        self.lbl_image_status = QLabel(self.tr("wm.status.no_img", "No image loaded"))
        self.lbl_image_status.setStyleSheet(f"color: {text_dim}; font-size: 10px;")
        layout.addWidget(self.lbl_image_status)

        layout.addSpacing(10)

        # 2. Watermark
        lbl_wm_section = QLabel(self.tr("wm.section.wm", "🎨 2. WATERMARK"))
        lbl_wm_section.setStyleSheet(f"color: {text_sec}; font-weight: bold; font-size: 12px;")
        layout.addWidget(lbl_wm_section)

        self.btn_load_watermark = QPushButton(self.tr("wm.load_wm", "📂 Load Watermark"))
        self.btn_load_watermark.setStyleSheet(Theme.get_button_style("#555"))
        self.btn_load_watermark.setFixedHeight(36)
        self.btn_load_watermark.clicked.connect(self.load_watermark_asset)
        layout.addWidget(self.btn_load_watermark)

        self.lbl_watermark_status = QLabel(self.tr("wm.status.none", "None"))
        self.lbl_watermark_status.setStyleSheet(f"color: {text_dim}; font-size: 10px;")
        layout.addWidget(self.lbl_watermark_status)

        layout.addWidget(QLabel(self.tr("wm.angle", "Rotation Angle:")))
        self.combo_angle = QComboBox()
        self.combo_angle.addItems(["0°", "45°", "90°", "135°", "180°", "225°", "270°", "315°"])
        self.combo_angle.setStyleSheet(Theme.get_input_style(self.accent_color))
        self.combo_angle.currentIndexChanged.connect(self.generate_preview)
        layout.addWidget(self.combo_angle)

        layout.addWidget(QLabel(self.tr("wm.scale", "Pattern Scale:")))
        self.slider_scale = QSlider(Qt.Horizontal)
        self.slider_scale.setRange(10, 200)
        self.slider_scale.setValue(100)
        self.slider_scale.valueChanged.connect(self.generate_preview)
        layout.addWidget(self.slider_scale)

        layout.addWidget(QLabel(self.tr("wm.opacity", "Opacity:")))
        self.slider_opacity = QSlider(Qt.Horizontal)
        self.slider_opacity.setRange(0, 100)
        self.slider_opacity.setValue(30)
        self.slider_opacity.valueChanged.connect(self.generate_preview)
        layout.addWidget(self.slider_opacity)

        layout.addSpacing(10)

        # 3. Logo
        lbl_logo_section = QLabel(self.tr("wm.section.logo", "🏷️ 3. LOGOTIPO"))
        lbl_logo_section.setStyleSheet(f"color: {text_sec}; font-weight: bold; font-size: 12px;")
        layout.addWidget(lbl_logo_section)

        self.btn_load_logo = QPushButton(self.tr("wm.load_logo", "📂 Load Logo"))
        self.btn_load_logo.setStyleSheet(Theme.get_button_style("#555"))
        self.btn_load_logo.setFixedHeight(36)
        self.btn_load_logo.clicked.connect(self.load_logo_asset)
        layout.addWidget(self.btn_load_logo)

        self.lbl_logo_status = QLabel(self.tr("wm.status.none", "None"))
        self.lbl_logo_status.setStyleSheet(f"color: {text_dim}; font-size: 10px;")
        layout.addWidget(self.lbl_logo_status)

        layout.addWidget(QLabel(self.tr("wm.logo_pos", "Logo Position:")))
        self.combo_logo_pos = QComboBox()
        self.combo_logo_pos.addItems(["Top-Right", "Top-Left", "Bottom-Right", "Bottom-Left"])
        self.combo_logo_pos.setStyleSheet(Theme.get_input_style(self.accent_color))
        self.combo_logo_pos.currentIndexChanged.connect(self.generate_preview)
        layout.addWidget(self.combo_logo_pos)

        layout.addWidget(QLabel(self.tr("wm.logo_size", "Logo Size (px):")))
        self.slider_logo_size = QSlider(Qt.Horizontal)
        self.slider_logo_size.setRange(50, 500)
        self.slider_logo_size.setValue(150)
        self.slider_logo_size.valueChanged.connect(self.generate_preview)
        layout.addWidget(self.slider_logo_size)

        layout.addStretch()

        self.btn_save = QPushButton(self.tr("common.save_copy", "💾 SAVE COPY"))
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.clicked.connect(self.save_copy)
        self.btn_save.setFixedHeight(44)
        self.btn_save.setStyleSheet(Theme.get_action_button_style(self.accent_color, "#000000"))
        layout.addWidget(self.btn_save)

        return container

    def _create_content(self) -> QWidget:
        theme    = self.context.get('theme_manager') if hasattr(self, 'context') else None
        bg_panel = theme.get_color('bg_panel') if theme else Theme.BG_PANEL
        border   = theme.get_color('border')   if theme else Theme.BORDER
        text_dim = theme.get_color('text_dim') if theme else Theme.TEXT_DIM

        container = QWidget()
        container.setAcceptDrops(True)
        container.dragEnterEvent = self._drag_enter
        container.dropEvent = self._drop

        layout = QVBoxLayout(container)

        preview_frame = QFrame()
        preview_frame.setStyleSheet(
            f"background-color: {bg_panel}; border: 1px solid {border}; border-radius: 10px;"
        )
        preview_layout = QVBoxLayout(preview_frame)

        self.lbl_preview = QLabel(self.tr("wm.preview_prompt", "Drop or Load Image to Begin"))
        self.lbl_preview.setAlignment(Qt.AlignCenter)
        self.lbl_preview.setStyleSheet(f"color: {text_dim}; font-size: 14px; font-weight: bold;")
        preview_layout.addWidget(self.lbl_preview)

        layout.addWidget(preview_frame)
        return container

    # ------------------------------------------------------------------ #

    def load_source_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self.view,
            self.tr("common.select_image", "Select Image"),
            self.last_dir,
            "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self.source_image = path
            self.lbl_image_status.setText(os.path.basename(path))
            self.last_dir = os.path.dirname(path)
            self.generate_preview()

    def load_watermark_asset(self):
        path, _ = QFileDialog.getOpenFileName(
            self.view,
            self.tr("wm.load_wm", "Select Watermark"),
            self.last_dir,
            "Images (*.png *.jpg *.svg)"
        )
        if path:
            self.watermark_asset = path
            self.lbl_watermark_status.setText(os.path.basename(path))
            self.generate_preview()

    def load_logo_asset(self):
        path, _ = QFileDialog.getOpenFileName(
            self.view,
            self.tr("wm.load_logo", "Select Logo"),
            self.last_dir,
            "Images (*.png *.jpg *.svg)"
        )
        if path:
            self.logo_asset = path
            self.lbl_logo_status.setText(os.path.basename(path))
            self.generate_preview()

    def _drag_enter(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def _drop(self, event):
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
            self.lbl_preview.setPixmap(
                pix.scaled(self.lbl_preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
            return

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
            self.lbl_preview.setPixmap(
                pix.scaled(self.lbl_preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )

        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    def save_copy(self):
        if not self.source_image:
            QMessageBox.warning(
                self.view,
                self.tr("common.error", "No Image"),
                self.tr("wm.msg.no_img", "Please load an image first.")
            )
            return

        if not self.watermark_asset and not self.logo_asset:
            QMessageBox.warning(
                self.view,
                self.tr("common.error", "No Assets"),
                self.tr("wm.msg.no_assets", "Please load a watermark or logo.")
            )
            return

        out_path, _ = QFileDialog.getSaveFileName(
            self.view,
            self.tr("common.save_copy", "Save Watermarked Image"),
            self.last_dir,
            "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if not out_path:
            return

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
            QMessageBox.information(
                self.view,
                self.tr("common.success", "Saved"),
                self.tr("wm.msg.saved", "Image saved successfully to:\n{path}").format(path=out_path)
            )
        else:
            QMessageBox.warning(
                self.view,
                self.tr("common.error", "Error"),
                f"{self.tr('wm.msg.save_failed', 'Failed to save image')}: {msg}"
            )

    def load_image_set(self, paths: list):
        if paths:
            self.source_image = paths[0]
            if hasattr(self, 'lbl_image_status'):
                self.lbl_image_status.setText(os.path.basename(self.source_image))
            if hasattr(self, 'lbl_preview'):
                self.generate_preview()

    def run_headless(self, params: dict, input_data) -> None:
        pass
