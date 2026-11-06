import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from PySide6.QtCore import QPersistentModelIndex, QSettings, QSignalBlocker, QStringListModel, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QCompleter, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton, QTabWidget, QToolButton,
    QVBoxLayout, QWidget,
)

from core.components.standard_layout import StandardToolLayout
from core.native_threads import owner_threads_ready, start_worker
from core.paths import CachePaths
from ..logic.confirmed_preview import load_previews, load_samples
from ..logic.export import ExportError, validate_name
from ..logic.recognition import ScanCancelled, ordered_review_groups, representatives, rescan, scan
from ..logic.review import Decision, ReviewSession
from ..logic.review_store import ReviewStore
from .confirmed_characters import ConfirmedCharacters
from .photo_gallery import PhotoGallery
from .photo_review import PhotoReviewDialog


log = logging.getLogger(__name__)
PREFIX = "tool.character_separator."
CONFIRMED_PAGE_SIZE = 96


@dataclass
class Candidate:
    members: tuple[int, ...]
    references: tuple[int, ...]
    rejected: bool = False
    suggested: str = ""


class SeparatorJob(QThread):
    progress = Signal(int, int)

    def __init__(self, kind, operation, parent, on_success=None):
        super().__init__(parent)
        self.kind = kind
        self.operation = operation
        self.on_success = on_success
        self.cancel = Event()
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.operation(self.cancel.is_set, self.progress.emit)
        except (ScanCancelled, ExportError, ValueError) as error:
            self.error = error
        except Exception as error:
            log.exception("Character Separator %s failed", self.kind)
            self.error = error


