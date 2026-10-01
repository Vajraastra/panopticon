"""Revisión de duplicados; preferencias por curación y retiro recuperable explícito."""
from dataclasses import asdict
from copy import deepcopy
import json
from pathlib import Path

from PySide6.QtCore import Qt, Slot, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout,
    QFileDialog, QMessageBox, QProgressBar, QSlider, QComboBox, QSpinBox, QFormLayout,
    QTreeWidget, QTreeWidgetItem, QPlainTextEdit, QLineEdit, QHeaderView, QSizePolicy)

from core.base_module import BaseModule
from core.theme import Theme
from core.paths import ProjectPaths
from core.duplicate_policy import DuplicatePolicy
from core.components.standard_layout import StandardToolLayout
from .logic.review_worker import ExactReviewWorker, VisualReviewWorker
from .logic.retirement_worker import DuplicateRetirementWorker
from .logic.database_worker import DatabaseGroupReviewWorker, NativeDuplicateQueryWorker
from .ui.overlap_panel import OverlapPanel
from .ui.perceptual_panel import PerceptualPanel
from .logic.perceptual_worker import PerceptualBackfillWorker
from .logic.recovery_worker import RecoveryReviewWorker
from .ui.recovery_panel import RecoveryPanel
from .logic.restore_worker import RecoveryRestoreWorker
from .logic.purge_worker import RecoveryPurgeWorker

# Un QThread sin parent sigue vivo aunque se destruya la vista durante I/O.
_LIVE_WORKERS = set()


