import logging
import os
import traceback
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton,
                               QHBoxLayout, QFrame, QStackedLayout, QComboBox,
                               QFileDialog, QGridLayout, QListWidget,
                               QGraphicsDropShadowEffect)
from PySide6.QtCore import Qt, Slot, QFileInfo
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QFont
from ..logic.profile_db import ProfileDB
from ..logic.thread_worker import RecognitionWorker, AutoScanWorker
from modules.librarian.logic.db_manager import DatabaseManager
from core.components.standard_layout import StandardToolLayout
from core.theme import Theme
import cv2

log = logging.getLogger(__name__)


class CharacterRecognitionView(QWidget):
    def __init__(self, context):
        super().__init__()
        self.context = context
        self.profile_db = ProfileDB()
        self.worker = None
        self.auto_worker = None
        self._auto_suggestion_name = None

        content = self._create_content()
        sidebar = self._create_sidebar()

        self.layout_manager = StandardToolLayout(
            content,
            sidebar_widget=sidebar,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self.layout_manager)

        self.refresh_profiles()

    def _tr(self, key, default=None):
        """Traduce usando el LocaleManager del contexto."""
        lm = self.context.get('locale_manager') if self.context else None
        if lm:
            return lm.tr(key, default)
        return default if default is not None else key

    def _create_content(self):
        self.viewport = QWidget()
        self.stack_layout = QGridLayout(self.viewport)
        self.stack_layout.setContentsMargins(0, 0, 0, 0)

        # Image layer
        self.image_label = QLabel(self._tr("cr.drop_zone", "Drop images here"))
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet(
            f"background-color: {Theme.BG_MAIN}; color: {Theme.TEXT_DIM}; font-size: 16px;"
        )

        # Overlay menu
        self.action_bar_container = QFrame()
        self.action_bar_container.setObjectName("OverlayMenu")
        self.action_bar_container.setStyleSheet(f"""
            #OverlayMenu {{
                background-color: rgba(20, 20, 20, 230);
                border-radius: 8px;
                border: 1px solid {Theme.BORDER};
            }}
            QLabel {{ color: {Theme.TEXT_PRIMARY}; }}
        """)

        try:
            shadow = QGraphicsDropShadowEffect()
            shadow.setBlurRadius(15)
            shadow.setColor(QColor(0, 0, 0, 180))
            shadow.setOffset(0, 4)
            self.action_bar_container.setGraphicsEffect(shadow)
        except Exception as e:
            log.warning(f"Drop shadow unavailable: {e}")

        self.action_bar_layout = QStackedLayout(self.action_bar_container)

        # Page 1: Prediction
        self.page_predict = QWidget()
        layout_predict = QVBoxLayout(self.page_predict)
        layout_predict.setContentsMargins(8, 8, 8, 8)
        layout_predict.setSpacing(5)

        self.lbl_prediction = QLabel(self._tr("cr.unknown", "Unknown"))
        self.lbl_prediction.setAlignment(Qt.AlignCenter)
        self.lbl_prediction.setStyleSheet("font-size: 13px; font-weight: bold;")

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(5)

        _btn_style = (
            "QPushButton {"
            "  color: white; padding: 4px 12px;"
            "  border-radius: 4px; font-size: 12px; font-weight: bold;"
            "}"
        )
        _btn_style_sm = (
            "QPushButton {"
            "  color: white; padding: 3px 8px;"
            "  border-radius: 4px; font-size: 11px;"
            "}"
        )
        self.btn_reject = QPushButton(self._tr("cr.btn.manual", "Manual (Esc)"))
        self.btn_confirm = QPushButton(self._tr("cr.btn.yes", "Yes (Enter)"))
        self.btn_confirm.setStyleSheet(f"background-color: {Theme.ACCENT_SUCCESS};" + _btn_style)
        self.btn_reject.setStyleSheet(f"background-color: {Theme.ACCENT_WARNING};" + _btn_style)

        btn_layout.addWidget(self.btn_reject)
        btn_layout.addWidget(self.btn_confirm)

        # Skip / Return row
        nav_layout = QHBoxLayout()
        nav_layout.setSpacing(5)
        self.btn_return = QPushButton(self._tr("cr.btn.return", "↩ Return"))
        self.btn_skip = QPushButton(self._tr("cr.btn.skip", "⏭ Skip"))
        _nav_color = "#3a3a3a"
        self.btn_return.setStyleSheet(f"background-color: {_nav_color};" + _btn_style_sm)
        self.btn_skip.setStyleSheet(f"background-color: {_nav_color};" + _btn_style_sm)
        nav_layout.addWidget(self.btn_return)
        nav_layout.addWidget(self.btn_skip)

        layout_predict.addWidget(self.lbl_prediction)
        layout_predict.addLayout(btn_layout)
        layout_predict.addLayout(nav_layout)

        # Page 2: Manual
        self.page_manual = QWidget()
        layout_manual = QVBoxLayout(self.page_manual)
        layout_manual.setContentsMargins(8, 8, 8, 8)
        layout_manual.setSpacing(5)

        self.combo_names = QComboBox()
        self.combo_names.setEditable(True)
        self.combo_names.setPlaceholderText(self._tr("cr.name_placeholder", "Name..."))
        self.combo_names.setStyleSheet("padding: 4px; font-size: 12px;")

        btn_manual_layout = QHBoxLayout()
        btn_manual_layout.setSpacing(5)
        btn_save = QPushButton(self._tr("cr.btn.save", "Save"))
        btn_save.setStyleSheet(f"background-color: {Theme.ACCENT_INFO};" + _btn_style)
        btn_cancel = QPushButton(self._tr("cr.btn.back", "Back"))
        btn_cancel.setStyleSheet(f"background-color: #555555; color: white;" + _btn_style)

        btn_manual_layout.addWidget(btn_cancel)
        btn_manual_layout.addWidget(btn_save)
        layout_manual.addWidget(self.combo_names)
        layout_manual.addLayout(btn_manual_layout)

        self.action_bar_layout.addWidget(self.page_predict)
        self.action_bar_layout.addWidget(self.page_manual)

        self.stack_layout.addWidget(self.image_label, 0, 0)
        self.stack_layout.addWidget(self.action_bar_container, 0, 0, Qt.AlignmentFlag.AlignCenter)

        self.btn_confirm.clicked.connect(self.on_confirm)
        self.btn_reject.clicked.connect(self.on_reject_clicked)
        self.btn_skip.clicked.connect(self.on_skip)
        self.btn_return.clicked.connect(self.on_return)
        btn_save.clicked.connect(self.on_manual_save)
        btn_cancel.clicked.connect(lambda: self.action_bar_layout.setCurrentIndex(0))

        self.action_bar_container.hide()

        self.viewport.setAcceptDrops(True)
        self.viewport.dragEnterEvent = self.dragEnterEvent
        self.viewport.dropEvent = self.dropEvent

        return self.viewport

    def _create_sidebar(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Header
        title_lbl = QLabel(self._tr("cr.title", "RECOGNIZER"))
        title_lbl.setStyleSheet(
            f"color: {Theme.ACCENT_MAIN}; font-weight: bold; font-size: 14px; letter-spacing: 1px;"
        )
        layout.addWidget(title_lbl)

        desc_lbl = QLabel(self._tr("cr.desc", "Auto-tag characters using facial recognition."))
        desc_lbl.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 11px;")
        desc_lbl.setWordWrap(True)
        layout.addWidget(desc_lbl)

        layout.addSpacing(10)

        # File Info
        lbl_meta = QLabel(self._tr("cr.file_info", "FILE INFO"))
        lbl_meta.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_meta)

        meta_style = (
            f"color: {Theme.TEXT_PRIMARY};"
            " font-family: 'Consolas', 'Monaco', monospace; font-size: 11px;"
        )
        self.lbl_filename = QLabel(self._tr("cr.no_file", "No File"))
        self.lbl_filename.setStyleSheet(
            f"color: {Theme.TEXT_PRIMARY}; font-size: 12px; font-weight: bold;"
        )
        self.lbl_filename.setWordWrap(True)
        layout.addWidget(self.lbl_filename)

        self.lbl_dimensions = QLabel("--- x ---")
        self.lbl_dimensions.setStyleSheet(meta_style)
        layout.addWidget(self.lbl_dimensions)

        self.lbl_filesize = QLabel("--- KB")
        self.lbl_filesize.setStyleSheet(meta_style)
        layout.addWidget(self.lbl_filesize)

        layout.addSpacing(10)

        # Current Tags
        lbl_tags = QLabel(self._tr("cr.tags", "CURRENT TAGS"))
        lbl_tags.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_tags)

        self.list_tags = QListWidget()
        self.list_tags.setStyleSheet(f"""
            QListWidget {{
                background-color: {Theme.BG_INPUT};
                border: 1px solid {Theme.BORDER};
                border-radius: 4px;
                color: {Theme.TEXT_PRIMARY};
                font-size: 11px;
            }}
            QListWidget::item {{ margin: 2px; }}
        """)
        self.list_tags.setFixedHeight(100)
        layout.addWidget(self.list_tags)

        self.btn_remove_tag = QPushButton(self._tr("cr.btn.remove_tag", "🗑 Remove Tag"))
        self.btn_remove_tag.setStyleSheet(f"""
            QPushButton {{
                background-color: {Theme.BG_INPUT};
                color: {Theme.TEXT_SECONDARY};
                border: 1px solid {Theme.BORDER};
                border-radius: 4px;
                font-size: 11px;
                padding: 3px 6px;
            }}
            QPushButton:hover {{ background-color: #3a3a3a; color: {Theme.TEXT_PRIMARY}; }}
        """)
        self.btn_remove_tag.clicked.connect(self.on_remove_tag)
        layout.addWidget(self.btn_remove_tag)

        layout.addSpacing(10)

        # Mode Toggle
        lbl_mode = QLabel(self._tr("cr.mode", "MODE"))
        lbl_mode.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_mode)

        self._detection_mode = 'illustration'

        _btn_active = f"""
            QPushButton {{
                background-color: {Theme.ACCENT_MAIN};
                color: #000000;
                border-radius: 0px;
                font-size: 11px;
                font-weight: bold;
                padding: 5px 4px;
            }}"""
        _btn_inactive = f"""
            QPushButton {{
                background-color: {Theme.BG_INPUT};
                color: {Theme.TEXT_DIM};
                border-radius: 0px;
                font-size: 11px;
                font-weight: normal;
                padding: 5px 4px;
            }}
            QPushButton:hover {{ background-color: {Theme.BG_PANEL}; color: {Theme.TEXT_PRIMARY}; }}"""

        self._style_active = _btn_active
        self._style_inactive = _btn_inactive

        toggle_frame = QFrame()
        toggle_frame.setStyleSheet(
            f"QFrame {{ border: 1px solid {Theme.BORDER}; border-radius: 4px; }}"
        )
        toggle_layout = QHBoxLayout(toggle_frame)
        toggle_layout.setContentsMargins(0, 0, 0, 0)
        toggle_layout.setSpacing(0)

        self.btn_mode_illustration = QPushButton(self._tr("cr.mode.illustration", "Illustration"))
        self.btn_mode_illustration.setStyleSheet(_btn_active)
        self.btn_mode_illustration.setToolTip(
            self._tr("cr.mode.illustration.tooltip", "Illustration / AI art mode (default)")
        )
        self.btn_mode_real = QPushButton(self._tr("cr.mode.real", "Real Person"))
        self.btn_mode_real.setStyleSheet(_btn_inactive)
        self.btn_mode_real.setToolTip(
            self._tr("cr.mode.real.tooltip", "Real photograph mode — uses landmark alignment")
        )

        toggle_layout.addWidget(self.btn_mode_illustration)
        toggle_layout.addWidget(self.btn_mode_real)
        layout.addWidget(toggle_frame)

        self.btn_mode_illustration.clicked.connect(lambda: self._set_detection_mode('illustration'))
        self.btn_mode_real.clicked.connect(lambda: self._set_detection_mode('real'))

        layout.addSpacing(10)

        # Source
        lbl_source = QLabel(self._tr("cr.source", "SOURCE"))
        lbl_source.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_source)

        btn_load = QPushButton(self._tr("common.load_folder", "📂 Load Folder"))
        btn_load.clicked.connect(self.on_load_folder_clicked)
        btn_load.setStyleSheet(f"""
            QPushButton {{
                background-color: {Theme.ACCENT_INFO};
                color: white;
                border-radius: 6px;
                font-size: 12px;
                font-weight: bold;
                padding: 6px 4px;
            }}
            QPushButton:hover {{ background-color: #a070e0; }}
        """)
        layout.addWidget(btn_load)

        layout.addSpacing(10)

        # Status
        lbl_status_header = QLabel(self._tr("cr.status_header", "STATUS"))
        lbl_status_header.setStyleSheet(
            f"color: {Theme.TEXT_SECONDARY}; font-weight: bold; font-size: 11px;"
        )
        layout.addWidget(lbl_status_header)

        self.lbl_status = QLabel(self._tr("cr.status.idle", "Idle"))
        self.lbl_status.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-size: 11px;")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)

        layout.addSpacing(10)

        # Auto Mode
        lbl_auto = QLabel(self._tr("cr.auto_mode", "AUTO MODE"))
        lbl_auto.setStyleSheet(
            f"color: {Theme.TEXT_SECONDARY}; font-weight: bold; font-size: 11px;"
        )
        layout.addWidget(lbl_auto)

        self.btn_auto_scan = QPushButton(self._tr("cr.auto_scan", "🤖 Auto Scan"))
        self.btn_auto_scan.setToolTip(
            self._tr("cr.auto_scan.tooltip",
                     "Pre-scan all images and suggest a bulk tag if one character dominates")
        )
        self.btn_auto_scan.setStyleSheet(f"""
            QPushButton {{
                background-color: {Theme.BG_INPUT};
                color: {Theme.TEXT_PRIMARY};
                border: 1px solid {Theme.BORDER};
                border-radius: 6px;
                font-size: 12px;
                font-weight: bold;
                padding: 6px 4px;
            }}
            QPushButton:hover {{ background-color: #2a2a2a; color: {Theme.ACCENT_MAIN}; }}
        """)
        self.btn_auto_scan.clicked.connect(self.on_auto_scan)
        layout.addWidget(self.btn_auto_scan)

        self.lbl_auto_status = QLabel("")
        self.lbl_auto_status.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 11px;")
        self.lbl_auto_status.setWordWrap(True)
        self.lbl_auto_status.hide()
        layout.addWidget(self.lbl_auto_status)

        # Suggestion banner (shown after AutoScanWorker finds a dominant character)
        self.frame_suggestion = QFrame()
        self.frame_suggestion.setStyleSheet(f"""
            QFrame {{
                background-color: {Theme.BG_INPUT};
                border: 1px solid {Theme.ACCENT_MAIN};
                border-radius: 6px;
                padding: 4px;
            }}
        """)
        sug_layout = QVBoxLayout(self.frame_suggestion)
        sug_layout.setContentsMargins(6, 6, 6, 6)
        sug_layout.setSpacing(4)

        self.lbl_suggestion_text = QLabel("")
        self.lbl_suggestion_text.setStyleSheet(
            f"color: {Theme.TEXT_PRIMARY}; font-size: 11px; font-weight: bold;"
        )
        self.lbl_suggestion_text.setWordWrap(True)
        sug_layout.addWidget(self.lbl_suggestion_text)

        sug_btn_layout = QHBoxLayout()
        sug_btn_layout.setSpacing(4)
        self.btn_tag_all = QPushButton(self._tr("cr.auto.tag_all", "Tag All"))
        self.btn_tag_all.setStyleSheet(
            f"background-color: {Theme.ACCENT_SUCCESS}; color: black;"
            " border-radius: 4px; font-size: 11px; font-weight: bold; padding: 4px 6px;"
        )
        self.btn_auto_cancel = QPushButton(self._tr("cr.auto.cancel", "Cancel"))
        self.btn_auto_cancel.setStyleSheet(
            "background-color: #555555; color: white;"
            " border-radius: 4px; font-size: 11px; padding: 4px 6px;"
        )
        sug_btn_layout.addWidget(self.btn_tag_all)
        sug_btn_layout.addWidget(self.btn_auto_cancel)
        sug_layout.addLayout(sug_btn_layout)

        self.btn_tag_all.clicked.connect(self.on_tag_all)
        self.btn_auto_cancel.clicked.connect(self.on_auto_cancel)
        self.frame_suggestion.hide()
        layout.addWidget(self.frame_suggestion)

        layout.addStretch()
        return container

    def _set_detection_mode(self, mode):
        self._detection_mode = mode
        if mode == 'illustration':
            self.btn_mode_illustration.setStyleSheet(self._style_active)
            self.btn_mode_real.setStyleSheet(self._style_inactive)
        else:
            self.btn_mode_real.setStyleSheet(self._style_active)
            self.btn_mode_illustration.setStyleSheet(self._style_inactive)

    def refresh_profiles(self):
        self.combo_names.clear()
        profiles = self.profile_db.get_all_profiles()
        self.combo_names.addItems(sorted(p[0] for p in profiles))

    @Slot(str, object, object, str, object, float)
    def on_image_processed(self, path, cv_img, embedding, suggestion, bbox, confidence):
        log.debug(f"UI received processed image: {path}")
        try:
            self.current_path = path
            self.current_embedding = embedding
            self.current_suggestion = suggestion

            file_info = QFileInfo(path)
            self.lbl_filename.setText(file_info.fileName())

            size_kb = file_info.size() / 1024.0
            if size_kb > 1024:
                self.lbl_filesize.setText(f"{size_kb/1024.0:.2f} MB")
            else:
                self.lbl_filesize.setText(f"{size_kb:.1f} KB")

            orig_h, orig_w = cv_img.shape[:2]
            self.lbl_dimensions.setText(f"{orig_w} x {orig_h} px")

            tags = DatabaseManager().get_tags_for_file(path)
            self.list_tags.clear()
            if tags:
                self.list_tags.addItems(sorted(tags))
            else:
                self.list_tags.addItem(self._tr("cr.no_tags", "(No tags)"))

            self.action_bar_layout.setCurrentIndex(0)

            h, w = cv_img.shape[:2]
            max_dim = 1024
            scale = 1.0

            if max(h, w) > max_dim:
                scale = max_dim / float(max(h, w))
                cv_img = cv2.resize(
                    cv_img, (int(w * scale), int(h * scale)),
                    interpolation=cv2.INTER_AREA
                )

            height, width = cv_img.shape[:2]
            bytes_per_line = cv_img.strides[0]
            q_img = QImage(cv_img.data, width, height, bytes_per_line, QImage.Format_RGB888)
            q_img = q_img.rgbSwapped()

            painter = QPainter(q_img)
            if bbox is not None:
                ox, oy, ow, oh = bbox
                x = int(ox * scale)
                y = int(oy * scale)
                bw = int(ow * scale)
                bh = int(oh * scale)

                pad_w = int(bw * 0.1)
                pad_h = int(bh * 0.1)
                x = max(0, x - pad_w)
                y = max(0, y - pad_h)
                bw = min(width - x, bw + 2 * pad_w)
                bh = min(height - y, bh + 2 * pad_h)

                if confidence > 0.6:
                    color = QColor("#50fa7b")
                elif confidence > 0.4:
                    color = QColor("#f1fa8c")
                else:
                    color = QColor("#ff5555")

                pen = QPen(color)
                pen.setWidth(3)
                painter.setPen(pen)
                painter.drawRect(x, y, bw, bh)

                target_name = suggestion if suggestion else self._tr("cr.unknown", "Unknown")
                painter.setBrush(color)
                painter.setPen(Qt.NoPen)

                font = QFont("Segoe UI", 12, QFont.Bold)
                painter.setFont(font)
                fm = painter.fontMetrics()
                text_w = fm.horizontalAdvance(target_name) + 10
                text_h = fm.height() + 5

                painter.drawRect(x, max(0, y - text_h), text_w, text_h)
                painter.setPen(QColor("black"))
                painter.drawText(x + 5, max(0, y - 5), target_name)

            painter.end()

            pixmap = QPixmap.fromImage(q_img)
            lbl_size = self.image_label.size()
            if lbl_size.width() > 0 and lbl_size.height() > 0:
                self.image_label.setPixmap(
                    pixmap.scaled(lbl_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
            self.image_label.repaint()

            _btn_style_inline = (
                "border-radius: 4px; padding: 4px 12px; font-weight: bold;"
            )
            if suggestion:
                self.lbl_prediction.setText(f"{suggestion}?\n({confidence:.2f})")
                self.btn_confirm.setText(self._tr("cr.btn.yes_short", "Yes"))
                self.btn_confirm.setVisible(True)
                self.btn_confirm.setStyleSheet(
                    f"background-color: {Theme.ACCENT_SUCCESS}; color: black; {_btn_style_inline}"
                )
                self.btn_reject.setText(self._tr("cr.btn.edit", "Edit"))
                self.btn_reject.setVisible(True)
                self.btn_reject.setStyleSheet(
                    f"background-color: {Theme.ACCENT_WARNING}; color: white; {_btn_style_inline}"
                )
            else:
                self.lbl_prediction.setText(self._tr("cr.unknown", "Unknown"))
                self.btn_confirm.setText(self._tr("cr.btn.identify", "Identify"))
                self.btn_confirm.setVisible(True)
                self.btn_confirm.setStyleSheet(
                    f"background-color: {Theme.ACCENT_INFO}; color: white; {_btn_style_inline}"
                )
                self.btn_reject.setVisible(False)

            self.action_bar_container.adjustSize()

            if self.stack_layout.indexOf(self.action_bar_container) != -1:
                self.stack_layout.removeWidget(self.action_bar_container)
                self.action_bar_container.setParent(self.viewport)

            if bbox:
                v_w = self.image_label.width()
                v_h = self.image_label.height()
                p_w = pixmap.width()
                p_h = pixmap.height()
                final_scale = min(v_w / p_w, v_h / p_h)
                disp_w = int(p_w * final_scale)
                disp_h = int(p_h * final_scale)
                off_x = (v_w - disp_w) // 2
                off_y = (v_h - disp_h) // 2

                ox, oy, ow, oh = bbox
                box_x = ox * scale
                box_y = oy * scale
                box_w = ow * scale
                box_h = oh * scale

                box_bottom_y_vp = (box_y + box_h) * final_scale + off_y
                box_center_x_vp = (box_x + box_w / 2) * final_scale + off_x

                menu_w = self.action_bar_container.width()
                menu_h = self.action_bar_container.height()

                target_x = max(10, min(int(box_center_x_vp - menu_w / 2), v_w - menu_w - 10))
                target_y = int(box_bottom_y_vp + 15)

                if target_y + menu_h > v_h - 10:
                    target_y = int(box_y * final_scale + off_y - menu_h - 15)

                self.action_bar_container.move(target_x, target_y)
            else:
                self.action_bar_container.move(
                    (self.viewport.width() - self.action_bar_container.width()) // 2,
                    (self.viewport.height() - self.action_bar_container.height()) // 2
                )

            self.action_bar_container.show()
            self.action_bar_container.raise_()

            if self.worker:
                self.worker.pause()

        except Exception as e:
            log.error(f"UI error processing image: {e}\n{traceback.format_exc()}")
            if self.worker:
                self.worker.pause()

    def on_reject_clicked(self):
        self.action_bar_layout.setCurrentIndex(1)
        self.combo_names.setFocus()

    def on_manual_save(self):
        name = self.combo_names.currentText().strip()
        if not name:
            return
        self.current_suggestion = name
        if self.current_embedding is not None:
            log.debug(f"Learning new face for: {name}")
            self.profile_db.add_reference(name, self.current_embedding)
            self.refresh_profiles()
        self.on_confirm()

    def on_confirm(self):
        log.debug(f"on_confirm called. Suggestion: '{getattr(self, 'current_suggestion', None)}'")
        if not hasattr(self, 'current_path'):
            return
        if not self.current_suggestion:
            self.on_reject_clicked()
            return
        success = DatabaseManager().add_tag_to_file(self.current_path, self.current_suggestion)
        if success:
            self.lbl_status.setText(
                self._tr("cr.tagged", "Tagged: {name}").format(name=self.current_suggestion)
            )
        if self.worker:
            self.worker.request_next()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return
        expanded_paths = []
        for url in urls:
            p = url.toLocalFile()
            if os.path.isdir(p):
                for root, _, files in os.walk(p):
                    for f in files:
                        if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.avif')):
                            expanded_paths.append(os.path.join(root, f))
            elif p.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.avif')):
                expanded_paths.append(p)
        if expanded_paths:
            log.debug(f"Drop: {len(expanded_paths)} images found.")
            self.load_images(expanded_paths)
        else:
            log.debug("Drop: no valid images found.")

    def on_load_folder_clicked(self):
        folder = QFileDialog.getExistingDirectory(
            self, self._tr("cr.select_folder", "Select Folder to Process")
        )
        if not folder:
            return
        expanded_paths = []
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.avif')):
                    expanded_paths.append(os.path.join(root, f))
        if expanded_paths:
            self.load_images(expanded_paths)
        else:
            self.lbl_status.setText(self._tr("qs.msg.no_images", "No images found."))

    def load_images(self, paths):
        self.lbl_status.setText(self._tr("cr.status.init", "Initializing AI Engine..."))

        if self.worker and self.worker.isRunning():
            self.worker.is_running = False
            self.worker.wait()

        self.worker = RecognitionWorker(paths, mode=self._detection_mode)
        self.worker.image_processed.connect(self.on_image_processed)
        self.worker.finished.connect(self.on_finished)
        self.worker.progress.connect(self.update_progress)
        self.worker.paused = True
        self.worker.request_next()
        self.worker.start()

    def update_progress(self, current, total):
        pass

    def on_skip(self):
        self.lbl_status.setText(self._tr("cr.skip.done", "Skipped."))
        if self.worker:
            self.worker.request_next()

    def on_return(self):
        if self.worker:
            self.worker.go_back()

    def on_remove_tag(self):
        selected = self.list_tags.currentItem()
        if not selected:
            self.lbl_status.setText(self._tr("cr.no_tag_selected", "No tag selected."))
            return
        tag = selected.text()
        if tag == self._tr("cr.no_tags", "(No tags)"):
            return
        if not hasattr(self, 'current_path'):
            return
        DatabaseManager().remove_tag_from_file(self.current_path, tag)
        tags = DatabaseManager().get_tags_for_file(self.current_path)
        self.list_tags.clear()
        if tags:
            self.list_tags.addItems(sorted(tags))
        else:
            self.list_tags.addItem(self._tr("cr.no_tags", "(No tags)"))
        self.lbl_status.setText(self._tr("cr.tag_removed", "Tag removed."))

    def on_auto_scan(self):
        if not self.worker or not getattr(self.worker, 'paths', None):
            self.lbl_auto_status.setText(
                self._tr("cr.status.idle", "Load images first.")
            )
            self.lbl_auto_status.show()
            return

        if self.auto_worker and self.auto_worker.isRunning():
            return

        self.lbl_auto_status.setText(self._tr("cr.auto.scanning", "Scanning..."))
        self.lbl_auto_status.show()
        self.frame_suggestion.hide()

        self.auto_worker = AutoScanWorker(self.worker.paths, mode=self._detection_mode)
        self.auto_worker.progress.connect(self.on_auto_progress)
        self.auto_worker.suggestion.connect(self.on_auto_suggestion)
        self.auto_worker.no_match.connect(self.on_auto_no_match)
        self.auto_worker.start()

    @Slot(int, int)
    def on_auto_progress(self, current, total):
        self.lbl_auto_status.setText(
            self._tr("cr.auto.scanning", "Scanning...") + f" {current}/{total}"
        )

    @Slot(str, float, int)
    def on_auto_suggestion(self, name, pct, count):
        self._auto_suggestion_name = name
        pct_str = f"{pct * 100:.0f}%"
        self.lbl_auto_status.setText(
            self._tr("cr.auto.suggestion", "Dominant: {name} ({pct})").format(
                name=name, pct=pct_str
            )
        )
        self.lbl_suggestion_text.setText(
            f"{self._tr('cr.auto.tag_all', 'Tag All')}:\n{name}  ({pct_str}, {count} imgs)"
        )
        self.frame_suggestion.show()

    @Slot()
    def on_auto_no_match(self):
        self.lbl_auto_status.setText(
            self._tr("cr.auto.no_match", "No dominant character found.")
        )
        self.frame_suggestion.hide()

    def on_tag_all(self):
        name = self._auto_suggestion_name
        if not name or not self.worker:
            return
        db = DatabaseManager()
        for path in self.worker.paths:
            db.add_tag_to_file(path, name)
        self.lbl_auto_status.setText(
            self._tr("cr.auto.done", "All tagged as {name}.").format(name=name)
        )
        self.frame_suggestion.hide()

    def on_auto_cancel(self):
        if self.auto_worker and self.auto_worker.isRunning():
            self.auto_worker.is_running = False
        self.frame_suggestion.hide()
        self.lbl_auto_status.hide()

    def on_finished(self):
        self.lbl_status.setText(self._tr("cr.status.done", "Done."))
        self.image_label.clear()
        self.image_label.setText(self._tr("cr.batch_done",
            "<h2>✨ Batch Completed! ✨</h2><p>All images processed.</p>"
            "<p style='color: #888;'>Drop more images to continue.</p>"
        ))
        self.image_label.setStyleSheet(
            f"QLabel {{ background-color: {Theme.BG_MAIN}; color: {Theme.TEXT_PRIMARY}; }}"
        )
        self.action_bar_container.hide()
        if self.worker:
            self.worker.deleteLater()
            self.worker = None