class CharacterSeparatorView(QWidget):
    request_open_images = Signal(str, object)

    def __init__(self, translate, context=None):
        super().__init__()
        self.translate = translate
        self.mother = self.source = None
        self.result = None
        self.groups = {}
        self.session = ReviewSession()
        self.assigned_images = {}
        self.has_references = False
        self.confirmed_page = 0
        self.confirmed_preview_key = None
        self.confirmed_preview = None
        self.job = None
        self.photo_review = None
        self.delete_dialog = None
        self.closing = False
        self.send_extensions = {}
        self.store = ReviewStore()
        self.settings = QSettings("Panopticon", "Character Separator")

        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        self.folder_button = self._button(
            "folder", "Choose parent folder", "folder_tip",
            "Choose any folder on disk. The named character folders will be created here.",
            self.choose_mother,
        )
        self.refine_button = self._button(
            "refine", "Refine a subfolder", "refine_tip",
            "Copy matches from a direct child into sibling folders. The input files stay in place.",
            self.choose_child,
        )
        self.parent_button = self._button(
            "parent", "Scan from parent", "parent_tip",
            "Return to the parent folder as the input for a new scan.", self.use_mother,
        )
        self.scan_button = self._button(
            "scan", "Scan next batch", "scan_tip",
            "Review up to 1,000 more pending images in filename order. Confirmed identities carry over.",
            self.start_scan,
        )
        self.autoscan = QCheckBox(self.t("autoscan", "Autoscan"))
        self.autoscan.setToolTip(self.t(
            "autoscan_tip", "When you create a character, look for it in the unassigned groups of the current batch.",
        ))
        self.autoscan.setChecked(self.settings.value("autoscan", False, type=bool))
        self.autoscan.toggled.connect(lambda checked: self.settings.setValue("autoscan", checked))
        self.scope_label = QLabel()
        self.scope_label.setTextFormat(Qt.TextFormat.PlainText)
        self.scope_label.setWordWrap(True)
        self.batch_notice = QLabel()
        self.batch_notice.setWordWrap(True)
        self.session_notice = QLabel()
        self.session_notice.setWordWrap(True)
        for widget in (self.folder_button, self.refine_button, self.parent_button,
                       self.scope_label, self.scan_button, self.autoscan, self.batch_notice, self.session_notice):
            side.addWidget(widget)
        self.tabs = QTabWidget()
        self.group_list = QListWidget()
        self.group_list.setToolTip(self.t(
            "groups_tip", "Groups with stronger CCIP evidence appear first. Select a group to see all its images.",
        ))
        self.group_list.currentItemChanged.connect(self.show_group)
        review_tab = self.tabs.addTab(self.group_list, self.t("review_tab", "Unidentified"))
        self.tabs.setTabToolTip(review_tab, self.t(
            "review_tab_tip", "Review the unassigned groups and suggestions in the current batch.",
        ))
        self.confirmed = ConfirmedCharacters(self.t, self.tabs)
        confirmed_tab = self.tabs.addTab(self.confirmed, self.t("confirmed_tab", "Identified"))
        self.tabs.setTabToolTip(confirmed_tab, self.t(
            "confirmed_tab_tip", "Inspect the images assigned to each character and correct their assignments.",
        ))
        for button in self.tabs.tabBar().findChildren(QToolButton):
            button.setToolTip(self.t("scroll_tabs_tip", "Scroll to tabs hidden by the available width."))
        side.addWidget(self.tabs, 1)

        canvas = QWidget()
        main = QVBoxLayout(canvas)
        self.help_label = QLabel()
        self.help_label.setWordWrap(True)
        main.addWidget(self.help_label)

        self.suggestion_row = QWidget()
        suggestions = QHBoxLayout(self.suggestion_row)
        suggestions.setContentsMargins(0, 0, 0, 0)
        self.suggested_button = self._button(
            "suggested_name", "{name}?", "suggested_group_tip",
            "Merge the remaining images into this character. Review and discard any outliers first.",
            self.accept_suggestion,
        )
        self.reject_suggestion_button = self._button(
            "reject_suggestion", "Reject suggestion", "reject_suggestion_tip",
            "Reject this name for these images and keep them in review.",
            self.reject_suggestion,
        )
        suggestions.addWidget(self.suggested_button)
        suggestions.addWidget(self.reject_suggestion_button)
        suggestions.addStretch()
        main.addWidget(self.suggestion_row)

        self.identity_names = QStringListModel(self)
        self.name_edit = self.create_name_edit()
        self.name_edit.returnPressed.connect(self.submit_name)
        main.addWidget(self.name_edit)
        self.group_actions = QWidget()
        group_actions = QVBoxLayout(self.group_actions)
        group_actions.setContentsMargins(0, 0, 0, 0)
        decisions = QHBoxLayout()
        self.approve_button = self._button(
            "approve", "Approve group", "approve_tip",
            "Confirm all remaining images in this group under the chosen name.", self.approve_group,
        )
        self.reject_button = self._button(
            "reject", "Reject identity", "reject_tip",
            "Leave these images pending so the full run can compare them with confirmed identities.",
            self.reject_group,
        )
        decisions.addWidget(self.approve_button)
        decisions.addWidget(self.reject_button)
        group_actions.addLayout(decisions)
        merge_row = QHBoxLayout()
        self.merge_target = QComboBox()
        self.merge_target.setModel(self.identity_names)
        self.merge_target.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.merge_target.setToolTip(self.t(
            "merge_target_tip", "Choose a character already confirmed in this review.",
        ))
        self.merge_target.currentIndexChanged.connect(self.update_controls)
        self.merge_button = self._button(
            "merge", "Merge into", "merge_tip",
            "Assign all images in this group to the chosen character and add its representative images as references.",
            self.merge_group,
        )
        merge_row.addWidget(self.merge_target, 1)
        merge_row.addWidget(self.merge_button)
        group_actions.addLayout(merge_row)
        main.addWidget(self.group_actions)
        self.photo_gallery = PhotoGallery(
            self.t("thumbnail_missing", "Preview unavailable"),
            self.t("photos_tip", "Double-click a thumbnail to enlarge it and review that image."),
        )
        self.photo_gallery.selectionModel().selectionChanged.connect(self.update_controls)
        self.photo_gallery.doubleClicked.connect(self.open_photo_review)
        main.addWidget(self.photo_gallery, 1)
        self.page_controls = QWidget()
        page_controls = QHBoxLayout(self.page_controls)
        page_controls.setContentsMargins(0, 0, 0, 0)
        self.prev_page = self._button(
            "previous_page", "Previous", "previous_page_tip",
            "Show the previous images assigned to this character.", lambda: self.change_confirmed_page(-1),
        )
        self.next_page = self._button(
            "next_page", "Next", "next_page_tip",
            "Show the next images assigned to this character.", lambda: self.change_confirmed_page(1),
        )
        self.page_notice = QLabel()
        page_controls.addWidget(self.prev_page)
        page_controls.addWidget(self.page_notice)
        page_controls.addWidget(self.next_page)
        page_controls.addStretch()
        main.addWidget(self.page_controls)
        photo_actions = QHBoxLayout()
        self.assign_photo_button = self._button(
            "assign_photo", "Assign selected image", "assign_photo_tip",
            "Give the selected image the entered identity and keep it as a reference for that name.",
            self.approve_photo,
        )
        self.exclude_photo_button = self._button(
            "exclude_photo", "Discard selected image", "exclude_photo_tip",
            "Exclude this image from later batches and copies in this review. The original stays on disk.",
            self.reject_photo,
        )
        photo_actions.addWidget(self.assign_photo_button)
        photo_actions.addWidget(self.exclude_photo_button)
        main.addLayout(photo_actions)

        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        footer_layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.hide()
        footer_layout.addWidget(self.progress)
        actions = QHBoxLayout()
        self.export_button = self._button(
            "export", "Filter and copy full folder", "export_tip",
            "Compare the whole folder with confirmed identities. Copy clear matches and their captions "
            "into character folders under the parent folder. You can do this after reopening the review.",
            self.start_export,
        )
        self.cancel_button = self._button(
            "cancel", "Cancel", "cancel_tip",
            "Stop after the current step. Completed copies stay in their destination.",
            self.cancel_job,
        )
        self.open_button = self._button(
            "open", "Open parent folder", "open_tip",
            "Find the character folders in File Explorer.", self.open_mother,
        )
        for widget in (self.export_button, self.cancel_button, self.open_button):
            actions.addWidget(widget)
        footer_layout.addLayout(actions)

        handoff = QHBoxLayout()
        self.exported_character = QComboBox()
        self.exported_character.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.exported_character.setPlaceholderText(self.t("send_character", "Choose an exported character"))
        self.exported_character.setToolTip(self.t(
            "send_character_tip", "Choose which character's verified copies to send from this review.",
        ))
        self.exported_character.currentIndexChanged.connect(self.update_controls)
        self.send_button = QPushButton(self.t("send_copies", "Open copies in…"))
        self.send_button.setToolTip(self.t(
            "send_copies_tip", "Send only the verified destination files for this character to another tool.",
        ))
        self.send_menu = QMenu(self.send_button)
        self.send_menu.setToolTipsVisible(True)
        self.send_button.setMenu(self.send_menu)
        handoff.addWidget(self.exported_character, 1)
        handoff.addWidget(self.send_button)
        footer_layout.addLayout(handoff)
        self.tabs.currentChanged.connect(self.change_identity_tab)
        self.confirmed.character_list.currentItemChanged.connect(self.select_confirmed_identity)
        self.confirmed.delete_requested.connect(self.request_delete_character)
        context = context or {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(StandardToolLayout(
            canvas, sidebar, footer, context.get("theme_manager"), context.get("event_bus"),
        ))
        self.change_identity_tab()
        self.apply_scope(None, None, self.session)
        self.status.setText(self.t("review_loading", "Loading saved characters…"))
        self.start_job("load_review", self.restore_scope, on_success=self.show_restored_scope)

    def t(self, key, fallback):
        return self.translate(PREFIX + key, fallback)

    def _button(self, key, text, tip_key, tip, action):
        button = QPushButton(self.t(key, text))
        button.setToolTip(self.t(tip_key, tip))
        button.clicked.connect(action)
        return button

    def create_name_edit(self):
        edit = QLineEdit()
        edit.setPlaceholderText(self.t("name_placeholder", "Character name"))
        edit.setToolTip(self.t(
            "name_tip", "Enter a new character name or choose a name already confirmed for this folder.",
        ))
        completer = QCompleter(self.identity_names, edit)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        edit.setCompleter(completer)
        return edit

    def create_character_picker(self):
        picker = QComboBox()
        picker.setEditable(True)
        picker.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        picker.setLineEdit(self.create_name_edit())
        picker.setModel(self.identity_names)
        picker.setCurrentIndex(-1)
        picker.setToolTip(self.t(
            "photo_review_character_tip", "Open the list of confirmed characters or type a new name.",
        ))
        return picker

    def set_scope(self, mother, source):
        if self.job is not None or self.closing or (mother, source) == (self.mother, self.source):
            return
        self.status.setText(self.t("review_loading", "Loading saved characters…"))
        self.start_job("load_review", lambda cancelled, _: self.load_scope(mother, source, cancelled),
                       on_success=self.show_restored_scope)

    def load_scope(self, mother, source, cancelled):
        mother, source = mother.resolve(strict=True), source.resolve(strict=True)
        return mother, source, self.store.load(mother, source, cancelled=cancelled)

    def restore_scope(self, cancelled, _):
        scope = self.store.last_scope()
        return self.load_scope(*scope, cancelled) if scope else None

    def show_restored_scope(self, loaded):
        if loaded is not None:
            self.apply_scope(*loaded)
        else:
            self.status.clear()

    def apply_scope(self, mother, source, session):
        self.mother, self.source = mother, source
        self.session = session
        self.confirmed_page = 0
        self.confirmed_preview_key = self.confirmed_preview = None
        self.show_destinations()
        self.clear_scan()
        if mother is None:
            self.scope_label.setText(self.t("choose_prompt", "Choose a parent folder to begin."))
        else:
            self.scope_label.setText(self.t(
                "scope", "Parent: {mother}\nInput: {source}",
            ).format(mother=mother, source=source))
        self.status.clear()
        self.show_session()
        self.update_controls()

    def clear_scan(self):
        self.result = None
        self.groups.clear()
        self.batch_notice.hide()
        self.refresh_groups()

    def release_current_scan(self):
        if self.result is not None:
            self.session.seen.difference_update(self.result.paths)
        self.clear_scan()

    def choose_mother(self):
        path = QFileDialog.getExistingDirectory(self, self.folder_button.text(), str(self.mother or ""))
        if path:
            try:
                mother = Path(path).resolve(strict=True)
            except OSError:
                self.status.setText(self.t("invalid_scope", "The parent or input folder is unavailable."))
                return
            self.set_scope(mother, mother)

    def choose_child(self):
        path = QFileDialog.getExistingDirectory(self, self.refine_button.text(), str(self.mother))
        if path:
            source = Path(path)
            if source.parent != self.mother or source.is_symlink() or source.is_junction():
                self.status.setText(self.t("direct_child", "Choose a direct subfolder of the parent folder."))
                return
            self.set_scope(self.mother, source)

    def use_mother(self):
        self.set_scope(self.mother, self.mother)

    def selected_id(self):
        item = self.group_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def group_label(self, key, group):
        name = group.suggested or self.t("identity", "Identity {number}").format(number=key)
        if group.rejected:
            return self.t("rejected_group", "{name} · rejected").format(name=name)
        if group.suggested:
            return self.t("suggested_group", "{name}? · {count} images").format(
                name=name, count=len(group.members),
            )
        return self.t("group_count", "{name} · {count} images").format(
            name=name, count=len(group.members),
        )

    def refresh_groups(self, selection=None):
        row = max(0, self.group_list.currentRow()) if selection is not None else 0
        with QSignalBlocker(self.group_list):
            self.group_list.clear()
            for key, group in self.groups.items():
                item = QListWidgetItem(self.group_label(key, group), self.group_list)
                item.setData(Qt.ItemDataRole.UserRole, key)
                item.setToolTip(item.text())
                if key == selection:
                    self.group_list.setCurrentItem(item)
            if self.group_list.currentItem() is None and self.groups:
                self.group_list.setCurrentRow(min(row, self.group_list.count() - 1))
        self.show_group()

    def show_group(self):
        if self.reviewing_confirmed():
            return
        selected = self.selected_id()
        group = self.groups.get(selected)
        self.name_edit.clear()
        self.suggestion_row.setVisible(bool(group and group.suggested))
        if group and group.suggested:
            self.suggested_button.setText(self.t("suggested_name", "{name}?").format(name=group.suggested))
        with QSignalBlocker(self.merge_target):
            self.merge_target.setCurrentIndex(-1)
        self.show_photos(self.result, group.members if group else ())

    def show_photos(self, result, members):
        if self.photo_gallery.model().set_members(result, members):
            self.photo_gallery.scrollToTop()
        self.update_controls()

    def reviewing_confirmed(self):
        return self.tabs.currentWidget() is self.confirmed

    def change_identity_tab(self):
        confirmed = self.reviewing_confirmed()
        self.group_actions.setVisible(not confirmed)
        self.assign_photo_button.setText(
            self.t("move_photo", "Move selected image") if confirmed else
            self.t("assign_photo", "Assign selected image")
        )
        self.assign_photo_button.setToolTip(
            self.t("move_photo_tip", "Assign the selected image to the character entered above.") if confirmed else
            self.t("assign_photo_tip", "Give the selected image the entered identity and keep it as a reference for that name.")
        )
        self.name_edit.setPlaceholderText(
            self.t("move_name", "Destination character") if confirmed else
            self.t("name_placeholder", "Character name")
        )
        if confirmed:
            self.name_edit.clear()
            self.suggestion_row.hide()
            self.help_label.setText(self.t(
                "confirmed_help", "Select a character to review its images. Move a selected image to correct its assignment.",
            ))
            self.refresh_confirmed_preview()
        else:
            self.page_controls.hide()
            self.help_label.setText(self.t(
                "help", "CCIP groups images by character. Review the proposed groups or decide on individual images. "
                "Confirmed examples help recognize characters in later batches and in the full run.",
            ))
            self.show_group()

    def select_confirmed_identity(self):
        self.confirmed_page = 0
        if self.reviewing_confirmed():
            self.name_edit.clear()
            self.refresh_confirmed_preview()

    def change_confirmed_page(self, step):
        self.confirmed_page += step
        self.refresh_confirmed_preview()

    def refresh_confirmed_preview(self):
        if not self.reviewing_confirmed() or self.job is not None or self.closing:
            return
        name = self.confirmed.selected_name()
        signatures = self.assigned_images.get(name, {})
        count = len(signatures)
        last_page = max(0, (count - 1) // CONFIRMED_PAGE_SIZE)
        self.confirmed_page = min(max(0, self.confirmed_page), last_page)
        self.page_controls.setVisible(last_page > 0)
        self.page_notice.setText(self.t("page_notice", "{page} of {total}").format(
            page=self.confirmed_page + 1, total=last_page + 1,
        ))
        offset = self.confirmed_page * CONFIRMED_PAGE_SIZE
        paths = sorted(signatures, key=lambda path: (path.name.casefold(), path.name))
        page = {path: signatures[path] for path in paths[offset:offset + CONFIRMED_PAGE_SIZE]}
        key = name, tuple(page.items())
        if self.confirmed_preview_key == key:
            self.show_confirmed_preview(self.confirmed_preview)
            return
        self.confirmed_preview_key = key
        self.show_confirmed_preview(None)
        if page:
            self.start_job("preview", lambda cancelled, progress: load_previews(page, cancelled, progress),
                           on_success=self.show_confirmed_preview)

    def show_confirmed_preview(self, preview):
        self.confirmed_preview = preview
        self.show_photos(preview, tuple(range(len(preview.paths))) if preview else ())

    def selected_photo(self):
        selection = self.photo_gallery.selectionModel().selectedIndexes()
        return selection[0] if selection else None

    def open_photo_review(self, index):
        if self.photo_review is not None or not self.photo_gallery.isEnabled() or not index.isValid():
            return
        result = self.photo_gallery.model().result
        group = None if self.reviewing_confirmed() else self.groups.get(self.selected_id())
        photo = QPersistentModelIndex(index)
        member = photo.data(Qt.ItemDataRole.UserRole)
        dialog = PhotoReviewDialog(
            result.paths[member], result.signatures[member], self.create_character_picker(),
            self.entered_name, self.t, self, suggestion=group.suggested if group else None,
        )
        self.photo_review = dialog
        dialog.finished.connect(lambda code: self.finish_photo_review(code, result, photo))
        dialog.destroyed.connect(self.clear_photo_review)
        dialog.open()

    def finish_photo_review(self, code, result, photo):
        if self.closing or self.job is not None or result is not self.photo_gallery.model().result:
            return
        if not photo.isValid():
            return
        if code == QDialog.DialogCode.Accepted and self.photo_review.name is not None:
            self.decide_photo(photo, self.photo_review.name)
        elif code == PhotoReviewDialog.DISCARDED:
            self.decide_photo(photo, None)

    def clear_photo_review(self):
        self.photo_review = None

    def update_controls(self):
        idle = self.job is None and self.delete_dialog is None and not self.closing
        confirmed = self.reviewing_confirmed()
        group = None if confirmed else self.groups.get(self.selected_id())
        selected = idle and group is not None
        self.folder_button.setEnabled(idle)
        self.refine_button.setEnabled(idle and self.mother is not None)
        self.parent_button.setEnabled(idle and self.source != self.mother)
        self.scan_button.setEnabled(idle and self.source is not None)
        self.autoscan.setEnabled(idle)
        self.tabs.setEnabled(idle)
        self.approve_button.setEnabled(selected)
        gallery_enabled = idle and self.photo_gallery.model().rowCount() > 0
        photo_selected = gallery_enabled and self.selected_photo() is not None
        self.name_edit.setEnabled(selected or (confirmed and photo_selected))
        self.photo_gallery.setEnabled(gallery_enabled)
        self.assign_photo_button.setEnabled(photo_selected)
        self.exclude_photo_button.setEnabled(photo_selected)
        self.reject_button.setEnabled(selected and not group.rejected and not group.suggested)
        has_suggestion = selected and bool(group.suggested)
        self.suggested_button.setEnabled(has_suggestion)
        self.reject_suggestion_button.setEnabled(has_suggestion)
        self.merge_target.setEnabled(selected and self.merge_target.count() > 0)
        self.merge_button.setEnabled(self.merge_target.isEnabled() and self.merge_target.currentIndex() >= 0)
        self.export_button.setEnabled(idle and self.has_references)
        self.cancel_button.setEnabled(self.job is not None and self.job.kind in {"scan", "autoscan", "export"}
                                      and not self.job.cancel.is_set() and not self.closing)
        self.open_button.setEnabled(idle and self.mother is not None)
        self.exported_character.setEnabled(idle and bool(self.session.destinations) and bool(self.send_menu.actions()))
        self.send_button.setEnabled(self.exported_character.isEnabled() and self.exported_character.currentIndex() >= 0)
        self.confirmed.set_review_enabled(idle and self.source is not None)
        count = len(self.assigned_images.get(self.confirmed.selected_name(), ())) if confirmed else 0
        self.prev_page.setEnabled(idle and confirmed and self.confirmed_page > 0)
        self.next_page.setEnabled(idle and confirmed and (self.confirmed_page + 1) * CONFIRMED_PAGE_SIZE < count)

    def entered_name(self, text, feedback):
        try:
            name = validate_name(text)
        except ValueError:
            feedback.setText(self.t("invalid_name", "Enter a valid Windows folder name."))
            return
        if self.source != self.mother and self.source.name.casefold() == name.casefold():
            feedback.setText(self.t("source_name", "Choose a different name from the input subfolder."))
            return
        return next((known for known in self.identity_names.stringList()
                     if known.casefold() == name.casefold()), name)

    def submit_name(self):
        if self.reviewing_confirmed():
            self.approve_photo()
        else:
            self.approve_group()

    def approve_group(self):
        if not self.approve_button.isEnabled() or not (name := self.entered_name(self.name_edit.text(), self.status)):
            return
        self.confirm_group(name, self.t("approved_status", "Approved: {name}").format(name=name))

    def confirm_group(self, name, message):
        key = self.selected_id()
        group = self.groups[key]
        decisions = {
            self.result.paths[index]: Decision(name, self.result.samples[index], index in group.references)
            for index in group.members
        }

        def apply():
            del self.groups[key]
            self.show_session()
            self.refresh_groups(key)
            self.update_batch_notice()
            self.status.setText(message)

        self.save_decisions(name, decisions, apply)

    def accept_suggestion(self):
        group = self.groups.get(self.selected_id())
        if self.suggested_button.isEnabled() and group and group.suggested:
            name = group.suggested
            self.confirm_group(name, self.t("merged_status", "Group merged into {name}.").format(name=name))

    def reject_suggestion(self):
        key = self.selected_id()
        group = self.groups.get(key)
        if not self.reject_suggestion_button.isEnabled() or not group or not group.suggested:
            return
        name = group.suggested
        signatures = {self.result.paths[index]: self.result.signatures[index] for index in group.members}
        mother, source = self.mother, self.source

        def committed(_):
            for path, signature in signatures.items():
                self.session.rejections.setdefault(path, {})[name] = signature
            group.suggested = ""
            self.refresh_groups(key)
            self.update_batch_notice()
            self.status.setText(self.t(
                "suggestion_rejected", "Suggestion rejected. The images are still available for review.",
            ))

        self.status.setText(self.t("review_saving", "Saving review…"))
        self.start_job("save_review", lambda *_: self.store.save_rejections(mother, source, name, signatures),
                       on_success=committed)

    def reject_group(self):
        if not self.reject_button.isEnabled():
            return
        key = self.selected_id()
        self.groups[key].rejected = True
        self.status.setText(self.t("rejected_status", "Identity rejected. Approve its name to restore it."))
        self.refresh_groups(key)

    def approve_photo(self):
        if not self.assign_photo_button.isEnabled() or not (name := self.entered_name(self.name_edit.text(), self.status)):
            return
        self.decide_photo(self.selected_photo(), name)

    def reject_photo(self):
        if not self.exclude_photo_button.isEnabled():
            return
        self.decide_photo(self.selected_photo(), None)

    def decide_photo(self, photo, name):
        index = photo.data(Qt.ItemDataRole.UserRole)
        if self.reviewing_confirmed():
            self.decide_confirmed_photo(index, name)
            return
        key, path = self.selected_id(), self.result.paths[index]
        decision = Decision(name, self.result.samples[index], bool(name))
        row = photo.row()
        self.save_decisions(name, {path: decision}, lambda: self.apply_photo_decision(key, index, row, name))

    def decide_confirmed_photo(self, index, name):
        previous = self.confirmed.selected_name()
        if name == previous:
            self.status.setText(self.t("same_character", "This image is already assigned to {name}.").format(name=name))
            return
        preview = self.photo_gallery.model().result
        path = preview.paths[index]
        current = self.session.decisions.get(path)
        decisions = {path: Decision(name, current.sample, bool(name))} if current else {}
        missing = {} if current else {path: preview.signatures[index]}

        def apply():
            if current and (current.reference or not any(
                decision.name == previous and decision.reference for decision in self.session.decisions.values()
            )):
                for group in self.groups.values():
                    if group.suggested == previous:
                        group.suggested = ""
                if self.result is not None:
                    self.refresh_groups(self.selected_id())
                    self.update_batch_notice()
            self.name_edit.clear()
            self.show_session()
            self.show_destinations()
            self.show_photo_decision(name)

        self.save_decisions(name, decisions, apply, missing_samples=missing)

    def save_decisions(self, name, decisions, apply, *, missing_samples=None):
        mother, source, session = self.mother, self.source, self.session
        autoscan = self.autoscan.isChecked() and name and name not in self.identity_names.stringList()

        def persist(cancelled, _):
            saved = decisions
            if missing_samples:
                samples = load_samples(missing_samples, session.cache, cancelled)
                saved = decisions | {path: Decision(name, sample, bool(name)) for path, sample in samples.items()}
            self.store.save_decisions(mother, source, saved)
            return saved

        def committed(saved):
            session.apply_decisions(saved)
            apply()
            if autoscan:
                self.start_autoscan(name)

        self.status.setText(self.t("review_saving", "Saving review…"))
        self.start_job("save_review", persist, on_success=committed)

    def apply_photo_decision(self, key, index, row, name):
        group = self.groups[key]
        group.members = tuple(member for member in group.members if member != index)
        if not group.members:
            del self.groups[key]
        else:
            group.references = tuple(ref for ref in group.references if ref != index)
            if not group.references:
                group.references = representatives(self.result, group.members)
        self.photo_gallery.model().remove(row)
        self.show_session()
        self.refresh_groups(key)
        self.update_batch_notice()
        self.show_photo_decision(name)

    def show_photo_decision(self, name):
        if name is None:
            self.status.setText(self.t("photo_excluded", "Image discarded from this review."))
        else:
            self.status.setText(self.t("photo_assigned", "Image assigned to {name}.").format(name=name))

    def merge_group(self):
        if not self.merge_button.isEnabled():
            return
        name = self.merge_target.currentText()
        self.confirm_group(name, self.t("merged_status", "Group merged into {name}.").format(name=name))

    def show_session(self):
        assigned = sum(decision.name is not None for decision in self.session.decisions.values())
        self.has_references = bool(assigned)
        self.assigned_images = self.session.assigned_images()
        names = sorted(self.assigned_images, key=str.casefold)
        previous = self.confirmed.selected_name()
        self.confirmed.set_characters({name: len(paths) for name, paths in self.assigned_images.items()})
        if self.confirmed.selected_name() != previous:
            self.confirmed_page = 0
        with QSignalBlocker(self.merge_target):
            self.identity_names.setStringList(names)
            self.merge_target.setPlaceholderText(
                self.t("merge_placeholder", "Choose a confirmed character") if names else
                self.t("merge_no_characters", "No confirmed characters yet"),
            )
            self.merge_target.setCurrentIndex(-1)
        self.session_notice.setText(self.t(
            "session_counts", "{identities} confirmed identities · {assigned} manually assigned · "
            "{discarded} discarded · {exported} organized.",
        ).format(
            identities=len(names),
            assigned=assigned,
            discarded=len(self.session.decisions) - assigned,
            exported=len(self.session.exported),
        ))
        self.session_notice.setVisible(self.source is not None)

    def request_delete_character(self, name):
        if self.job is not None or self.delete_dialog is not None or self.closing:
            return
        if name not in self.identity_names.stringList():
            return
        dialog = QMessageBox(QMessageBox.Icon.Warning, self.t("confirmed_delete", "Delete character…"),
                             self.t("delete_character_question", 'Remove "{name}" from this review?').format(name=name),
                             parent=self)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setInformativeText(self.t(
            "delete_character_detail", "Its images return to review. Original files and exported copies stay on disk.",
        ))
        remove = dialog.addButton(self.t("delete_character_action", "Delete character"), QMessageBox.ButtonRole.DestructiveRole)
        remove.setToolTip(self.t("delete_character_action_tip", "Remove the saved identity and release its images for review."))
        cancel = dialog.addButton(self.t("cancel", "Cancel"), QMessageBox.ButtonRole.RejectRole)
        cancel.setToolTip(self.t("delete_character_cancel_tip", "Close this dialog and keep the character."))
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.finished.connect(lambda _: self.finish_delete_request(dialog, remove, name))
        self.delete_dialog = dialog
        self.update_controls()
        dialog.open()

    def finish_delete_request(self, dialog, remove, name):
        self.delete_dialog = None
        confirmed = dialog.clickedButton() is remove
        dialog.deleteLater()
        if confirmed and not self.closing:
            mother, source = self.mother, self.source
            self.status.setText(self.t("review_saving", "Saving review…"))
            self.start_job("save_review", lambda *_: self.store.delete_character(mother, source, name),
                           on_success=lambda _: self.apply_character_deletion(name))
        else:
            self.update_controls()

    def apply_character_deletion(self, name):
        self.session.delete_character(name)
        # Pending suggestions may have depended on the removed references.
        self.release_current_scan()
        self.show_destinations()
        self.show_session()
        self.status.setText(self.t("character_deleted", "{name} was removed. Its images can be reviewed again.").format(name=name))

    def start_scan(self):
        if not self.scan_button.isEnabled():
            return
        source, session = self.source, self.session
        self.status.setText(self.t("scanning", "Scanning images and comparing possible identities…"))
        self.start_job("scan", lambda cancelled, progress: scan(
            source, cancelled, progress, excluded_paths=session.next_batch_exclusions(),
            references=session.references(), cache=session.cache, rejected=session.rejections,
        ))

    def start_autoscan(self, name):
        members = tuple(index for group in self.groups.values() if not group.suggested for index in group.members)
        if not members:
            return
        result, session = self.result, self.session
        self.status.setText(self.t("autoscanning", "Looking for {name} in unassigned groups…").format(name=name))
        self.start_job("autoscan", lambda cancelled, progress: rescan(
            result, members, session.references(), name, cancelled, progress, rejected=session.rejections,
        ), on_success=lambda matches: self.show_autoscan(name, matches))

    def show_autoscan(self, name, matches):
        if matches:
            matched = set(matches)
            key = max(self.groups) + 1
            for group_key, group in tuple(self.groups.items()):
                remaining = tuple(index for index in group.members if index not in matched)
                if not remaining:
                    del self.groups[group_key]
                elif remaining != group.members:
                    group.members = remaining
                    group.references = representatives(self.result, remaining)
            self.groups = {key: Candidate(matches, representatives(self.result, matches), suggested=name),
                           **self.groups}
            self.refresh_groups(key)
            self.update_batch_notice()
        self.status.setText(self.t(
            "autoscan_done", "{name} created · {count} additional images suggested.",
        ).format(name=name, count=len(matches)))

    def start_export(self):
        if not self.export_button.isEnabled():
            return
        mother, source, session = self.mother, self.source, self.session

        def copy(cancelled, progress):
            run = session.full_run(mother, source, cancelled, progress)
            try:
                self.store.save_export(mother, source, run)
            except Exception as error:
                log.exception("Character Separator could not save export receipts")
                return run, error
            return run, None

        self.status.setText(self.t("exporting", "Filtering the full folder and copying clear matches…"))
        self.start_job("export", copy, on_success=self.finish_export)

    def finish_export(self, result):
        run, storage_error = result
        self.show_export(run)
        if storage_error is not None:
            self.status.setText(self.status.text() + " " + self.t(
                "review_export_save_error", "The export results could not be saved for reopening. Copied files are on disk.",
            ))

    def start_job(self, kind, operation, *, on_success=None):
        if self.job is not None or self.closing:
            return
        job = self.job = SeparatorJob(kind, operation, self, on_success)
        job.progress.connect(self.set_progress)
        job.finished.connect(self.job_finished)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.update_controls()
        try:
            start_worker(job, self)
        except Exception as error:
            job.cancel.set()
            self.job_finished(job, error)

    @Slot(int, int)
    def set_progress(self, done, total):
        if self.sender() is not self.job:
            return
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)

    def cancel_job(self):
        if self.job is not None:
            self.job.cancel.set()
            self.status.setText(self.t("cancelling", "Cancelling after the current step…"))
            self.update_controls()

    @Slot()
    def job_finished(self, job=None, startup_error=None):
        job = self.sender() if job is None else job
        if job is None or job is not self.job:
            return
        self.job = None
        self.progress.hide()
        job.deleteLater()
        if self.closing:
            return
        if startup_error is not None:
            self.show_error(startup_error, job.kind)
        elif job.error is not None:
            if job.kind == "export" and isinstance(job.error, ScanCancelled):
                self.status.setText(self.t("full_cancelled", "Full run cancelled during analysis."))
            else:
                self.show_error(job.error, job.kind)
        elif job.kind == "scan":
            if job.cancel.is_set():
                self.status.setText(self.t("scan_cancelled", "Scan cancelled."))
            else:
                self.show_scan(job.result)
        elif job.kind == "autoscan" and job.cancel.is_set():
            self.status.setText(self.t("scan_cancelled", "Scan cancelled."))
        elif job.on_success is not None:
            job.on_success(job.result)
        self.update_controls()
        self.refresh_confirmed_preview()

    def show_scan(self, result):
        self.result = result
        self.session.seen.update(result.paths)
        self.session.seen.update(result.failed)
        self.groups = {
            key: Candidate(group, representatives(result, group), suggested=name)
            for key, (group, name) in enumerate(ordered_review_groups(result), 1)
        }
        self.update_batch_notice()
        self.batch_notice.show()
        self.refresh_groups()
        self.tabs.setCurrentWidget(self.group_list)
        self.status.setText(self.t(
            "scan_done", "{images} images scanned · {groups} candidate identities · {failed} unreadable or changed.",
        ).format(images=len(result.paths), groups=len(self.groups), failed=len(result.failed)))

    def update_batch_notice(self):
        suggested = sum(len(group.members) for group in self.groups.values() if group.suggested)
        self.batch_notice.setText(self.t(
            "batch_notice", "{matched} images with suggestions · {remaining} remain for later batches.",
        ).format(matched=suggested, remaining=self.result.excluded))

    def show_export(self, run):
        self.session.apply(run)
        self.show_destinations()
        self.clear_scan()
        self.show_session()
        if run.error is not None:
            self.show_error(run.error, "export")
            return
        result = run.output
        if result.cancelled:
            text = self.t("export_cancelled", "Copy cancelled. {copied} copied · {skipped} already present.")
        else:
            text = self.t(
                "export_done", "{copied} copied · {skipped} already present · {pending} without a clear match "
                "· {unreadable} unreadable. Scan another batch to review pending images.",
            )
        self.status.setText(text.format(
            copied=result.copied, skipped=result.skipped, pending=run.pending, unreadable=run.unreadable,
        ))
        if (result.copied or result.skipped) and not CachePaths.open_folder(self.mother):
            self.status.setText(
                self.status.text() + " " + self.t("open_failed", "The parent folder could not be opened.")
            )

    def set_send_targets(self, targets, extensions):
        self.send_extensions = extensions
        self.send_menu.clear()
        for key, title in targets.items():
            action = self.send_menu.addAction(title)
            action.setToolTip(self.t("send_target_tip", "Open the selected copies in {tool}.").format(tool=title))
            action.triggered.connect(lambda checked=False, target=key: self.send_exported(target))
        self.update_controls()

    def show_destinations(self):
        character = self.exported_character.currentData()
        with QSignalBlocker(self.exported_character):
            self.exported_character.clear()
            for name, paths in sorted(self.session.destinations.items(), key=lambda item: item[0].casefold()):
                label = self.t("group_count", "{name} · {count} images").format(name=name, count=len(paths))
                self.exported_character.addItem(label, name)
            self.exported_character.setCurrentIndex(self.exported_character.findData(character))

    def send_exported(self, target):
        if not self.send_button.isEnabled():
            return
        paths = self.session.destinations[self.exported_character.currentData()]
        accepted = self.send_extensions.get(target)
        if accepted is not None and any(path.suffix.casefold() not in accepted for path in paths):
            formats = ", ".join(sorted(ext.removeprefix(".").upper() for ext in accepted))
            self.status.setText(self.t(
                "send_unsupported", "This tool accepts {formats}. The character set contains other formats.",
            ).format(formats=formats))
            return
        self.request_open_images.emit(target, sorted(map(str, paths), key=str.casefold))

    def show_error(self, error, kind):
        if kind == "save_review" and isinstance(error, ValueError) and str(error) == "source_changed":
            self.release_current_scan()
            self.status.setText(self.t("source_changed", "An input image changed. Scan the folder again."))
            return
        if kind in {"load_review", "save_review"}:
            log.warning("Character Separator %s: %s", kind, error)
            key, fallback = (
                ("review_load_error", "Could not open the saved review. Check that the input folder is available.")
                if kind == "load_review" else ("review_save_error", "Could not save the review. Try again.")
            )
            self.status.setText(self.t(key, fallback))
            return
        if isinstance(error, ScanCancelled):
            self.status.setText(self.t("scan_cancelled", "Scan cancelled."))
            return
        if isinstance(error, ExportError):
            messages = {
                "invalid_scope": ("invalid_scope", "The parent or input folder is unavailable."),
                "invalid_name": ("invalid_name", "Enter a valid Windows folder name."),
                "destination_conflict": ("destination_conflict", "A different file or folder occupies the destination."),
                "source_changed": ("source_changed", "An input image changed. Scan the folder again."),
                "caption_conflict": ("caption_conflict", "More than one image uses the same caption name."),
            }
            reason = self.t(*messages.get(error.code, ("copy_failed", "A copy could not be verified.")))
            if error.path is not None:
                reason = f"{reason} {error.path}"
            self.status.setText(self.t(
                "export_error", "Copy stopped after {copied} copied and {skipped} already present. {reason}",
            ).format(copied=error.result.copied, skipped=error.result.skipped, reason=reason))
            return
        if isinstance(error, ValueError) and str(error) in {"model_checksum", "model_output"}:
            self.status.setText(self.t("model_failed", "CCIP did not pass model verification. Try scanning again."))
        else:
            log.warning("Character Separator %s: %s", kind, error)
            self.status.setText(self.t("operation_failed", "The operation failed. Details are in the application log."))

    def open_mother(self):
        if self.mother is not None and not CachePaths.open_folder(self.mother):
            self.status.setText(self.t("open_failed", "The parent folder could not be opened."))

    def shutdown(self):
        self.closing = True
        if self.photo_review is not None and self.photo_review.isVisible():
            self.photo_review.reject()
        if self.delete_dialog is not None:
            self.delete_dialog.reject()
        if self.job is not None and self.job.kind in {"load_review", "preview", "scan", "autoscan", "export"}:
            self.job.cancel.set()
        self.update_controls()

    def shutdown_ready(self):
        return self.job is None and self.photo_review is None and self.delete_dialog is None and owner_threads_ready(self)