class DuplicateFinderModule(BaseModule):
    request_open_gallery = Signal(list, str)

    def __init__(self):
        super().__init__()
        self._name = 'Duplicate Finder'
        self._description = 'Review exact or visually similar files and preserve documented copies.'
        self._icon = '👯'
        self.accent_color = Theme.ACCENT_MAIN
        self.context = {}
        self.view = self.worker = self.retirement_worker = None
        self.native_worker = None
        self.phash_worker=None
        self._phash_busy=False
        self.recovery_review_worker=None
        self.recovery_restore_worker=None
        self.recovery_purge_worker=None
        self._purge_request=None
        self._recovery_busy=False
        self._native_busy = False
        self._db_prepared = None
        self._db_page, self._db_cursors, self._db_has_more = 0, [None], False
        self._pending_db_page = 0
        self.review = None
        self.folder_path = ''
        self._closed = self._busy = self._acting = False
        self._token = 0
        self._rows, self._checked_paths, self._page = [], set(), 0

    def text(self, key, default):
        return self.tr('dup.'+key, default)

    def get_view(self):
        if self.view is None:
            content, sidebar, bottom = self._content(), self._sidebar(), self._bottom()
            self.view = StandardToolLayout(content, sidebar_widget=sidebar, bottom_widget=bottom,
                theme_manager=self.context.get('theme_manager'), event_bus=self.context.get('event_bus'))
            self.view.destroyed.connect(self.on_unload)
            self._controls()
        return self.view

    def _sidebar(self):
        box = QWidget()
        layout = QVBoxLayout(box)
        title = QLabel(self.text('title', 'Scan options'))
        title.setStyleSheet(f'font-weight:bold;color:{self.accent_color};')
        layout.addWidget(title)
        layout.addWidget(QLabel(self.text('mode_label', 'Comparison mode:')))
        self.combo_mode = QComboBox()
        self.combo_mode.addItems([self.text('mode_hash', 'Exact content (SHA-256)'), self.text('mode_visual', 'Visual similarity (review only)')])
        self.combo_mode.addItem(self.text('mode_database', 'Native library (exact groups)'))
        self.combo_mode.addItem(self.text('mode_overlap', 'Folder overlap (review only)'))
        self.combo_mode.addItem(self.text('mode_phash','pHash by folders (save)'))
        self.combo_mode.addItem(self.text('mode_recovery_review','Recovery journal (review)'))
        self.combo_mode.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.combo_mode.setMinimumContentsLength(16)
        self.combo_mode.setToolTip(self.text('mode_tip', 'Exact content verifies current bytes and curation. Visual similarity only proposes groups for review.'))
        self.combo_mode.setStyleSheet(Theme.get_input_style())
        self.combo_mode.currentIndexChanged.connect(self._configuration_changed)
        layout.addWidget(self.combo_mode)
        self.threshold_container = QWidget()
        threshold = QVBoxLayout(self.threshold_container)
        threshold.setContentsMargins(0, 0, 0, 0)
        self.lbl_thr_val = QLabel()
        self.slider_thr = QSlider(Qt.Horizontal)
        self.slider_thr.setRange(1, 20)
        self.slider_thr.setValue(5)
        self.slider_thr.setToolTip(self.text('threshold_tip', 'Maximum pHash distance; smaller values require closer similarity. Similarity never authorizes retirement.'))
        self.slider_thr.valueChanged.connect(self._configuration_changed)
        threshold.addWidget(self.lbl_thr_val)
        threshold.addWidget(self.slider_thr)
        layout.addWidget(self.threshold_container)
        self.weight_box = QWidget()
        form = QFormLayout(self.weight_box)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.weights = {}
        defaults = DuplicatePolicy()
        for name, label in (('manual_tag_weight', 'Manual tags'), ('other_tag_weight', 'Other tags'),
                ('rating_weight', 'Rating'), ('generation_weight', 'Generation data'), ('provenance_weight', 'Source information')):
            control = QSpinBox()
            control.setRange(0, 1000)
            control.setValue(getattr(defaults, name))
            control.setStyleSheet(Theme.get_input_style())
            control.setToolTip(self.text('weight_tip', 'Weight for comparing documented copies. Curation always comes first; conflicting or complementary information prevents retirement.'))
            control.lineEdit().setToolTip(control.toolTip())
            control.valueChanged.connect(self._configuration_changed)
            self.weights[name] = control
            form.addRow(self.text(name, label), control)
        layout.addWidget(self.weight_box)
        self.btn_select = self._button('select_folder', 'Select folder', self.select_folder)
        layout.addWidget(self.btn_select)
        self.lbl_folder = QLabel(self.text('no_folder', 'No folder selected'))
        self.lbl_folder.setWordWrap(True)
        self.lbl_folder.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_folder.setTextFormat(Qt.PlainText)
        layout.addWidget(self.lbl_folder)
        self.recovery_label=QLabel(self.text('recovery_label', 'Recovery folder:'))
        layout.addWidget(self.recovery_label)
        self.recovery = QLineEdit(str(ProjectPaths.root()/'data'/'retired_duplicates'))
        self.recovery.setStyleSheet(Theme.get_input_style())
        self.recovery.setToolTip(self.text('recovery_tip', 'Persistent destination outside the collection and Cherry projects, on the same volume. Retired files and their recovery journal are kept here.'))
        layout.addWidget(self.recovery)
        self.btn_recovery = self._button('choose_recovery', 'Choose recovery folder', self.select_recovery)
        layout.addWidget(self.btn_recovery)
        note = QLabel(self.text('policy_note', 'Documented copies take priority. Conflicts, unknown identities and external captions or sidecars require review. Visual similarity does not authorize retirement.'))
        note.setWordWrap(True)
        self.policy_note=note
        note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(note)
        layout.addStretch()
        self.btn_run = self._button('run_scan', 'Start scan', self.run_scan)
        layout.addWidget(self.btn_run)
        return box

    def _content(self):
        box = QWidget()
        layout = QVBoxLayout(box)
        self.status = QLabel(self.text('placeholder', 'Select a folder and start the scan to find duplicates.'))
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.tree = QTreeWidget()
        theme = self.context.get('theme_manager')
        color = theme.get_color if theme else Theme._c
        self.tree.setStyleSheet(f'QTreeView::item:selected {{ background-color:{color("accent_main")}; color:{color("bg_main")}; }}')
        self.tree.setHeaderLabels([self.text(key, label) for key, label in
            (('copy_path', 'Copy path'), ('decision', 'Decision'), ('rating', 'Rating'), ('tags', 'Tags'))])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            self.tree.header().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.tree.itemChanged.connect(self._checked)
        self.tree.currentItemChanged.connect(self._details)
        layout.addWidget(self._database_controls())
        self.overlap_panel=OverlapPanel(self.text,self.accent_color,color)
        self.overlap_panel.changed.connect(self._configuration_changed)
        layout.addWidget(self.overlap_panel,1)
        self.phash_panel=PerceptualPanel(self.text,self.accent_color)
        self.phash_panel.changed.connect(self._configuration_changed)
        layout.addWidget(self.phash_panel,1)
        self.recovery_panel=RecoveryPanel(self.text,self.accent_color,color)
        self.recovery_panel.changed.connect(self._configuration_changed)
        self.recovery_panel.selection_changed.connect(self._controls)
        self.recovery_panel.restore_requested.connect(self.restore_selected_copy)
        self.recovery_panel.purge_preview_requested.connect(self.prepare_selected_purge)
        self.recovery_panel.purge_requested.connect(self.purge_selected_copy)
        layout.addWidget(self.recovery_panel,1)
        layout.addWidget(self.tree, 3)
        navigation = QHBoxLayout()
        self.btn_previous = self._button('previous', 'Previous', lambda: self._navigate(-1))
        self.btn_next = self._button('next', 'Next', lambda: self._navigate(1))
        self.btn_open_group = self._button('open_group', 'View group in Gallery', self.open_group)
        self.page_status = QLabel()
        navigation.addWidget(self.btn_previous)
        navigation.addWidget(self.page_status, 1)
        navigation.addWidget(self.btn_open_group)
        navigation.addWidget(self.btn_next)
        layout.addLayout(navigation)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setToolTip(self.text('details_tip', 'Complete captured copy state: tags, sources, categories, rating and metadata. Select a group to review all its copies.'))
        layout.addWidget(self.details, 2)
        self.action_status = QPlainTextEdit()
        self.action_status.setReadOnly(True)
        self.action_status.setMaximumHeight(120)
        layout.addWidget(self.action_status)
        return box

    def _database_controls(self):
        self.db_box = QWidget()
        layout = QVBoxLayout(self.db_box)
        layout.setContentsMargins(0,0,0,0)
        self.db_status = QLabel(self.text('db_status_unknown', 'Search index status has not been checked.'))
        self.db_status.setTextFormat(Qt.PlainText)
        self.db_status.setWordWrap(True)
        layout.addWidget(self.db_status)
        self.db_tree = QTreeWidget()
        self.db_tree.setStyleSheet(self.tree.styleSheet())
        self.db_tree.setToolTip(self.text('db_tree_tip','Select a complete stored-hash group for physical verification; counts include active native copies across folders.'))
        self.db_tree.setHeaderLabels([self.text('db_hash', 'Stored SHA-256'),
            self.text('db_copies', 'Active copies'), self.text('db_review', 'Verification')])
        self.db_tree.header().setSectionResizeMode(0,QHeaderView.Stretch)
        self.db_tree.header().setSectionResizeMode(1,QHeaderView.ResizeToContents)
        self.db_tree.setMaximumHeight(180)
        self.db_tree.itemSelectionChanged.connect(self._db_selected)
        layout.addWidget(self.db_tree)
        actions = QHBoxLayout()
        self.btn_db_prepare = self._button('db_prepare', 'Prepare search index', self.prepare_database)
        self.btn_db_previous = self._button('db_previous', 'Previous groups', lambda:self._db_page_request(-1))
        self.btn_db_next = self._button('db_next', 'Next groups', lambda:self._db_page_request(1))
        self.btn_db_verify = self._button('db_verify', 'Verify selected group', self.verify_database_group)
        for button in (self.btn_db_prepare,self.btn_db_previous,self.btn_db_next,self.btn_db_verify):
            actions.addWidget(button)
        layout.addLayout(actions)
        return self.db_box

    def _bottom(self):
        box = QWidget()
        layout = QHBoxLayout(box)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress, 1)
        self.btn_cancel = self._button('cancel', 'Cancel', self.cancel)
        self.btn_cancel.setToolTip(self.text('cancel_tip', 'Stop preparation or deletion before the file is marked. An admitted retirement or restoration, or a marked deletion, finishes with its journal without blocking the interface.'))
        self.btn_auto_select = self._button('auto_select', 'Select recommended copies', self.auto_select_duplicates)
        self.btn_auto_select.setToolTip(self.text('auto_select_tip', 'Only marks exact copies whose complete information is preserved in the retained copy. Never selects the first file by position.'))
        self.btn_delete = self._button('delete', 'Retire selected copies', self.delete_files)
        self.btn_delete.setToolTip(self.text('delete_tip', 'Rechecks the entire exact group, archives selected IDs and moves files to the persistent recovery folder. Cherry receipts are retained.'))
        for button in (self.btn_cancel, self.btn_auto_select, self.btn_delete):
            layout.addWidget(button)
        return box

    def _button(self, key, label, callback):
        button = QPushButton(self.text(key, label))
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(self.text(key+'_tip', label))
        button.setStyleSheet(Theme.get_button_style(self.accent_color))
        button.clicked.connect(callback)
        return button

    def _policy(self):
        return DuplicatePolicy(**{name: control.value() for name, control in self.weights.items()})

    def _valid_review(self):
        mode = self.combo_mode.currentIndex()
        expected_folder = str(ProjectPaths.root()) if mode==2 else self.folder_path
        expected_scope = 'database' if mode==2 else 'folder'
        return bool(self.review and self.review.get('token') == self._token and self.review['folder'] == expected_folder and
            self.review.get('scope','folder') == expected_scope and self.review['mode'] == 'hash' and mode in (0,2) and self.review['complete'] and
            not self.review['cancelled'] and not self.review['error'] and self.review['policy'] == asdict(self._policy()))

    def _controls(self):
        idle = not self._closed and not self._busy and not self._acting and not self._native_busy and not self._phash_busy and not self._recovery_busy
        database = self.combo_mode.currentIndex()==2
        overlap = self.combo_mode.currentIndex()==3
        phash=self.combo_mode.currentIndex()==4
        recovery_review=self.combo_mode.currentIndex()==5
        report_only=overlap or phash or recovery_review
        for control in (self.btn_run, self.btn_select, self.combo_mode, self.slider_thr, self.recovery, self.btn_recovery, *self.weights.values()):
            control.setEnabled(idle)
        self.threshold_container.setVisible(self.combo_mode.currentIndex() == 1)
        self.weight_box.setVisible(self.combo_mode.currentIndex() in (0,2))
        self.db_box.setVisible(database)
        self.lbl_folder.setText(self.text('db_scope','Scope: all active indexed copies') if database else
            (self.folder_path or self.text('no_folder','No folder selected')))
        self.btn_select.setEnabled(idle and not database and not report_only)
        self.btn_select.setVisible(not report_only)
        self.lbl_folder.setVisible(not report_only)
        self.overlap_panel.setVisible(overlap)
        self.overlap_panel.set_idle(idle)
        self.phash_panel.setVisible(phash)
        self.phash_panel.set_idle(idle)
        self.recovery_panel.setVisible(recovery_review)
        self.recovery_panel.set_idle(idle)
        self.recovery_panel.restore.setEnabled(bool(idle and recovery_review and self.context.get("data_service") is not None and self.recovery_panel.selected_restore()))
        self.recovery_panel.prepare_purge.setEnabled(bool(idle and recovery_review and self.context.get('data_service') is not None and self.recovery_panel.selected_purge_request()))
        self.recovery_panel.purge.setEnabled(bool(idle and recovery_review and self.context.get('data_service') is not None and self.recovery_panel.selected_purge()))
        for widget in (self.tree,self.details,self.action_status,self.btn_previous,self.btn_next,self.btn_open_group,self.page_status):
            widget.setVisible(not report_only)
        for widget in (self.recovery_label,self.recovery,self.btn_recovery,self.policy_note,self.btn_auto_select,self.btn_delete):
            widget.setVisible(not report_only)
        self.btn_run.setText(self.text('recovery_review_run','Review recovery journal') if recovery_review else self.text('phash_run','Calculate and save pHash') if phash else self.text('overlap_run','Compare folder pairs') if overlap else
            self.text('db_search', 'Find native candidates') if database else self.text('run_scan','Start scan'))
        self.btn_run.setToolTip(self.text('recovery_review_run_tip','Inspect one complete retirement journal against current native identity, curation and file bytes outside the GUI thread. No writes, restore or purge occur.') if recovery_review else self.text('phash_run_tip','Run the separate explicit CPU phase on these folders. Checks bytes and saves derived evidence in batches; does not change curation, Cherry catalogs or images.') if phash else self.text('overlap_run_tip','Compare all pairs of selected folders by stored unique hashes and show coverage. No image reads, merges or retirement occur.') if overlap else
            self.text('db_search_tip', 'Read a page of stored-hash candidates. Physical bytes and curation still require group verification.') if database else self.text('run_scan_tip','Read current files and documented states. No database records or file bytes are changed by scanning.'))
        if overlap:self.btn_run.setEnabled(idle and 2<=len(self.overlap_panel.paths())<=32)
        if phash:self.btn_run.setEnabled(idle and 1<=len(self.phash_panel.paths())<=32)
        if recovery_review:self.btn_run.setEnabled(idle and bool(self.recovery_panel.path()))
        self.lbl_thr_val.setText(self.text('value', 'Value: {value}').format(value=self.slider_thr.value()))
        self.btn_cancel.setEnabled(not self._closed and (self._busy or self._acting or self._native_busy or self._phash_busy or self._recovery_busy))
        valid = idle and self._valid_review()
        self.btn_auto_select.setEnabled(bool(valid and any(report['remove'] for report in self.review['reports'].values())))
        self.btn_delete.setEnabled(bool(valid and self._checked_paths and (not database or self._db_prepared is True)))
        self.btn_previous.setEnabled(idle and self._page > 0)
        self.btn_next.setEnabled(idle and (self._page+1)*100 < len(self._rows))
        self.btn_open_group.setEnabled(bool(idle and self.review and self.tree.currentItem() is not None and self.context.get('main_window')))
        self.db_tree.setEnabled(idle)
        self.btn_db_prepare.setEnabled(idle and database)
        self.btn_db_previous.setEnabled(idle and database and self._db_page>0)
        self.btn_db_next.setEnabled(idle and database and self._db_has_more)
        selected = self.db_tree.currentItem()
        self.btn_db_verify.setEnabled(bool(idle and database and selected and selected.data(0,Qt.UserRole)['reviewable']))

    def _configuration_changed(self, *unused):
        if self.view is None or self._closed:
            return
        self._token += 1
        self.overlap_panel.invalidate()
        self.phash_panel.invalidate()
        self.recovery_panel.invalidate()
        self._checked_paths.clear()
        if self.review:
            self.status.setText(self.text('rescan_required', 'Options changed. Scan again before retiring files.'))
        if self.combo_mode.currentIndex()==4:
            self.status.setText(self.text('phash_scope_changed','Choose the explicit scope and run the pHash phase. Previous accepted batches remain stored.'))
        if self.combo_mode.currentIndex()==5:
            self.status.setText(self.text('recovery_review_changed','Choose a journal and request a fresh review. No changes are made automatically.'))
        self._render_page()
        self._controls()
        if self.combo_mode.currentIndex()==2 and self._db_prepared is None and not self._native_busy:
            self._native_request('get_duplicate_search_status')

    def select_folder(self):
        if self._closed or self._busy or self._acting or self._native_busy or self._phash_busy or self._recovery_busy:
            return
        folder = QFileDialog.getExistingDirectory(self.view, self.text('select_folder', 'Select folder'))
        if folder:
            self.folder_path = folder
            self.lbl_folder.setText(folder)
            self._configuration_changed()

    def select_recovery(self):
        if self._closed or self._busy or self._acting or self._native_busy or self._phash_busy or self._recovery_busy:
            return
        folder = QFileDialog.getExistingDirectory(self.view, self.text('choose_recovery', 'Choose recovery folder'))
        if folder:
            self.recovery.setText(folder)

    def run_scan(self):
        if self._closed or self._busy or self._acting or self._native_busy or self._phash_busy or self._recovery_busy or (self.worker is not None and self.worker.isRunning()):
            return
        if self.combo_mode.currentIndex()==5:
            self._start_recovery_review()
            return
        if self.combo_mode.currentIndex()==4:
            self._start_phash()
            return
        if self.combo_mode.currentIndex()==3:
            self._native_request('get_folder_overlap',folders=self.overlap_panel.paths(),threshold=self.overlap_panel.threshold.value())
            return
        if self.combo_mode.currentIndex()==2:
            self._db_cursors=[None]
            self._native_request('get_duplicate_candidates_page',page=0,after_hash=None,limit=25)
            return
        if not self.folder_path:
            self.status.setText(self.text('no_folder_warn', 'Select a folder first.'))
            return
        self._token += 1
        self._busy = True
        self.review, self._rows, self._checked_paths, self._page = None, [], set(), 0
        self.details.clear()
        self._render_page()
        self.progress.setRange(0, 0)
        self.progress.show()
        self.status.setText(self.text('phase_inventory', 'Reading the folder inventory…'))
        if self.combo_mode.currentIndex() == 0:
            worker = ExactReviewWorker(self.folder_path, self.context.get('data_service'), policy=self._policy(), token=self._token)
        else:
            worker = VisualReviewWorker(self.folder_path, self.slider_thr.value(), token=self._token,service=self.context.get('data_service'))
        self.worker = worker
        _LIVE_WORKERS.add(worker)
        worker.finished.connect(lambda worker=worker: _LIVE_WORKERS.discard(worker))
        worker.finished.connect(self._native_finished)
        worker.progress.connect(self.update_progress)
        worker.phase.connect(self._phase)
        worker.finished_signal.connect(self.on_scan_finished)
        self._controls()
        worker.start()

    @Slot(str)
    def _phase(self, phase):
        if not self._closed and self.sender() is self.worker:
            self.status.setText(self.text('phase_'+phase, 'Preparing the duplicate review…'))

    @Slot(int, int, str)
    def update_progress(self, current, total, message):
        if not self._closed and self.sender() is self.worker:
            self.progress.setRange(0, max(total, 1))
            self.progress.setValue(current)

    @Slot(dict)
    def on_scan_finished(self, result):
        if self._closed or self.sender() is not self.worker or result.get('token') != self._token:
            return
        self.review = result
        self._rows = [(digest, record) for digest, report in result['reports'].items() for record in report['copies']]
        if result['cancelled']:
            message = self.text('cancelled', 'Scan cancelled. No files were changed.')
        elif result['error']:
            message = self.text('scan_failed', 'The scan could not complete: {reason}').format(reason=self.reason(result['error']))
        elif result['issue_count']:
            message = self.text('incomplete', 'Review incomplete: {count} issues. Retirement is disabled.').format(count=result['issue_count'])
        elif result['mode'] == 'visual':
            message = self.text('visual_review_only', 'Visual groups are for review only. Use exact content to retire copies.')
        else:
            message = self.text('scan_summary', '{groups} groups, {copies} copies. Documented copies take priority.').format(groups=len(result['groups']), copies=len(self._rows))
        self.status.setText(message)
        if result['issues']:
            self.action_status.setPlainText(json.dumps(result['issues'], ensure_ascii=False, indent=2))
        self._render_page()

    @Slot()
    def _native_finished(self):
        worker = self.sender()
        if worker is self.worker:
            self.worker = None
            self._busy = False
            if not self._closed:
                self.progress.hide()
                self._render_page()
                self._controls()
        worker.deleteLater()

    def _navigate(self, offset):
        if self._closed or self._busy or self._acting:
            return
        page = self._page+offset
        if 0 <= page <= max(0, (len(self._rows)-1)//100):
            self._page = page
            self._render_page()
            self._controls()

    def _render_page(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        headers = {}
        valid = not self._busy and not self._acting and self._valid_review()
        for digest, record in self._rows[self._page*100:(self._page+1)*100]:
            report, path = self.review['reports'][digest], record['path']
            if digest not in headers:
                header = QTreeWidgetItem([self.text('group_header', 'Group {id} ({count} copies)').format(id=digest[:12], count=len(report['copies']))])
                header.setData(0, Qt.UserRole, (digest, None))
                header.setToolTip(0, self.reason(report['reason']))
                self.tree.addTopLevelItem(header)
                header.setExpanded(True)
                headers[digest] = header
            role = 'candidate' if path in report['remove'] else ('keep' if report['status']=='recommended' else 'review')
            state = record.get('state')
            try:
                label = Path(path).relative_to(Path(self.review['folder'])).as_posix()
            except ValueError:
                label = path
            item = QTreeWidgetItem([label, self.text('role_'+role, role), str(state['file']['rating']) if state else '', str(len(state['tags'])) if state else ''])
            item.setData(0, Qt.UserRole, (digest, path))
            item.setToolTip(0, path)
            item.setToolTip(1, self.reason(report['reason']))
            headers[digest].addChild(item)
            if valid and path in report['remove']:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Checked if path in self._checked_paths else Qt.Unchecked)
            else:
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
        self.tree.blockSignals(False)
        self.page_status.setText(self.text('page', 'Page {page}/{pages} · {count} copies').format(page=self._page+1,
            pages=max(1, (len(self._rows)+99)//100), count=len(self._rows)))
        self.details.clear()

    def _checked(self, item, column):
        if column != 0 or self._closed or self._busy or self._acting or not self._valid_review():
            return
        value = item.data(0, Qt.UserRole)
        if not value or value[1] not in self.review['reports'][value[0]]['remove']:
            return
        if item.checkState(0) == Qt.Checked:
            self._checked_paths.add(value[1])
        else:
            self._checked_paths.discard(value[1])
        self._controls()

    def _details(self, item, previous):
        if self._closed or item is None or self.review is None:
            return
        digest, path = item.data(0, Qt.UserRole)
        report = self.review['reports'][digest]
        value = report if path is None else dict(decision={key: value for key, value in report.items() if key!='copies'},
            copy=next(record for record in report['copies'] if record['path']==path))
        self.details.setPlainText(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        self._controls()

    def open_group(self):
        if self._closed or self._busy or self._acting or self.review is None or self.tree.currentItem() is None:
            return
        digest, _ = self.tree.currentItem().data(0, Qt.UserRole)
        paths = [record['path'] for record in self.review['reports'][digest]['copies']
                 if record['path'].lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.avif'))]
        if not paths or len(paths) > 1000:
            self.action_status.setPlainText(self.text('gallery_group_limit', 'Gallery handoff requires 1 to 1,000 images in the complete group. No images were sent.'))
            return
        self.request_open_gallery.emit(paths, self.text('gallery_group_title', 'Duplicate review'))

    def auto_select_duplicates(self):
        if self._closed or self._busy or self._acting or not self._valid_review():
            return
        self._checked_paths = {path for report in self.review['reports'].values() for path in report['remove']}
        self._render_page()
        self._controls()

    def _confirm_retirement(self, selected, recovery):
        box = QMessageBox(self.view)
        box.setWindowTitle(self.text('confirm_title', 'Review retirement'))
        box.setText(self.text('confirm_retirement', 'Retire {count} exact copies to:\n{folder}\n\nThe retained copies and Cherry download history are preserved.').format(count=len(selected), folder=recovery))
        box.setDetailedText(json.dumps(dict(selected=selected, policy=self.review['policy'],
            groups=[report for report in self.review['reports'].values() if set(selected).intersection(report['remove'])]), ensure_ascii=False, indent=2))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        return box.exec() == QMessageBox.Yes

    def delete_files(self):
        if (self._closed or self._busy or self._acting or self._native_busy or not self._valid_review()
                or (self.combo_mode.currentIndex()==2 and self._db_prepared is not True)
                or (self.retirement_worker is not None and self.retirement_worker.isRunning())):
            return
        selected = sorted(self._checked_paths)
        allowed = {path for report in self.review['reports'].values() for path in report['remove']}
        recovery = self.recovery.text().strip()
        if not recovery or not Path(recovery).is_absolute():
            self.action_status.setPlainText(self.text('recovery_required', 'Choose an absolute persistent recovery folder before retirement.'))
            return
        if not selected or not set(selected) <= allowed or not self._confirm_retirement(selected, recovery):
            return
        # Una señal del event loop modal puede invalidar la aprobación.
        if self._closed or self._busy or self._acting or not self._valid_review():
            return
        self._acting = True
        self._controls()
        self._render_page()
        self.action_status.setPlainText(self.text('retiring', 'Rechecking exact copies and preparing recoverable retirement…'))
        worker = DuplicateRetirementWorker(self.context.get('data_service'), review=self.review,
            selected=selected, recovery_folder=recovery, token=self._token)
        self.retirement_worker = worker
        _LIVE_WORKERS.add(worker)
        worker.finished.connect(lambda worker=worker: _LIVE_WORKERS.discard(worker))
        worker.finished.connect(self._retirement_finished)
        worker.finished_signal.connect(self._retired)
        worker.start()

    @Slot(int, object, object)
    def _retired(self, ticket, result, error):
        if self._closed or self.sender() is not self.retirement_worker or ticket != self._token:
            return
        self._token += 1  # La aprobación se consume también si falló.
        self._checked_paths.clear()
        if error is not None:
            message = self.text('retirement_failed', 'Retirement was not confirmed: {reason}. Scan again before retrying.').format(reason=self.reason(error))
        else:
            if result['complete']:
                message = self.text('retirement_summary', 'Recovered {count} copies. Read the results and journals before retrying.').format(count=result['moved'])
            else:
                reason = next((group['error'] for group in result['groups'] if group['error']), 'unconfirmed')
                message = self.text('retirement_partial', 'Retirement incomplete: {count} copies moved. {reason} Review the catalog and recovery journals before retrying.').format(count=result['moved'], reason=self.reason(reason))
            message += '\n'+json.dumps(result, ensure_ascii=False, indent=2)
            if any(group['archived'] for group in result['groups']):
                if self.review.get('scope')=='database':
                    self.db_tree.clear()
                    self._db_page,self._db_cursors,self._db_has_more=0,[None],False
                bus = self.context.get('event_bus')
                if bus:
                    affected = (sorted({Path(path).parent.as_posix() for path in self.retirement_worker.arguments['selected']})
                        if self.review.get('scope')=='database' else [self.review['folder']])
                    for folder in affected:
                        bus.publish('library_changed',folder)
        self.action_status.setPlainText(message)
        self.status.setText(self.text('rescan_required', 'Scan again before retiring files.'))
        self._render_page()
        self._controls()

    @Slot()
    def _retirement_finished(self):
        worker = self.sender()
        if worker is self.retirement_worker:
            self.retirement_worker = None
            self._acting = False
            if not self._closed:
                self._render_page()
                self._controls()
        worker.deleteLater()

    def shutdown_ready(self):
        return all(worker is None or not worker.isRunning()
                   for worker in (self.worker, self.retirement_worker, self.native_worker,self.phash_worker,self.recovery_review_worker,self.recovery_restore_worker,self.recovery_purge_worker))

    def _db_selected(self):
        if self.view is None or self._closed or self._busy or self._acting or self._native_busy or self._phash_busy or self._recovery_busy:
            return
        self._token += 1
        self.review,self._rows,self._checked_paths,self._page=None,[],set(),0
        self._render_page()
        self._controls()

    def _db_page_request(self, offset):
        page=self._db_page+offset
        if not 0<=page<len(self._db_cursors) or (offset>0 and not self._db_has_more):
            return
        self._native_request('get_duplicate_candidates_page',page=page,after_hash=self._db_cursors[page],limit=25)

    def prepare_database(self):
        if self.combo_mode.currentIndex()==2:
            self._native_request('prepare_duplicate_search')

    def _native_request(self, operation, *, page=0, **arguments):
        if (self._closed or self._busy or self._acting or self._native_busy or self._phash_busy or self._recovery_busy
                or (self.native_worker is not None and self.native_worker.isRunning())):
            return
        self._token+=1
        self.overlap_panel.invalidate()
        self.phash_panel.invalidate()
        self.recovery_panel.invalidate()
        self.review,self._rows,self._checked_paths,self._page=None,[],set(),0
        self._render_page()
        self._native_busy=True
        self._pending_db_page=page
        worker=NativeDuplicateQueryWorker(self.context.get('data_service'),operation,token=self._token,**arguments)
        self.native_worker=worker
        _LIVE_WORKERS.add(worker)
        worker.finished.connect(lambda worker=worker:_LIVE_WORKERS.discard(worker))
        worker.finished.connect(self._native_query_finished)
        worker.finished_signal.connect(self._native_received)
        self.db_status.setText(self.text('db_working','Reading the native catalog or finishing index preparation…'))
        if operation=='get_folder_overlap':
            self.status.setText(self.text('overlap_working','Comparing stored hash sets and classifying folder pairs…'))
        self._controls()
        worker.start()

    @Slot(dict)
    def _native_received(self,result):
        if self._closed or self.sender() is not self.native_worker or result['token']!=self._token:
            return
        if result['cancelled'] or result['error']:
            self.db_status.setText(self.text('db_failed','Catalog action incomplete: {reason}').format(
                reason=self.reason(result['error'] or 'cancelled')))
            if result['operation']=='get_duplicate_candidates_page':
                self.db_tree.clear()
                self._db_has_more=False
            if result['operation']=='get_folder_overlap':
                self.status.setText(self.text('overlap_failed','Folder comparison incomplete: {reason}').format(reason=self.reason(result['error'] or 'cancelled')))
            return
        if result['operation']=='get_folder_overlap':
            value=result['value']
            self.overlap_panel.show_result(value)
            self.status.setText(self.text('overlap_summary','{folders} folders, {pairs} classified pairs. Stored hashes only; inspect coverage before acting.').format(folders=len(value['folders']),pairs=len(value['pairs'])))
            return
        if result['operation'] in ('get_duplicate_search_status','prepare_duplicate_search'):
            self._db_prepared=result['value'] is True if result['operation']=='prepare_duplicate_search' else result['value']['prepared']
            self.db_status.setText(self.text('db_ready','Search index prepared. Find candidates, then verify a complete group.') if self._db_prepared else
                self.text('db_unprepared','Search index is not prepared. Candidate reads are available; global retirement requires preparation.'))
            return
        value=result['value']
        self.db_tree.blockSignals(True)
        self.db_tree.clear()
        for group in value['groups']:
            item=QTreeWidgetItem([group['digest'],str(group['copies']),
                self.text('db_verifiable','Verify current bytes') if group['reviewable'] else self.text('db_large_group','Separate review required (>256 copies)')])
            item.setData(0,Qt.UserRole,group)
            item.setToolTip(0,group['digest']+'\n'+self.text('db_hash_tip','Stored hashes only propose candidates. They never authorize retirement.'))
            self.db_tree.addTopLevelItem(item)
        self.db_tree.blockSignals(False)
        self._db_page=self._pending_db_page
        self._db_has_more=value['has_more']
        self._db_cursors=self._db_cursors[:self._db_page+1]
        if value['has_more']:
            self._db_cursors.append(value['next_after_hash'])
        self.db_status.setText(self.text('db_page','Candidate page {page}: {groups} groups. Select one and verify its current bytes and curation.').format(
            page=self._db_page+1,groups=len(value['groups'])))

    @Slot()
    def _native_query_finished(self):
        worker=self.sender()
        if worker is self.native_worker:
            self.native_worker=None
            self._native_busy=False
            if not self._closed:
                self._controls()
        worker.deleteLater()

    def verify_database_group(self):
        if (self._closed or self._busy or self._acting or self._native_busy or self._phash_busy or self._recovery_busy
                or self.combo_mode.currentIndex()!=2 or self.db_tree.currentItem() is None
                or (self.worker is not None and self.worker.isRunning())):
            return
        group=self.db_tree.currentItem().data(0,Qt.UserRole)
        if not group['reviewable']:
            return
        self._token+=1
        self._busy=True
        self.review,self._rows,self._checked_paths,self._page=None,[],set(),0
        self._render_page()
        self.progress.setRange(0,0)
        self.progress.show()
        worker=DatabaseGroupReviewWorker(str(ProjectPaths.root()),self.context.get('data_service'),group['digest'],
            expected_count=group['copies'],policy=self._policy(),token=self._token)
        self.worker=worker
        _LIVE_WORKERS.add(worker)
        worker.finished.connect(lambda worker=worker:_LIVE_WORKERS.discard(worker))
        worker.finished.connect(self._native_finished)
        worker.progress.connect(self.update_progress)
        worker.phase.connect(self._phase)
        worker.finished_signal.connect(self.on_scan_finished)
        self._controls()
        worker.start()

    def _purge_idle(self):
        return (not self._closed and self.combo_mode.currentIndex()==5 and not self._busy and not self._acting
            and not self._native_busy and not self._phash_busy and not self._recovery_busy
            and (self.recovery_purge_worker is None or not self.recovery_purge_worker.isRunning())
            and self.context.get('data_service') is not None)

    def prepare_selected_purge(self):
        if not self._purge_idle():return
        arguments=self.recovery_panel.selected_purge_request()
        if arguments is None:return
        self.recovery_panel.invalidate_purge()
        self._start_purge_worker('preview',arguments)

    def _confirm_purge(self,arguments,evidence):
        box=QMessageBox(self.view)
        box.setWindowTitle(self.text('purge_confirm_title','Confirm permanent deletion'))
        box.setTextFormat(Qt.PlainText)
        box.setText(self.text('purge_confirm','Permanently delete copy ID {id}?\n\nFile: {target}\nSize: {size} bytes\n\nVerified retained copy ID: {keeper_id}\nRetained file: {keeper}\n\nThis bypasses the Recycle Bin and cannot be undone from recovery. Offline IDs, curation, journals and Cherry receipts remain. This evidence is checked again before deletion.').format(
            id=arguments['copy_id'],target=evidence['destination'],size=evidence['size'],
            keeper_id=evidence['keeper_id'],keeper=evidence['keeper']))
        box.setStandardButtons(QMessageBox.Yes|QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        box.button(QMessageBox.Yes).setText(self.text('purge_confirm_yes','Delete permanently'))
        box.button(QMessageBox.No).setText(self.text('purge_confirm_no','Cancel'))
        box.button(QMessageBox.Yes).setToolTip(self.text('purge_confirm_yes_tip','Confirm only this verified file. It will be permanently deleted after verification is repeated.'))
        box.button(QMessageBox.No).setToolTip(self.text('purge_confirm_no_tip','Close without admitting deletion. The current preview is retained.'))
        try:return box.exec()==QMessageBox.Yes
        finally:
            try:box.deleteLater()
            except RuntimeError:pass

    def purge_selected_copy(self):
        if not self._purge_idle():return
        arguments=self.recovery_panel.selected_purge()
        if arguments is None:return
        # Captura de valores concretos antes de un modal reentrante.
        evidence=deepcopy(self.recovery_panel.purge_preview['evidence'])
        ticket=self._token
        if not self._confirm_purge(arguments,evidence):return
        if not self._purge_idle() or ticket!=self._token or arguments!=self.recovery_panel.selected_purge():return
        self.recovery_panel.invalidate()
        self._start_purge_worker('purge',arguments)

    def _start_purge_worker(self,operation,arguments):
        self._acting=True;self._token+=1
        self._purge_request=arguments
        self.progress.setRange(0,0);self.progress.show()
        self.status.setText(self.text('purge_preview_working','Verifying the selected recovery copy and the retained copy…') if operation=='preview' else
            self.text('purge_working','Repeating verification and preparing a separate permanent deletion journal…'))
        keys=('journal','copy_id','journal_fingerprint','expected_fingerprint')+(('preview_fingerprint',) if operation=='purge' else ())
        worker=RecoveryPurgeWorker(self.context['data_service'],operation=operation,token=self._token,
            **{key:arguments[key] for key in keys})
        self.recovery_purge_worker=worker
        _LIVE_WORKERS.add(worker)
        worker.finished.connect(lambda worker=worker:_LIVE_WORKERS.discard(worker))
        worker.finished.connect(self._recovery_purge_finished)
        worker.finished_signal.connect(self._recovery_purge_received)
        self._controls();worker.start()

    @Slot(dict)
    def _recovery_purge_received(self,result):
        if self._closed or self.sender() is not self.recovery_purge_worker or result['token']!=self._token:return
        self._token+=1
        if result['operation']=='preview':
            if self._purge_request!=self.recovery_panel.selected_purge_request():
                self.recovery_panel.invalidate_purge()
                self.status.setText(self.text('purge_selection_changed','Selection changed; prepare a new preview for the selected copy.'))
                return
            self.recovery_panel.show_purge_preview(result,self.reason)
            message=self.text('purge_preview_ready','Preview ready. Inspect the copy and retained file before confirming permanent deletion.') if result['complete'] else (
                self.text('purge_preview_cancelled','Deletion preview cancelled. No file was deleted.') if result['cancelled'] else
                self.text('purge_preview_failed','Deletion preview incomplete: {reason}.').format(reason=self.reason(result['error'])))
        else:
            self.recovery_panel.show_purge(result,self.reason)
            message=self.text('purge_summary','Copy ID {id} permanently deleted; offline identity, curation and Cherry receipts retained.').format(id=result['copy_id']) if result['complete'] else (
                self.text('purge_cancelled','Deletion cancelled before marking the file. No file was deleted.') if result['cancelled'] else
                self.text('purge_journal_partial','File deletion confirmed; operation journal incomplete: {reason}. Keep the journals for review.').format(reason=self.reason(result['error'])) if result['deleted'] else
                self.text('purge_partial','File deletion not confirmed: {reason}. Inspect the operation journal before retrying.').format(reason=self.reason(result['error'])))
        # Recuperación está fuera del scope operativo; no indexar esa carpeta.
        self.status.setText(message)

    @Slot()
    def _recovery_purge_finished(self):
        worker=self.sender()
        if worker is self.recovery_purge_worker:
            self.recovery_purge_worker=None;self._purge_request=None;self._acting=False
            if not self._closed:self.progress.hide();self._controls()
        worker.deleteLater()

    def _confirm_restore(self,arguments):
        box=QMessageBox(self.view)
        box.setWindowTitle(self.text('restore_confirm_title','Review restoration'))
        box.setTextFormat(Qt.PlainText)
        action=self.text('restore_confirm_move','Return copy ID {id} from:\n{destination}\n\nTo its original path:\n{source}') if arguments['observed']=='retired_verified' else self.text('restore_confirm_online','Reactivate copy ID {id} at its intact original path:\n{source}')
        box.setText(action.format(id=arguments['copy_id'],source=arguments['source'],destination=arguments['destination'])+'\n\n'+self.text('restore_confirm_note','Preserves the same ID, curation and Cherry download receipts. Rechecks before admission and never overwrites an occupied path. The original retirement journal is retained; restoration has its own journal.'))
        box.setStandardButtons(QMessageBox.Yes|QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        box.button(QMessageBox.Yes).setText(self.text('restore_confirm_yes','Restore'))
        box.button(QMessageBox.No).setText(self.text('restore_confirm_no','Cancel'))
        box.button(QMessageBox.Yes).setToolTip(self.text('restore_confirm_yes_tip','Confirm this copy and these paths; the worker checks them again before admission.'))
        box.button(QMessageBox.No).setToolTip(self.text('restore_confirm_no_tip','Close without admitting restoration; the current reviewed evidence is retained.'))
        try:return box.exec()==QMessageBox.Yes
        finally:
            try:box.deleteLater()
            except RuntimeError:pass  # El padre puede haber sido destruido en el modal.

    def restore_selected_copy(self):
        if (self.combo_mode.currentIndex()!=5 or self._closed or self._busy or self._acting or self._native_busy or self._phash_busy or self._recovery_busy
                or (self.recovery_restore_worker is not None and self.recovery_restore_worker.isRunning())):
            return
        arguments=self.recovery_panel.selected_restore()
        if arguments is None or self.context.get('data_service') is None:return
        ticket=self._token
        if not self._confirm_restore(arguments):return
        # La aprobación pertenece a esta copia/ruta/huellas, capturadas antes del modal.
        if (self._closed or self._busy or self._acting or self._native_busy or self._phash_busy or self._recovery_busy
                or ticket!=self._token or self.combo_mode.currentIndex()!=5 or arguments!=self.recovery_panel.selected_restore()):
            return
        self._acting=True
        self._token+=1
        self.recovery_panel.invalidate()
        self.progress.setRange(0,0);self.progress.show()
        self.status.setText(self.text('restore_working','Rechecking the selected copy and preparing a separate restoration journal…'))
        worker=RecoveryRestoreWorker(self.context['data_service'],token=self._token,
            **{key:arguments[key] for key in ('journal','copy_id','journal_fingerprint','expected_fingerprint')})
        self.recovery_restore_worker=worker
        _LIVE_WORKERS.add(worker)
        worker.finished.connect(lambda worker=worker:_LIVE_WORKERS.discard(worker))
        worker.finished.connect(self._recovery_restore_finished)
        worker.finished_signal.connect(self._recovery_restored)
        self._controls();worker.start()

    @Slot(dict)
    def _recovery_restored(self,result):
        if self._closed or self.sender() is not self.recovery_restore_worker:return
        if (result['moved'] or result['committed']) and result['source']:
            bus=self.context.get('event_bus')
            if bus:bus.publish('library_changed',Path(result['source']).parent.as_posix())
        if result['token']!=self._token:return
        self._token+=1
        self.recovery_panel.show_restore(result,self.reason)
        if result['complete']:
            message=self.text('restore_summary','Copy ID {id} restored with curation preserved. Request a fresh journal review.').format(id=result['copy_id'])
        elif result['cancelled']:
            message=self.text('restore_cancelled','Restoration cancelled before admission. No file was moved or ID reactivated.')
        elif result['committed']:
            message=self.text('restore_journal_partial','Native restoration confirmed; operation journal incomplete: {reason}. Keep the bytes and journals for review.').format(reason=self.reason(result['error']))
        else:
            message=self.text('restore_partial','Restoration not confirmed: {reason}. Inspect the paths and operation journal before retrying.').format(reason=self.reason(result['error']))
        self.status.setText(message)

    @Slot()
    def _recovery_restore_finished(self):
        worker=self.sender()
        if worker is self.recovery_restore_worker:
            self.recovery_restore_worker=None
            self._acting=False
            if not self._closed:
                self.progress.hide();self._controls()
        worker.deleteLater()

    def _start_recovery_review(self):
        if (self.combo_mode.currentIndex()!=5 or self._closed or self._busy or self._acting or self._native_busy or self._phash_busy or self._recovery_busy
                or (self.recovery_review_worker is not None and self.recovery_review_worker.isRunning())):
            return
        if self.context.get('data_service') is None:
            self.status.setText(self.text('recovery_review_service_required','The native library service is required to review IDs and curation.'))
            return
        self._token+=1
        self._recovery_busy=True
        self.review,self._rows,self._checked_paths,self._page=None,[],set(),0
        self.recovery_panel.invalidate()
        self._render_page()
        self.progress.setRange(0,0)
        self.progress.show()
        self.status.setText(self.text('recovery_review_working','Checking journal, native identities and both file locations…'))
        worker=RecoveryReviewWorker(self.context['data_service'],self.recovery_panel.path(),token=self._token)
        self.recovery_review_worker=worker
        _LIVE_WORKERS.add(worker)
        worker.finished.connect(lambda worker=worker:_LIVE_WORKERS.discard(worker))
        worker.finished.connect(self._recovery_review_finished)
        worker.finished_signal.connect(self._recovery_review_received)
        worker.progress.connect(self._recovery_review_progress)
        self._controls()
        worker.start()

    @Slot(int,int,str)
    def _recovery_review_progress(self,current,total,path):
        worker=self.sender()
        if not self._closed and worker is self.recovery_review_worker and worker.token==self._token:
            self.progress.setRange(0,max(1,total));self.progress.setValue(current)

    @Slot(dict)
    def _recovery_review_received(self,result):
        if self._closed or self.sender() is not self.recovery_review_worker or result['token']!=self._token:return
        self.recovery_panel.show_result(result,self.reason)
        if result['error']:
            self.status.setText(self.text('recovery_review_failed','Journal review incomplete: {reason}').format(reason=self.reason(result['error'])))
        elif result['cancelled']:
            self.status.setText(self.text('recovery_review_cancelled','Journal review cancelled. No changes were made.'))
        else:
            self.status.setText(self.text('recovery_review_summary','Journal reviewed: {verified} verified retirements · {pending} requiring review. No changes were made.').format(
                verified=result['verified_retired'],pending=len(result['moves'])-result['verified_retired']))

    @Slot()
    def _recovery_review_finished(self):
        worker=self.sender()
        if worker is self.recovery_review_worker:
            self.recovery_review_worker=None
            self._recovery_busy=False
            if not self._closed:
                self.progress.hide();self._controls()
        worker.deleteLater()

    def _start_phash(self):
        if self.combo_mode.currentIndex()!=4 or self._closed or self._busy or self._acting or self._native_busy or self._phash_busy or self._recovery_busy or (self.phash_worker is not None and self.phash_worker.isRunning()):
            return
        if self.context.get('data_service') is None:
            self.status.setText(self.text('phash_service_required','The native library service is required to store pHash.'))
            return
        self._token+=1
        self._phash_busy=True
        self.review,self._rows,self._checked_paths,self._page=None,[],set(),0
        self.phash_panel.invalidate()
        self.recovery_panel.invalidate()
        self._render_page()
        self.progress.setRange(0,0)
        self.progress.show()
        self.status.setText(self.text('phash_working','Verifying current bytes and preparing persistent pHash…'))
        worker=PerceptualBackfillWorker(self.context.get('data_service'),self.phash_panel.paths(),token=self._token,
            pixel_limit=self.phash_panel.pixels.value(),max_images=self.phash_panel.images.value())
        self.phash_worker=worker
        _LIVE_WORKERS.add(worker)
        worker.finished.connect(lambda worker=worker:_LIVE_WORKERS.discard(worker))
        worker.finished.connect(self._phash_finished)
        worker.finished_signal.connect(self._phash_received)
        worker.progress.connect(self._phash_progress)
        self._controls()
        worker.start()

    @Slot(int,int,str)
    def _phash_progress(self,current,total,path):
        if not self._closed and self.sender() is self.phash_worker:
            self.progress.setRange(0,max(1,total))
            self.progress.setValue(current)

    @Slot(dict)
    def _phash_received(self,result):
        if self._closed or self.sender() is not self.phash_worker or result['token']!=self._token:return
        self.phash_panel.show_result(result,self.reason)
        summary=self.text('phash_summary','Stored: {stored} · Reused: {reused} · Issues: {issues}. Confirmed batches are preserved.').format(
            stored=result['persisted'],reused=result['reused'],issues=result['issue_count'])
        if result['error']:summary=self.text('phash_failed','pHash phase incomplete: {reason}').format(reason=self.reason(result['error']))+'\n'+summary
        elif result['cancelled']:summary=self.text('phash_cancelled','pHash phase cancelled; accepted writes completed.')+'\n'+summary
        elif not result['complete']:summary=self.text('phash_partial','pHash phase has pending issues. Inspect the result before retrying.')+'\n'+summary
        self.status.setText(summary)

    @Slot()
    def _phash_finished(self):
        worker=self.sender()
        if worker is self.phash_worker:
            self.phash_worker=None
            self._phash_busy=False
            if not self._closed:
                self.progress.hide()
                self._controls()
        worker.deleteLater()

    def reason(self, error):
        code = str(getattr(error, 'reason', error)).rsplit(': ', 1)[-1]
        return self.text('reason_'+code, code)

    def cancel(self):
        if self._closed:
            return
        if self.worker is not None:
            self.worker.stop()
        if self.retirement_worker is not None:
            self.retirement_worker.stop()
        if self.native_worker is not None:
            self.native_worker.stop()
        if self.phash_worker is not None:
            self.phash_worker.stop()
        if self.recovery_review_worker is not None:
            self.recovery_review_worker.stop()
        if self.recovery_restore_worker is not None:
            self.recovery_restore_worker.stop()
        if self.recovery_purge_worker is not None:
            self.recovery_purge_worker.stop()

    def on_unload(self, *unused):
        self._closed = True
        self._token += 1
        if self.worker is not None:
            self.worker.stop()
        if self.retirement_worker is not None:
            self.retirement_worker.stop()
        if self.native_worker is not None:
            self.native_worker.stop()
        if self.phash_worker is not None:
            self.phash_worker.stop()
        if self.recovery_review_worker is not None:
            self.recovery_review_worker.stop()
        if self.recovery_restore_worker is not None:
            self.recovery_restore_worker.stop()
        if self.recovery_purge_worker is not None:
            self.recovery_purge_worker.stop()

    def run_headless(self, params, input_data):
        raise NotImplementedError('duplicate_review_requires_explicit_approval')
