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
from core.database_manager import DatabaseManager
from core.library_async import LibraryReader, LibraryWriter
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
        self._auto_paths = ()
        self._bulk_remaining = ()
        self._bulk_done = 0
        self._bulk_cancelled = False
        self._pending_edit = None
        self._closed = False
        self._curation_ready = False
        self._generation = 0
        self._queued_paths = None
        self._retiring = False
        self.current_path = None
        self.db = DatabaseManager(service=context.get('data_service'))
        self._reader = LibraryReader(self.db.service, self)
        self._writer = LibraryWriter(self.db.service, self)
        self._reader.result.connect(self._on_curation)
        self._writer.result.connect(self._on_edit)
        self.destroyed.connect(lambda: self.shutdown())

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
        self._set_data_controls()
        self.lbl_status.setTextFormat(Qt.PlainText)
        self.lbl_auto_status.setTextFormat(Qt.PlainText)

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
                background-color: {Theme.BG_PANEL};
                border-radius: 8px;
                border: 1px solid {Theme.BORDER};
            }}
            QLabel {{ color: {Theme.TEXT_PRIMARY}; }}
        """)

        try:
            shadow = QGraphicsDropShadowEffect()
            shadow.setBlurRadius(15)
            shadow_color = QColor(Theme.BORDER)
            shadow_color.setAlpha(180)
            shadow.setColor(shadow_color)
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
            f"  color: {Theme.BG_MAIN}; padding: 4px 12px;"
            "  border-radius: 4px; font-size: 12px; font-weight: bold;"
            "}"
        )
        _btn_style_sm = (
            "QPushButton {"
            f"  color: {Theme.TEXT_PRIMARY}; padding: 3px 8px;"
            "  border-radius: 4px; font-size: 11px;"
            "}"
        )
        self.btn_reject = QPushButton(self._tr("cr.btn.manual", "Manual (Esc)"))
        self.btn_reject.setToolTip(self._tr(
            "cr.btn_manual_tip",
            "Rejects the suggestion and lets you type the name by hand. Shortcut: Esc."))
        self.btn_confirm = QPushButton(self._tr("cr.btn.yes", "Yes (Enter)"))
        self.btn_confirm.setToolTip(self._tr(
            "cr.btn_yes_tip",
            "Confirms the face is who it suggests and saves the tag. Shortcut: Enter."))
        self.btn_confirm.setStyleSheet(f"background-color: {Theme.ACCENT_SUCCESS};" + _btn_style)
        self.btn_reject.setStyleSheet(f"background-color: {Theme.ACCENT_WARNING};" + _btn_style)

        btn_layout.addWidget(self.btn_reject)
        btn_layout.addWidget(self.btn_confirm)

        # Skip / Return row
        nav_layout = QHBoxLayout()
        nav_layout.setSpacing(5)
        self.btn_return = QPushButton(self._tr("cr.btn.return", "↩ Return"))
        self.btn_return.setToolTip(self._tr(
            "cr.btn_return_tip", "Back to the previous recognition screen."))
        self.btn_skip = QPushButton(self._tr("cr.btn.skip", "⏭ Skip"))
        self.btn_skip.setToolTip(self._tr(
            "cr.btn_skip_tip", "Moves to the next face without tagging this one."))
        _nav_color = Theme.BG_INPUT
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
        self.combo_names.setToolTip(self._tr(
            "cr.combo_names_tip",
            "Characters already known. Pick one instead of typing it to avoid duplicates "
            "from a typo."))
        self.combo_names.setEditable(True)
        self.combo_names.setPlaceholderText(self._tr("cr.name_placeholder", "Name..."))
        self.combo_names.setStyleSheet("padding: 4px; font-size: 12px;")

        btn_manual_layout = QHBoxLayout()
        btn_manual_layout.setSpacing(5)
        btn_save = self.btn_manual_save = QPushButton(self._tr("cr.btn.save", "Save"))
        btn_save.setToolTip(self._tr(
            "cr.btn_save_tip", "Saves the typed name and links it to this face."))
        btn_save.setStyleSheet(f"background-color: {Theme.ACCENT_INFO};" + _btn_style)
        btn_cancel = self.btn_manual_cancel = QPushButton(self._tr("cr.btn.back", "Back"))
        btn_cancel.setToolTip(self._tr(
            "cr.btn_back_tip", "Goes back without saving the typed name."))
        btn_cancel.setStyleSheet(f"background-color: {Theme.BG_PANEL}; color: {Theme.TEXT_PRIMARY};"
                                "padding: 4px 12px; border-radius: 4px; font-size: 12px;")

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
        self.list_tags.setToolTip(self._tr('tool.character_recognizer.tags_tip',
            'Tags confirmed by the library. Select one to remove it without changing the image.'))
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

        self.btn_retry_tags = QPushButton(self._tr('tool.character_recognizer.retry_tags', 'Reload tags'))
        self.btn_retry_tags.setToolTip(self._tr('tool.character_recognizer.retry_tags_tip',
            'Retries the library read for this image after an error.'))
        self.btn_retry_tags.clicked.connect(self._request_curation)
        layout.addWidget(self.btn_retry_tags)

        self.btn_remove_tag = QPushButton(self._tr("cr.btn.remove_tag", "🗑 Remove Tag"))
        self.btn_remove_tag.setToolTip(self._tr(
            "cr.btn_remove_tag_tip",
            "Removes this tag from the image. The file itself is untouched."))
        self.btn_remove_tag.setStyleSheet(f"""
            QPushButton {{
                background-color: {Theme.BG_INPUT};
                color: {Theme.TEXT_SECONDARY};
                border: 1px solid {Theme.BORDER};
                border-radius: 4px;
                font-size: 11px;
                padding: 3px 6px;
            }}
            QPushButton:hover {{ background-color: {Theme.BG_PANEL}; color: {Theme.TEXT_PRIMARY}; }}
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
                color: {Theme.BG_MAIN};
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
        self.btn_mode_illustration.setToolTip(self._tr(
            "cr.mode_illustration_tip",
            "Mode for drawings and anime. Uses a detector trained on illustrated faces; "
            "it does worse on real photos."))
        self.btn_mode_illustration.setStyleSheet(_btn_active)
        self.btn_mode_illustration.setToolTip(
            self._tr("cr.mode.illustration.tooltip", "Illustration / AI art mode (default)")
        )
        self.btn_mode_real = QPushButton(self._tr("cr.mode.real", "Real Person"))
        self.btn_mode_real.setToolTip(self._tr(
            "cr.mode_real_tip",
            "Mode for photography. Uses a detector trained on real faces; it does worse "
            "on drawings."))
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
        btn_load.setToolTip(self._tr(
            "cr.load_folder_tip", "Folder of images to look for known faces in."))
        btn_load.clicked.connect(self.on_load_folder_clicked)
        btn_load.setStyleSheet(f"""
            QPushButton {{
                background-color: {Theme.ACCENT_INFO};
                color: {Theme.BG_MAIN};
                border-radius: 6px;
                font-size: 12px;
                font-weight: bold;
                padding: 6px 4px;
            }}
            QPushButton:hover {{ background-color: {Theme._c('accent_hover')}; }}
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
        self.btn_auto_scan.setToolTip(self._tr(
            "cr.auto_scan_tip",
            "Goes through the whole folder recognising faces without asking one by one, "
            "then shows what it found for you to confirm."))
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
            QPushButton:hover {{ background-color: {Theme.BG_PANEL}; color: {Theme.ACCENT_MAIN}; }}
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
        self.btn_tag_all.setToolTip(self._tr(
            "cr.tag_all_tip",
            "Applies the suggested name to every indexed image in the scanned batch, "
            "including images without a match. Stops on error; retry keeps confirmed tags."))
        self.btn_tag_all.setStyleSheet(
            f"background-color: {Theme.ACCENT_SUCCESS}; color: {Theme.BG_MAIN};"
            " border-radius: 4px; font-size: 11px; font-weight: bold; padding: 4px 6px;"
        )
        self.btn_auto_cancel = QPushButton(self._tr("cr.auto.cancel", "Cancel"))
        self.btn_auto_cancel.setToolTip(self._tr(
            "cr.auto_cancel_tip",
            "Stops the automatic scan. Whatever was tagged so far is kept."))
        self.btn_auto_cancel.setStyleSheet(
            f"background-color: {Theme.BG_PANEL}; color: {Theme.TEXT_PRIMARY};"
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

    def _set_data_controls(self):
        idle = not self._closed and self._pending_edit is None
        ready = idle and self._curation_ready
        for control in (self.btn_confirm, self.btn_manual_save, self.btn_remove_tag):
            control.setEnabled(ready)
        for control in (self.btn_skip, self.btn_return, self.btn_reject,
                        self.btn_manual_cancel, self.combo_names, self.btn_auto_scan):
            control.setEnabled(idle)
        self.btn_auto_cancel.setEnabled(not self._closed and
            (idle or self._pending_edit['kind'] == 'bulk'))
        self.btn_tag_all.setEnabled(idle and bool(self._auto_paths or self._bulk_remaining))
        self.btn_retry_tags.setEnabled(idle and bool(self.current_path))

    def _request_curation(self):
        if self._closed or self._pending_edit is not None or not self.current_path:
            return
        self._curation_ready = False
        self.list_tags.clear()
        self.list_tags.addItem(self._tr('tool.character_recognizer.data_loading', 'Loading tags...'))
        self._set_data_controls()
        ticket = self._reader.request('get_curation_snapshot', (self.current_path,))
        self._read_request = (ticket, self.current_path, self._generation)

    @Slot(int, object, object)
    def _on_curation(self, ticket, rows, error):
        if self._closed or getattr(self, '_read_request', None) != (
                ticket, self.current_path, self._generation):
            return
        self.list_tags.clear()
        if error is None and (not isinstance(rows, list) or len(rows) != 1):
            error = RuntimeError('Incomplete curation snapshot')
        if error is not None:
            self.list_tags.addItem(self._tr('tool.character_recognizer.data_error',
                'Cannot read tags: {reason}').format(reason=str(error) or type(error).__name__))
        elif rows[0]['id'] is None:
            self.list_tags.addItem(self._tr('tool.character_recognizer.unregistered',
                'Index this image in Librarian before editing its tags.'))
        else:
            self._curation_ready = True
            tags = rows[0]['tags']
            self.list_tags.addItems(sorted(tags) if tags else [self._tr('cr.no_tags', '(No tags)')])
        self._set_data_controls()

    def _submit_edit(self, kind, path, name, embedding=None):
        if self._closed or self._pending_edit is not None:
            return
        job = dict(kind=kind, path=str(path), name=name, embedding=embedding,
                   generation=self._generation, worker=self.worker)
        self._pending_edit = job
        if path == self.current_path:
            self._reader.invalidate()
        self._set_data_controls()
        label = self.lbl_auto_status if kind == 'bulk' else self.lbl_status
        label.setText(self._tr('tool.character_recognizer.bulk_saving',
            'Saving tags: {done}/{total} confirmed.').format(
                done=self._bulk_done, total=self._bulk_done + len(self._bulk_remaining))
            if kind == 'bulk' else self._tr('tool.character_recognizer.saving', 'Saving tags...'))
        try:
            payload = {'remove_tags' if kind == 'remove' else 'add_tags': (name,)}
            job['ticket'] = self._writer.submit('edit_curation', path, **payload)
        except Exception as exc:
            self._on_edit(job.get('ticket'), None, exc)

    @Slot(int, object, object)
    def _on_edit(self, ticket, committed, error):
        job = self._pending_edit
        if self._closed or job is None or ticket != job.get('ticket'):
            return
        self._pending_edit = None
        current = job['generation'] == self._generation
        if error is not None or committed is not True:
            reason = (str(error) or type(error).__name__) if error is not None else 'not_committed'
            if current:
                key = 'bulk_error' if job['kind'] == 'bulk' else 'write_error'
                label = self.lbl_auto_status if job['kind'] == 'bulk' else self.lbl_status
                label.setText(self._tr('tool.character_recognizer.' + key,
                    'Stopped after {done} confirmed images: {reason}.' if key == 'bulk_error'
                    else 'Cannot save tags: {reason}').format(
                        done=self._bulk_done, reason=reason))
        elif current:
            if job['kind'] == 'bulk':
                self._bulk_done += 1
                self._bulk_remaining = self._bulk_remaining[1:]
                if self._bulk_remaining:
                    self._submit_edit('bulk', self._bulk_remaining[0], job['name'])
                    return
                self.lbl_auto_status.setText(self._tr('tool.character_recognizer.bulk_cancelled',
                    'Stopped after {done} confirmed images.').format(done=self._bulk_done)
                    if self._bulk_cancelled else self._tr('cr.auto.done',
                    'All tagged as {name}.').format(name=job['name']))
                self.frame_suggestion.hide()
                self._auto_paths = ()
            else:
                self.lbl_status.setText(self._tr('cr.tag_removed', 'Tag removed.')
                    if job['kind'] == 'remove' else self._tr('cr.tagged',
                    'Tagged: {name}').format(name=job['name']))
                if job['kind'] == 'manual' and job['embedding'] is not None:
                    # DB especializada: sólo aprender tras confirmar la curación central.
                    try:
                        self.profile_db.add_reference(job['name'], job['embedding'])
                        self.refresh_profiles()
                    except Exception:
                        log.exception('Cannot update character profile after curation commit')
                if job['kind'] != 'remove' and self.current_path == job['path'] and self.worker is job['worker']:
                    if self.worker:
                        self.worker.request_next()
        if self.current_path:
            self._request_curation()
        self._set_data_controls()

    def shutdown(self):
        self._closed = True
        self._reader.close()
        self._writer.close()
        self._queued_paths = None
        self._bulk_remaining = ()
        for worker in (self.worker, self.auto_worker):
            if worker:
                worker.is_running = False

    @Slot(str, object, object, str, object, float)
    def on_image_processed(self, path, cv_img, embedding, suggestion, bbox, confidence):
        if self._closed or self._retiring or (self.sender() is not None and self.sender() is not self.worker):
            return
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

            self._generation += 1
            self._curation_ready = False
            self._reader.invalidate()
            self._request_curation()

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
                    color = QColor(Theme.ACCENT_SUCCESS)
                elif confidence > 0.4:
                    color = QColor(Theme.ACCENT_WARNING)
                else:
                    color = QColor(Theme.ACCENT_DANGER)

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
                painter.setPen(QColor(Theme.BG_MAIN))
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
                    f"background-color: {Theme.ACCENT_SUCCESS}; color: {Theme.BG_MAIN}; {_btn_style_inline}"
                )
                self.btn_reject.setText(self._tr("cr.btn.edit", "Edit"))
                self.btn_reject.setVisible(True)
                self.btn_reject.setStyleSheet(
                    f"background-color: {Theme.ACCENT_WARNING}; color: {Theme.BG_MAIN}; {_btn_style_inline}"
                )
            else:
                self.lbl_prediction.setText(self._tr("cr.unknown", "Unknown"))
                self.btn_confirm.setText(self._tr("cr.btn.identify", "Identify"))
                self.btn_confirm.setVisible(True)
                self.btn_confirm.setStyleSheet(
                    f"background-color: {Theme.ACCENT_INFO}; color: {Theme.BG_MAIN}; {_btn_style_inline}"
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
        if self._closed or self._pending_edit is not None:
            return
        self.action_bar_layout.setCurrentIndex(1)
        self.combo_names.setFocus()

    def on_manual_save(self):
        if self._closed or self._pending_edit is not None or not self._curation_ready:
            return
        name = self.combo_names.currentText().strip()
        if not name:
            return
        self.current_suggestion = name
        embedding = self.current_embedding.copy() if self.current_embedding is not None else None
        self._submit_edit('manual', self.current_path, name, embedding)

    def on_confirm(self):
        log.debug(f"on_confirm called. Suggestion: '{getattr(self, 'current_suggestion', None)}'")
        if self._closed or self._pending_edit is not None or not self._curation_ready or not self.current_path:
            return
        if not self.current_suggestion:
            self.on_reject_clicked()
            return
        self._submit_edit('confirm', self.current_path, self.current_suggestion)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if self._closed:
            return
        urls = event.mimeData().urls()
        if not urls:
            return
        expanded_paths = []
        for url in urls:
            p = url.toLocalFile()
            if os.path.isdir(p):
                for root, _, files in os.walk(p):
                    for f in files:
                        if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                            expanded_paths.append(os.path.join(root, f))
            elif p.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                expanded_paths.append(p)
        if expanded_paths:
            log.debug(f"Drop: {len(expanded_paths)} images found.")
            self.load_images(expanded_paths)
        else:
            log.debug("Drop: no valid images found.")

    def on_load_folder_clicked(self):
        if self._closed:
            return
        folder = QFileDialog.getExistingDirectory(
            self, self._tr("cr.select_folder", "Select Folder to Process")
        )
        if not folder or self._closed:
            return
        expanded_paths = []
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                    expanded_paths.append(os.path.join(root, f))
        if expanded_paths:
            self.load_images(expanded_paths)
        else:
            self.lbl_status.setText(self._tr("qs.msg.no_images", "No images found."))

    def load_images(self, paths):
        if self._closed:
            return
        self._queued_paths = list(paths)
        self._generation += 1
        self.current_path = None
        self._curation_ready = False
        self._reader.invalidate()
        self._auto_paths = ()
        self._bulk_remaining = ()
        self._bulk_done = 0
        self._auto_suggestion_name = None
        self._auto_cancelled = True
        self.frame_suggestion.hide()
        self.action_bar_container.hide()
        if self.auto_worker:
            self.auto_worker.is_running = False
        self._set_data_controls()
        self.lbl_status.setText(self._tr("cr.status.init", "Initializing AI Engine..."))

        if self.worker:
            if self.worker.isRunning():
                self.worker.is_running = False
            self._retiring = True
            return  # Reemplazar sólo tras finished nativo; nunca esperar desde Qt.

        self._start_recognition()

    def _start_recognition(self):
        if self._closed or self._queued_paths is None:
            return
        paths, self._queued_paths = self._queued_paths, None
        self._retiring = False

        self.worker = RecognitionWorker(paths, mode=self._detection_mode)
        self.worker.image_processed.connect(self.on_image_processed)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.progress.connect(self.update_progress)
        self.worker.error.connect(self.on_engine_error)
        self.worker.finished.connect(self._on_worker_stopped)
        self.worker.paused = True
        self.worker.request_next()
        self.worker.start()

    @Slot()
    def _on_worker_stopped(self):
        worker = self.sender()
        if worker is not self.worker:
            return
        self.worker = None
        worker.deleteLater()
        self._start_recognition()

    def update_progress(self, current, total):
        pass

    @Slot(str)
    def on_engine_error(self, msg):
        if self._closed or self._retiring or (self.sender() is not None and self.sender() is not self.worker):
            return
        self.lbl_status.setText(
            self._tr("cr.error.engine", "AI Engine error: {msg}").format(msg=msg)
        )

    @Slot(str)
    def on_auto_error(self, msg):
        if not self._accept_auto_result():
            return
        self.lbl_auto_status.setText(
            self._tr("cr.error.engine", "AI Engine error: {msg}").format(msg=msg)
        )
        self.lbl_auto_status.show()

    def on_skip(self):
        if self._closed or self._pending_edit is not None:
            return
        self.lbl_status.setText(self._tr("cr.skip.done", "Skipped."))
        if self.worker:
            self.worker.request_next()

    def on_return(self):
        if self._closed or self._pending_edit is not None:
            return
        if self.worker:
            self.worker.go_back()

    def on_remove_tag(self):
        if self._closed or self._pending_edit is not None or not self._curation_ready:
            return
        selected = self.list_tags.currentItem()
        if not selected:
            self.lbl_status.setText(self._tr("cr.no_tag_selected", "No tag selected."))
            return
        tag = selected.text()
        if tag == self._tr("cr.no_tags", "(No tags)"):
            return
        if not self.current_path:
            return
        self._submit_edit('remove', self.current_path, tag)

    def on_auto_scan(self):
        if self._closed or self._pending_edit is not None:
            return
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
        self._auto_paths = ()
        self._bulk_remaining = ()
        self._bulk_done = 0

        self.auto_worker = AutoScanWorker(list(self.worker.paths), mode=self._detection_mode)
        self._auto_input = tuple(self.worker.paths)
        self._auto_cancelled = False
        self.auto_worker.progress.connect(self.on_auto_progress)
        self.auto_worker.suggestion.connect(self.on_auto_suggestion)
        self.auto_worker.no_match.connect(self.on_auto_no_match)
        self.auto_worker.error.connect(self.on_auto_error)
        self.auto_worker.start()

    def _accept_auto_result(self):
        return (not self._closed and not getattr(self, '_auto_cancelled', True)
                and (self.sender() is None or self.sender() is self.auto_worker))

    @Slot(int, int)
    def on_auto_progress(self, current, total):
        if not self._accept_auto_result():
            return
        self.lbl_auto_status.setText(
            self._tr("cr.auto.scanning", "Scanning...") + f" {current}/{total}"
        )

    @Slot(str, float, int)
    def on_auto_suggestion(self, name, pct, count):
        if not self._accept_auto_result():
            return
        self._auto_suggestion_name = name
        self._auto_paths = tuple(dict.fromkeys(self._auto_input))
        self._bulk_remaining = ()
        self._bulk_done = 0
        self._set_data_controls()
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
        if not self._accept_auto_result():
            return
        self.lbl_auto_status.setText(
            self._tr("cr.auto.no_match", "No dominant character found.")
        )
        self.frame_suggestion.hide()

    def on_tag_all(self):
        name = self._auto_suggestion_name
        if self._closed or self._pending_edit is not None or not name:
            return
        if not self._bulk_remaining:
            self._bulk_remaining = self._auto_paths
            self._bulk_done = 0
            self._bulk_cancelled = False
        if self._bulk_remaining:
            self._submit_edit('bulk', self._bulk_remaining[0], name)

    def on_auto_cancel(self):
        if self._closed or (self._pending_edit is not None and self._pending_edit['kind'] != 'bulk'):
            return
        self._bulk_cancelled = True
        self._auto_cancelled = True
        self._auto_paths = ()
        self._bulk_remaining = ()
        if self.auto_worker and self.auto_worker.isRunning():
            self.auto_worker.is_running = False
        self.frame_suggestion.hide()
        if self._pending_edit is None:
            self.lbl_auto_status.hide()
        self._set_data_controls()

    def on_finished(self):
        if self._closed or self._retiring or (self.sender() is not None and self.sender() is not self.worker):
            return
        self.current_path = None
        self._curation_ready = False
        self._reader.invalidate()
        self._set_data_controls()
        self.lbl_status.setText(self._tr("cr.status.done", "Done."))
        self.image_label.clear()
        self.image_label.setText(self._tr("cr.batch_done",
            "<h2>✨ Batch Completed! ✨</h2><p>All images processed.</p>"
            "<p>Drop more images to continue.</p>"
        ))
        self.image_label.setStyleSheet(
            f"QLabel {{ background-color: {Theme.BG_MAIN}; color: {Theme.TEXT_PRIMARY}; }}"
        )
        self.action_bar_container.hide()
