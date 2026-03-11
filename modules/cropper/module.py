import os
import re
import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog, QMessageBox,
    QFrame, QSizePolicy, QScrollArea, QComboBox, QSpinBox, QStackedWidget
)
from PySide6.QtCore import Qt, Signal, QMimeData
from PySide6.QtGui import QIcon, QAction, QColor, QStandardItem

from core.base_module import BaseModule
from core.theme import Theme
from core.components.standard_layout import StandardToolLayout
from modules.cropper.logic.cropper_widget import ImageCropperWidget
from modules.cropper.logic.cropper_logic import crop_image

# Combo indices (fixed positions)
AR_FREE   = 0
AR_CUSTOM = 1
# indices 2+ are ratio presets; separators are disabled items (no signal fired)


class CropperModule(BaseModule):
    def __init__(self):
        super().__init__()
        self._name = "Smart Cropper"
        self._description = "High-precision cropping tool for AI training datasets."
        self._icon = "✂️"
        self.accent_color = "#ff79c6"
        self.view = None

    def get_view(self) -> QWidget:
        if self.view:
            return self.view

        self.cropper_widget = ImageCropperWidget()

        sidebar = self._create_sidebar()
        bottom  = self._create_bottom_bar()
        content = self._create_content()

        self.view = StandardToolLayout(
            content,
            sidebar,
            bottom,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )

        self.view.setAcceptDrops(True)
        self.view.dragEnterEvent = self._drag_enter
        self.view.dropEvent = self._drop

        return self.view

    def _drag_enter(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def _drop(self, event):
        files  = [u.toLocalFile() for u in event.mimeData().urls()]
        images = [f for f in files if f.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.webp', '.tiff'))]
        if images:
            self.load_image(images[0])

    def _create_content(self) -> QWidget:
        theme      = self.context.get('theme_manager') if hasattr(self, 'context') else None
        bg_main    = theme.get_color('bg_main') if theme else Theme.BG_MAIN
        border_col = theme.get_color('border')  if theme else Theme.BORDER
        text_dim   = theme.get_color('text_dim') if theme else Theme.TEXT_DIM

        self.stack = QStackedWidget()
        self.stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Page 0: drop zone
        self.page_drop = QFrame()
        self.page_drop.setObjectName("drop_zone")
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

        # Page 1: cropper
        cropper_container = QWidget()
        cropper_layout = QVBoxLayout(cropper_container)
        cropper_layout.setContentsMargins(0, 0, 0, 0)
        self.cropper_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        cropper_layout.addWidget(self.cropper_widget)
        self.stack.addWidget(cropper_container)

        return self.stack

    def _create_sidebar(self) -> QWidget:
        theme    = self.context.get('theme_manager') if hasattr(self, 'context') else None
        text_dim = theme.get_color('text_dim')       if theme else Theme.TEXT_DIM
        text_sec = theme.get_color('text_secondary') if theme else Theme.TEXT_SECONDARY

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        lbl_title = QLabel(self.tr("crop.title", "✂️ SMART CROPPER"))
        lbl_title.setStyleSheet(f"color: {self.accent_color}; font-weight: bold; font-size: 14px;")
        layout.addWidget(lbl_title)

        lbl_desc = QLabel(self.tr("crop.desc", "High-precision cropping for AI datasets."))
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"color: {text_dim}; font-size: 11px;")
        layout.addWidget(lbl_desc)

        layout.addSpacing(10)

        lbl_ar = QLabel(self.tr("crop.ratio", "Aspect Ratio:"))
        lbl_ar.setStyleSheet(f"color: {text_sec}; font-size: 12px;")
        layout.addWidget(lbl_ar)

        self.combo_ar = QComboBox()
        self.combo_ar.addItem(self.tr("crop.free",   "Free"))    # 0 = AR_FREE
        self.combo_ar.addItem(self.tr("crop.custom", "Custom"))  # 1 = AR_CUSTOM

        def _sep(label):
            """Disabled separator item."""
            item = QStandardItem(label)
            item.setEnabled(False)
            item.setForeground(QColor("#555"))
            return item

        model = self.combo_ar.model()
        model.appendRow(_sep(self.tr("crop.group.dataset", "── Dataset ──")))
        for text in [
            self.tr("crop.r.1_1",  "1:1  — Square (Dataset)"),
            self.tr("crop.r.2_1",  "2:1  — Landscape (Dataset)"),
            self.tr("crop.r.1_2",  "1:2  — Portrait (Dataset)"),
        ]:
            self.combo_ar.addItem(text)

        model.appendRow(_sep(self.tr("crop.group.monitors", "── Monitors ──")))
        for text in [
            self.tr("crop.r.4_3",   "4:3   — VGA / iPad"),
            self.tr("crop.r.16_9",  "16:9  — HD Monitor / TV"),
            self.tr("crop.r.16_10", "16:10 — MacBook / Laptop"),
            self.tr("crop.r.21_9",  "21:9  — Ultrawide"),
        ]:
            self.combo_ar.addItem(text)

        model.appendRow(_sep(self.tr("crop.group.phones", "── Phones ──")))
        for text in [
            self.tr("crop.r.9_16", "9:16  — Phone Portrait"),
            self.tr("crop.r.9_19", "9:19  — Modern Phone"),
            self.tr("crop.r.9_21", "9:21  — Tall Phone"),
        ]:
            self.combo_ar.addItem(text)

        self.combo_ar.currentIndexChanged.connect(self._on_ar_changed)
        layout.addWidget(self.combo_ar)

        self.container_custom = QWidget()
        hbox_custom = QHBoxLayout(self.container_custom)
        hbox_custom.setContentsMargins(0, 0, 0, 0)

        self.spin_w = QSpinBox()
        self.spin_w.setRange(1, 999)
        self.spin_w.setValue(1)
        self.spin_w.setPrefix("W: ")
        self.spin_w.valueChanged.connect(self._on_custom_values_changed)

        lbl_x = QLabel(":")
        lbl_x.setStyleSheet(f"color: {text_dim}; font-weight: bold;")

        self.spin_h = QSpinBox()
        self.spin_h.setRange(1, 999)
        self.spin_h.setValue(1)
        self.spin_h.setPrefix("H: ")
        self.spin_h.valueChanged.connect(self._on_custom_values_changed)

        hbox_custom.addWidget(self.spin_w)
        hbox_custom.addWidget(lbl_x)
        hbox_custom.addWidget(self.spin_h)

        self.container_custom.setVisible(False)
        layout.addWidget(self.container_custom)

        layout.addStretch()
        return container

    def _create_bottom_bar(self) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(15)

        self.btn_open = QPushButton(self.tr("crop.open", "📂 Open Image"))
        self.btn_open.setCursor(Qt.PointingHandCursor)
        self.btn_open.clicked.connect(self.open_image_dialog)
        self.btn_open.setFixedWidth(120)
        layout.addWidget(self.btn_open)

        layout.addStretch()

        self.btn_save = QPushButton(self.tr("crop.save", "💾 Save Crop"))
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.clicked.connect(self.save_crop)
        self.btn_save.setFixedWidth(120)
        self.btn_save.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.accent_color};
                color: black;
                border-radius: 4px;
                font-weight: bold;
                padding: 6px;
            }}
            QPushButton:hover {{ background-color: white; }}
        """)
        layout.addWidget(self.btn_save)

        return container

    # ------------------------------------------------------------------ #

    def _on_ar_changed(self, index: int):
        """Compare by index to be locale-safe. Skip disabled separator items."""
        item = self.combo_ar.model().item(index)
        if item and not item.isEnabled():
            # Jump to next enabled item
            self.combo_ar.setCurrentIndex(index + 1)
            return

        if index == AR_FREE:
            self.container_custom.setVisible(False)
            self.cropper_widget.set_fixed_aspect_ratio(None)
        elif index == AR_CUSTOM:
            self.container_custom.setVisible(True)
            self._on_custom_values_changed()
        else:
            self.container_custom.setVisible(True)
            # Parse W:H from "W:H  — Description" format
            try:
                parts = self.combo_ar.itemText(index).split(" ")[0].split(":")
                w, h = int(parts[0]), int(parts[1])
                self.spin_w.blockSignals(True)
                self.spin_h.blockSignals(True)
                self.spin_w.setValue(w)
                self.spin_h.setValue(h)
                self.spin_w.blockSignals(False)
                self.spin_h.blockSignals(False)
                self.cropper_widget.set_fixed_aspect_ratio(w / h)
            except Exception:
                pass

    def _on_custom_values_changed(self):
        if self.combo_ar.currentIndex() != AR_CUSTOM:
            self.combo_ar.blockSignals(True)
            self.combo_ar.setCurrentIndex(AR_CUSTOM)
            self.combo_ar.blockSignals(False)

        w, h = self.spin_w.value(), self.spin_h.value()
        if h > 0:
            self.cropper_widget.set_fixed_aspect_ratio(w / h)

    def open_image_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self.view,
            self.tr("common.load_image", "Open Image"),
            "",
            "Images (*.png *.jpg *.jpeg *.bmp)"
        )
        if path:
            self.load_image(path)

    def load_image(self, path: str):
        self.cropper_widget.load_image(path)
        self.stack.setCurrentIndex(1)

    def save_crop(self):
        if not self.cropper_widget.image_path:
            return

        rect = self.cropper_widget.get_crop_rect_normalized()
        if not rect:
            QMessageBox.warning(
                self.view,
                self.tr("common.error", "Warning"),
                self.tr("common.no_selection", "No selection made.")
            )
            return

        save_path, selected_filter = QFileDialog.getSaveFileName(
            self.view,
            self.tr("common.save_copy", "Save Crop"),
            "",
            "PNG (*.png);;JPG (*.jpg)"
        )
        if not save_path:
            return

        # Auto-append extension if the user didn't type one
        if not os.path.splitext(save_path)[1]:
            m = re.search(r'\*(\.\w+)', selected_filter)
            if m:
                save_path += m.group(1)

        try:
            from modules.librarian.logic.db_manager import DatabaseManager
            db = DatabaseManager()
            tags   = db.get_tags_for_file(self.cropper_widget.image_path)
            rating = db.get_file_rating(self.cropper_widget.image_path)
            crop_image(self.cropper_widget.image_path, rect, save_path, tags=tags, rating=rating)
            QMessageBox.information(
                self.view,
                self.tr("opt.done", "Done"),
                self.tr("crop.saved", "Saved to {path}").format(path=save_path)
            )
        except Exception as e:
            logging.warning(f"[Cropper] Save failed: {e}")
            QMessageBox.warning(
                self.view,
                self.tr("common.error", "Error"),
                str(e)
            )

    def load_image_set(self, paths: list):
        if paths:
            self.load_image(paths[0])

    def run_headless(self, params: dict, input_data) -> None:
        pass
