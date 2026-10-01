import os
import logging
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout,
                               QScrollArea, QFrame, QFileDialog, QMessageBox, QProgressBar,
                               QComboBox, QSlider, QSizePolicy, QGridLayout)
from PySide6.QtCore import Qt, Signal, QSize, QTimer, Slot
from PySide6.QtGui import QPixmap
from shiboken6 import isValid

from core.base_module import BaseModule
from core.theme import Theme
from core.components.standard_layout import StandardToolLayout
from .logic.worker import WatermarkWorker
from core.native_threads import start_worker, native_done, owner_threads_ready
from core.paths import CachePaths


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
        self.context = {}
        self._closed = False
        self._worker = None
        self._worker_result = None
        self._preview_generation = 0
        self._preview_pending = None
        self._save_pending = None
        self._save_dialog_open = False
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(80)
        self._preview_timer.timeout.connect(self._launch_next)

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
        self.view.destroyed.connect(self._view_destroyed)
        self._sync_save_control()
        if self.source_image:
            self.lbl_image_status.setText(os.path.basename(self.source_image))
            self.generate_preview()
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
        self.btn_load_image.setToolTip(self.tr(
            "wm.load_image_tip",
            "Loads the image to stamp onto. The original file is never touched: the "
            "result is saved as a separate copy."))
        self.btn_load_image.setStyleSheet(Theme.get_action_button_style(self.accent_color))
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
        self.btn_load_watermark.setToolTip(self.tr(
            "wm.load_wm_tip",
            "Image tiled across the whole photo. Use a PNG with transparency so the "
            "background shows through."))
        self.btn_load_watermark.setStyleSheet(Theme.get_button_style(Theme.TEXT_SECONDARY))
        self.btn_load_watermark.setFixedHeight(36)
        self.btn_load_watermark.clicked.connect(self.load_watermark_asset)
        layout.addWidget(self.btn_load_watermark)

        self.lbl_watermark_status = QLabel(self.tr("wm.status.none", "None"))
        self.lbl_watermark_status.setStyleSheet(f"color: {text_dim}; font-size: 10px;")
        layout.addWidget(self.lbl_watermark_status)

        layout.addWidget(QLabel(self.tr("wm.angle", "Rotation Angle:")))
        self.combo_angle = QComboBox()
        self.combo_angle.setToolTip(self.tr(
            "wm.angle_tip",
            "Tilt of the tiled pattern. A diagonal one is harder to crop out than a "
            "horizontal one."))
        self.combo_angle.addItems(["0°", "45°", "90°", "135°", "180°", "225°", "270°", "315°"])
        self.combo_angle.setStyleSheet(Theme.get_input_style(self.accent_color))
        self.combo_angle.currentIndexChanged.connect(self.generate_preview)
        layout.addWidget(self.combo_angle)

        layout.addWidget(QLabel(self.tr("wm.scale", "Pattern Scale:")))
        self.slider_scale = QSlider(Qt.Horizontal)
        self.slider_scale.setToolTip(self.tr(
            "wm.scale_tip",
            "Size of each tile, from 10% to 200%. Bigger means fewer tiles, further "
            "apart."))
        self.slider_scale.setRange(10, 200)
        self.slider_scale.setValue(100)
        self.slider_scale.valueChanged.connect(self.generate_preview)
        layout.addWidget(self.slider_scale)

        layout.addWidget(QLabel(self.tr("wm.opacity", "Opacity:")))
        self.slider_opacity = QSlider(Qt.Horizontal)
        self.slider_opacity.setToolTip(self.tr(
            "wm.opacity_tip",
            "How transparent the pattern is, from 0 to 100%. Under 20% it barely gets in "
            "the way, but it is also easier to remove."))
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
        self.btn_load_logo.setToolTip(self.tr(
            "wm.load_logo_tip",
            "Image placed ONCE in a corner, on top of the tiled pattern. They are "
            "separate things: you can use just one of them."))
        self.btn_load_logo.setStyleSheet(Theme.get_button_style(Theme.TEXT_SECONDARY))
        self.btn_load_logo.setFixedHeight(36)
        self.btn_load_logo.clicked.connect(self.load_logo_asset)
        layout.addWidget(self.btn_load_logo)

        self.lbl_logo_status = QLabel(self.tr("wm.status.none", "None"))
        self.lbl_logo_status.setStyleSheet(f"color: {text_dim}; font-size: 10px;")
        layout.addWidget(self.lbl_logo_status)

        layout.addWidget(QLabel(self.tr("wm.logo_pos", "Logo Position:")))
        self.combo_logo_pos = QComboBox()
        self.combo_logo_pos.setToolTip(self.tr(
            "wm.logo_pos_tip", "Corner where the logo is placed."))
        self.combo_logo_pos.addItems([self.tr('wm.position.'+key, label) for key,label in
            (('top_right','Top-Right'), ('top_left','Top-Left'),
             ('bottom_right','Bottom-Right'), ('bottom_left','Bottom-Left'))])
        self.combo_logo_pos.setStyleSheet(Theme.get_input_style(self.accent_color))
        self.combo_logo_pos.currentIndexChanged.connect(self.generate_preview)
        layout.addWidget(self.combo_logo_pos)

        layout.addWidget(QLabel(self.tr("wm.logo_size", "Logo Size (px):")))
        self.slider_logo_size = QSlider(Qt.Horizontal)
        self.slider_logo_size.setToolTip(self.tr(
            "wm.logo_size_tip",
            "Logo width in pixels, from 50 to 500. It is a fixed size: it does not scale "
            "with the image, so on a small photo it looks proportionally bigger."))
        self.slider_logo_size.setRange(50, 500)
        self.slider_logo_size.setValue(150)
        self.slider_logo_size.valueChanged.connect(self.generate_preview)
        layout.addWidget(self.slider_logo_size)

        layout.addStretch()

        self.btn_save = QPushButton(self.tr("common.save_copy", "💾 SAVE COPY"))
        self.btn_save.setToolTip(self.tr(
            "wm.save_copy_tip",
            "Saves a copy with the mark applied. NOTE: it deliberately strips ALL metadata "
            "from the result (prompts, tags, EXIF), because these copies are meant for "
            "publishing. The original keeps its own."))
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.clicked.connect(self.save_copy)
        self.btn_save.setFixedHeight(44)
        self.btn_save.setStyleSheet(Theme.get_action_button_style(self.accent_color))
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
        if not self._alive():
            return
        path, _ = QFileDialog.getOpenFileName(
            self.view,
            self.tr("common.select_image", "Select Image"),
            self.last_dir,
            "Images (*.png *.jpg *.jpeg *.webp *.avif)"
        )
        if path and self._alive():
            self.source_image = path
            self.lbl_image_status.setText(os.path.basename(path))
            self.last_dir = os.path.dirname(path)
            self.generate_preview()

    def load_watermark_asset(self):
        if not self._alive():
            return
        path, _ = QFileDialog.getOpenFileName(
            self.view,
            self.tr("wm.load_wm", "Select Watermark"),
            self.last_dir,
            "Images (*.png *.jpg *.svg)"
        )
        if path and self._alive():
            self.watermark_asset = path
            self.lbl_watermark_status.setText(os.path.basename(path))
            self.generate_preview()

    def load_logo_asset(self):
        if not self._alive():
            return
        path, _ = QFileDialog.getOpenFileName(
            self.view,
            self.tr("wm.load_logo", "Select Logo"),
            self.last_dir,
            "Images (*.png *.jpg *.svg)"
        )
        if path and self._alive():
            self.logo_asset = path
            self.lbl_logo_status.setText(os.path.basename(path))
            self.generate_preview()

    def _drag_enter(self, event):
        if self._alive() and event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def _drop(self, event):
        if not self._alive():
            event.ignore()
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        image_paths = [p for p in paths if p.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.avif'))]
        if image_paths:
            self.source_image = image_paths[0]
            self.lbl_image_status.setText(os.path.basename(self.source_image))
            self.generate_preview()

    def _alive(self):
        return not self._closed and self.view is not None and isValid(self.view)

    def _options(self):
        return dict(source_path=self.source_image, watermark_path=self.watermark_asset,
            logo_path=self.logo_asset, wm_angle=self.combo_angle.currentIndex()*45,
            wm_scale=self.slider_scale.value()/100.0, wm_opacity=self.slider_opacity.value()/100.0,
            logo_position=('top-right','top-left','bottom-right','bottom-left')[self.combo_logo_pos.currentIndex()],
            logo_size=self.slider_logo_size.value())

    def _sync_save_control(self):
        if not self._alive():
            return
        busy = self._save_dialog_open or self._save_pending is not None or (
            self._worker is not None and self._worker.operation == 'save')
        self.btn_save.setEnabled(bool(self.source_image and (self.watermark_asset or self.logo_asset)) and not busy)
        self.btn_save.setText(self.tr('wm.saving_copy','Saving copy…') if self._save_pending is not None or (
            self._worker is not None and self._worker.operation == 'save')
            else self.tr('common.save_copy','💾 SAVE COPY'))

    def generate_preview(self):
        if not self._alive() or not self.source_image:
            return
        self._preview_generation += 1
        size = (max(1,min(1600,self.lbl_preview.width())), max(1,min(1200,self.lbl_preview.height())))
        self._preview_pending = (self._preview_generation, self._options(), size)
        self.lbl_preview.clear()
        self.lbl_preview.setText(self.tr('wm.preview_busy','Generating preview…'))
        if self._worker is not None and self._worker.operation == 'preview':
            self._worker.stop()
        self._sync_save_control()
        self._preview_timer.start()

    @Slot()
    def _launch_next(self):
        if not self._alive() or self._worker is not None:
            return
        if self._save_pending is not None:
            options, output = self._save_pending
            self._save_pending = None
            worker = WatermarkWorker('save', options, output=output,
                                     service=(self.context or {}).get('data_service'))
        elif self._preview_pending is not None and not self._preview_timer.isActive():
            generation, options, size = self._preview_pending
            self._preview_pending = None
            worker = WatermarkWorker('preview', options, generation=generation, preview_size=size)
        else:
            return
        self._worker, self._worker_result = worker, None
        worker.finished_signal.connect(self._received, Qt.QueuedConnection)
        worker.finished.connect(self._finished, Qt.QueuedConnection)
        self._sync_save_control()
        try:
            start_worker(worker, self)
        except Exception as error:
            self._worker_result = dict(operation=worker.operation, generation=worker.generation,
                output=worker.output, success=False, cancelled=False, image=None, error=str(error))
            worker.stop()
            self._reap(worker)

    @Slot(dict)
    def _received(self, result):
        if self.sender() is self._worker:
            self._worker_result = result

    @Slot()
    def _finished(self):
        if self.sender() is self._worker:
            self._reap(self._worker)

    def _reap(self, worker):
        if worker is not self._worker:
            return
        if not native_done(worker):
            QTimer.singleShot(25, self, lambda: self._reap(worker))
            return
        result, self._worker_result = self._worker_result, None
        self._worker = None
        worker.deleteLater()
        if not self._alive():
            return
        self._sync_save_control()
        result = result or dict(success=False, cancelled=False, error='watermarker_result_missing', image=None)
        if worker.operation == 'preview':
            if worker.generation == self._preview_generation and not result['cancelled']:
                if result['success']:
                    self.lbl_preview.setPixmap(QPixmap.fromImage(result['image']))
                else:
                    self.lbl_preview.clear()
                    self.lbl_preview.setText(self.tr('wm.preview_failed','Preview failed: {reason}')
                                             .format(reason=str(result['error'])[:1024]))
        elif not result['cancelled']:
            if result['success']:
                QMessageBox.information(self.view, self.tr('common.success','Saved'),
                    self.tr('wm.msg.saved','Image saved successfully to:\n{path}').format(path=worker.output))
                if self._alive():
                    CachePaths.open_folder(os.path.dirname(os.path.abspath(worker.output)))
            else:
                QMessageBox.warning(self.view, self.tr('common.error','Error'),
                    self.tr('wm.save_failed_detail','Could not save the copy: {reason}')
                    .format(reason=str(result['error'])[:1024]))
        self._launch_next()

    def save_copy(self):
        if not self._alive() or self._save_dialog_open or self._save_pending is not None or (
                self._worker is not None and self._worker.operation == 'save'):
            return
        if not self.source_image:
            QMessageBox.warning(self.view, self.tr('common.error','No Image'),
                                self.tr('wm.msg.no_img','Please load an image first.'))
            return
        if not self.watermark_asset and not self.logo_asset:
            QMessageBox.warning(self.view, self.tr('common.error','No Assets'),
                                self.tr('wm.msg.no_assets','Please load a watermark or logo.'))
            return
        options = self._options()
        self._save_dialog_open = True
        self._sync_save_control()
        try:
            output, _ = QFileDialog.getSaveFileName(self.view,
                self.tr('common.save_copy','Save Watermarked Image'), self.last_dir,
                self.tr('wm.image_filter','Images (*.png *.jpg *.jpeg *.webp *.avif)'),
                options=QFileDialog.DontConfirmOverwrite)
        finally:
            self._save_dialog_open = False
            self._sync_save_control()
        if not self._alive() or not output:
            return
        if options != self._options():
            QMessageBox.warning(self.view, self.tr('common.error','Error'),
                self.tr('wm.selection_changed','The image or settings changed while choosing a destination. Review them and save again.'))
            return
        self._save_pending = (options, output)
        self.generate_preview()  # Refresh after the prioritized save, including a cancelled preview.
        self._launch_next()

    def _view_destroyed(self):
        self.view = None
        self.on_unload()

    def on_unload(self):
        self._closed = True
        self._preview_generation += 1
        self._preview_pending = self._save_pending = None
        self._preview_timer.stop()
        if self._worker is not None:
            self._worker.stop()

    def shutdown_ready(self):
        return owner_threads_ready(self) and native_done(self._worker)

    def load_image_set(self, paths: list):
        if self._closed:
            return
        if paths:
            self.source_image = paths[0]
            if hasattr(self, 'lbl_image_status'):
                self.lbl_image_status.setText(os.path.basename(self.source_image))
            if hasattr(self, 'lbl_preview'):
                self.generate_preview()

    def run_headless(self, params: dict, input_data) -> None:
        pass
