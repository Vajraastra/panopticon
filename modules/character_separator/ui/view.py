import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from PySide6.QtCore import QSignalBlocker, QStringListModel, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox, QCompleter, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from core.components.standard_layout import StandardToolLayout
from core.paths import CachePaths
from ..logic.export import ExportError, validate_name
from ..logic.recognition import ScanCancelled, representatives, scan
from ..logic.review import Decision, ReviewSession
from .photo_gallery import PhotoGallery


log = logging.getLogger(__name__)
PREFIX = "tool.character_separator."


@dataclass
class Candidate:
    members: tuple[int, ...]
    references: tuple[int, ...]
    rejected: bool = False
    suggested: str = ""


class SeparatorJob(QThread):
    progress = Signal(int, int)

    def __init__(self, kind, operation, parent):
        super().__init__(parent)
        self.kind = kind
        self.operation = operation
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
    def __init__(self, translate, context=None):
        super().__init__()
        self.translate = translate
        self.mother = self.source = None
        self.result = None
        self.groups = {}
        self.session = ReviewSession()
        self.job = None
        self.closing = False

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
        self.scope_label = QLabel()
        self.scope_label.setTextFormat(Qt.TextFormat.PlainText)
        self.scope_label.setWordWrap(True)
        self.batch_notice = QLabel()
        self.batch_notice.setWordWrap(True)
        self.session_notice = QLabel()
        self.session_notice.setWordWrap(True)
        for widget in (self.folder_button, self.refine_button, self.parent_button,
                       self.scope_label, self.scan_button, self.batch_notice, self.session_notice):
            side.addWidget(widget)
        side.addWidget(QLabel(self.t("review", "Review suggested identities")))
        self.group_list = QListWidget()
        self.group_list.setToolTip(self.t("groups_tip", "Select a group to see all its images."))
        self.group_list.currentItemChanged.connect(self.show_group)
        side.addWidget(self.group_list, 1)

        canvas = QWidget()
        main = QVBoxLayout(canvas)
        help_label = QLabel(self.t(
            "help", "Review each proposed group or decide on individual images. Confirmed examples "
            "help recognize characters in later batches and in the full run.",
        ))
        help_label.setWordWrap(True)
        main.addWidget(help_label)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(self.t("name_placeholder", "Character name"))
        self.name_edit.setToolTip(self.t(
            "name_tip", "Enter a new character name or choose a name already confirmed in this session.",
        ))
        self.identity_names = QStringListModel(self)
        completer = QCompleter(self.identity_names, self.name_edit)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.name_edit.setCompleter(completer)
        self.name_edit.returnPressed.connect(self.approve_group)
        main.addWidget(self.name_edit)
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
        main.addLayout(decisions)
        merge_row = QHBoxLayout()
        self.merge_target = QComboBox()
        self.merge_target.setToolTip(self.t(
            "merge_target_tip", "Choose the other group that shows the same character.",
        ))
        self.merge_target.currentIndexChanged.connect(self.update_controls)
        self.merge_button = self._button(
            "merge", "Merge into", "merge_tip",
            "Keep the target name and retain examples from both groups as separate references.",
            self.merge_group,
        )
        merge_row.addWidget(self.merge_target, 1)
        merge_row.addWidget(self.merge_button)
        main.addLayout(merge_row)
        self.photo_gallery = PhotoGallery(self.t("thumbnail_missing", "Preview unavailable"))
        self.photo_gallery.setToolTip(self.t(
            "photos_tip", "Scroll to see every image in the group. Select a thumbnail to assign or discard that image.",
        ))
        self.photo_gallery.selectionModel().selectionChanged.connect(self.update_controls)
        main.addWidget(self.photo_gallery, 1)
        photo_actions = QHBoxLayout()
        self.assign_photo_button = self._button(
            "assign_photo", "Assign selected image", "assign_photo_tip",
            "Give the selected image the entered identity and keep it as a reference for that name.",
            self.approve_photo,
        )
        self.exclude_photo_button = self._button(
            "exclude_photo", "Discard selected image", "exclude_photo_tip",
            "Exclude this image from later batches and copies in this session. The original stays on disk.",
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
            "into character folders under the parent folder.",
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
        context = context or {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(StandardToolLayout(
            canvas, sidebar, footer, context.get("theme_manager"), context.get("event_bus"),
        ))
        self.set_scope(None, None)

    def t(self, key, fallback):
        return self.translate(PREFIX + key, fallback)

    def _button(self, key, text, tip_key, tip, action):
        button = QPushButton(self.t(key, text))
        button.setToolTip(self.t(tip_key, tip))
        button.clicked.connect(action)
        return button

    def set_scope(self, mother, source):
        if (mother, source) == (self.mother, self.source) and self.source is not None:
            return
        self.mother, self.source = mother, source
        self.session = ReviewSession()
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
            return self.t("suggested_group", "{name} · suggested · {count} images").format(
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
        selected = self.selected_id()
        group = self.groups.get(selected)
        self.name_edit.setText(group.suggested if group else "")
        with QSignalBlocker(self.merge_target):
            self.merge_target.clear()
            self.merge_target.addItem(self.t("merge_placeholder", "Choose another identity"), None)
            for key, other in self.groups.items():
                if key != selected and not other.rejected:
                    self.merge_target.addItem(self.group_label(key, other), key)
        if self.photo_gallery.model().set_members(self.result, group.members if group else ()):
            self.photo_gallery.scrollToTop()
        self.update_controls()

    def selected_photo(self):
        selection = self.photo_gallery.selectionModel().selectedIndexes()
        return selection[0] if selection else None

    def update_controls(self):
        idle = self.job is None and not self.closing
        group = self.groups.get(self.selected_id())
        selected = idle and group is not None
        self.folder_button.setEnabled(idle)
        self.refine_button.setEnabled(idle and self.mother is not None)
        self.parent_button.setEnabled(idle and self.source != self.mother)
        self.scan_button.setEnabled(idle and self.source is not None)
        self.group_list.setEnabled(idle)
        self.name_edit.setEnabled(selected)
        self.approve_button.setEnabled(selected)
        photo_selected = selected and self.selected_photo() is not None
        self.photo_gallery.setEnabled(selected)
        self.assign_photo_button.setEnabled(photo_selected)
        self.exclude_photo_button.setEnabled(photo_selected)
        self.reject_button.setEnabled(selected and not group.rejected)
        self.merge_target.setEnabled(selected and not group.rejected and self.merge_target.count() > 1)
        self.merge_button.setEnabled(self.merge_target.isEnabled() and self.merge_target.currentData() is not None)
        self.export_button.setEnabled(idle and self.identity_names.rowCount() > 0)
        self.cancel_button.setEnabled(self.job is not None and not self.job.cancel.is_set() and not self.closing)
        self.open_button.setEnabled(idle and self.mother is not None)

    def entered_name(self):
        try:
            name = validate_name(self.name_edit.text())
        except ValueError:
            self.status.setText(self.t("invalid_name", "Enter a valid Windows folder name."))
            return
        if self.source != self.mother and self.source.name.casefold() == name.casefold():
            self.status.setText(self.t("source_name", "Choose a different name from the input subfolder."))
            return
        return next((known for known in self.identity_names.stringList()
                     if known.casefold() == name.casefold()), name)

    def approve_group(self):
        if not self.approve_button.isEnabled() or not (name := self.entered_name()):
            return
        key = self.selected_id()
        group = self.groups.pop(key)
        for index in group.members:
            self.session.decisions[self.result.paths[index]] = Decision(
                name, self.result.samples[index], index in group.references,
            )
        self.status.setText(self.t("approved_status", "Approved: {name}").format(name=name))
        self.show_session()
        self.refresh_groups(key)

    def reject_group(self):
        if not self.reject_button.isEnabled():
            return
        key = self.selected_id()
        self.groups[key].rejected = True
        self.status.setText(self.t("rejected_status", "Identity rejected. Approve its name to restore it."))
        self.refresh_groups(key)

    def approve_photo(self):
        if not self.assign_photo_button.isEnabled() or not (name := self.entered_name()):
            return
        self.decide_photo(name)
        self.status.setText(self.t("photo_assigned", "Image assigned to {name}.").format(name=name))

    def reject_photo(self):
        if not self.exclude_photo_button.isEnabled():
            return
        self.decide_photo(None)
        self.status.setText(self.t("photo_excluded", "Image discarded for this session."))

    def decide_photo(self, name):
        key = self.selected_id()
        photo = self.selected_photo()
        index = photo.data(Qt.ItemDataRole.UserRole)
        group = self.groups[key]
        path = self.result.paths[index]
        self.session.decisions[path] = Decision(name, self.result.samples[index], bool(name))
        group.members = tuple(member for member in group.members if member != index)
        if not group.members:
            del self.groups[key]
        else:
            group.references = tuple(ref for ref in group.references if ref != index)
            if not group.references:
                group.references = representatives(self.result, group.members)
        self.photo_gallery.model().remove(photo.row())
        self.show_session()
        self.refresh_groups(key)

    def merge_group(self):
        if not self.merge_button.isEnabled():
            return
        source = self.groups.pop(self.selected_id())
        key = self.merge_target.currentData()
        target = self.groups[key]
        target.members = target.members + source.members
        target.references = target.references + source.references
        self.status.setText(self.t("merged_status", "Groups merged. References from both groups were retained."))
        self.refresh_groups(key)

    def show_session(self):
        names = sorted(self.session.references(), key=str.casefold)
        self.identity_names.setStringList(names)
        self.session_notice.setText(self.t(
            "session_counts", "{identities} confirmed identities · {assigned} manually assigned · "
            "{discarded} discarded · {exported} organized.",
        ).format(
            identities=len(names),
            assigned=sum(decision.name is not None for decision in self.session.decisions.values()),
            discarded=sum(decision.name is None for decision in self.session.decisions.values()),
            exported=len(self.session.exported),
        ))
        self.session_notice.setVisible(self.source is not None)

    def start_scan(self):
        if not self.scan_button.isEnabled():
            return
        self.clear_scan()
        source, session = self.source, self.session
        self.start_job("scan", lambda cancelled, progress: scan(
            source, cancelled, progress, excluded_paths=session.next_batch_exclusions(),
            references=session.references(), cache=session.cache,
        ))
        self.status.setText(self.t("scanning", "Scanning images and comparing possible identities…"))

    def start_export(self):
        if not self.export_button.isEnabled():
            return
        mother, source, session = self.mother, self.source, self.session
        self.start_job("export", lambda cancelled, progress: session.full_run(
            mother, source, cancelled, progress,
        ))
        self.status.setText(self.t("exporting", "Filtering the full folder and copying clear matches…"))

    def start_job(self, kind, operation):
        if self.job is not None or self.closing:
            return
        self.job = SeparatorJob(kind, operation, self)
        self.job.progress.connect(self.set_progress)
        self.job.finished.connect(self.job_finished)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.update_controls()
        self.job.start()

    def set_progress(self, done, total):
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)

    def cancel_job(self):
        if self.job is not None:
            self.job.cancel.set()
            self.status.setText(self.t("cancelling", "Cancelling after the current step…"))
            self.update_controls()

    def job_finished(self):
        job, self.job = self.job, None
        self.progress.hide()
        job.deleteLater()
        if self.closing:
            return
        if job.error is not None:
            if job.kind == "export" and isinstance(job.error, ScanCancelled):
                self.status.setText(self.t("full_cancelled", "Full run cancelled during analysis."))
            else:
                self.show_error(job.error, job.kind)
        elif job.kind == "scan":
            if job.cancel.is_set():
                self.status.setText(self.t("scan_cancelled", "Scan cancelled."))
            else:
                self.show_scan(job.result)
        else:
            self.show_export(job.result)
        self.update_controls()

    def show_scan(self, result):
        self.result = result
        self.session.seen.update(result.paths)
        self.session.seen.update(result.failed)
        self.batch_notice.setText(self.t(
            "batch_notice", "{matched} match confirmed identities · {remaining} remain for later batches.",
        ).format(matched=len(result.matches), remaining=result.excluded))
        self.batch_notice.show()
        suggested = {}
        for index, path in enumerate(result.paths):
            if path in result.matches:
                suggested.setdefault(result.matches[path], []).append(index)
        self.groups = {
            key: Candidate(group, representatives(result, group))
            for key, group in enumerate(result.groups, 1)
        }
        for key, (name, group) in enumerate(suggested.items(), len(self.groups) + 1):
            self.groups[key] = Candidate(tuple(group), representatives(result, group), suggested=name)
        self.refresh_groups()
        self.status.setText(self.t(
            "scan_done", "{images} images scanned · {groups} candidate identities · {failed} unreadable or changed.",
        ).format(images=len(result.paths), groups=len(self.groups), failed=len(result.failed)))

    def show_export(self, run):
        self.session.apply(run)
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

    def show_error(self, error, kind):
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
        self.cancel_job()
        self.update_controls()

    def shutdown_ready(self):
        return self.job is None
