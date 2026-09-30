"""Revisión de duplicados; preferencias por curación y retiro recuperable explícito."""
from dataclasses import asdict
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
        self.btn_cancel.setToolTip(self.text('cancel_tip', 'Stop preparation without blocking the interface. An accepted retirement group is not cancelled; its recovery journal preserves the plan if interrupted.'))
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
        idle = not self._closed and not self._busy and not self._acting and not self._native_busy
        database = self.combo_mode.currentIndex()==2
        overlap = self.combo_mode.currentIndex()==3
        for control in (self.btn_run, self.btn_select, self.combo_mode, self.slider_thr, self.recovery, self.btn_recovery, *self.weights.values()):
            control.setEnabled(idle)
        self.threshold_container.setVisible(self.combo_mode.currentIndex() == 1)
        self.weight_box.setVisible(self.combo_mode.currentIndex() in (0,2))
        self.db_box.setVisible(database)
        self.lbl_folder.setText(self.text('db_scope','Scope: all active indexed copies') if database else
            (self.folder_path or self.text('no_folder','No folder selected')))
        self.btn_select.setEnabled(idle and not database and not overlap)
        self.btn_select.setVisible(not overlap)
        self.lbl_folder.setVisible(not overlap)
        self.overlap_panel.setVisible(overlap)
        self.overlap_panel.set_idle(idle)
        for widget in (self.tree,self.details,self.action_status,self.btn_previous,self.btn_next,self.btn_open_group,self.page_status):
            widget.setVisible(not overlap)
        for widget in (self.recovery_label,self.recovery,self.btn_recovery,self.policy_note,self.btn_auto_select,self.btn_delete):
            widget.setVisible(not overlap)
        self.btn_run.setText(self.text('overlap_run','Compare folder pairs') if overlap else
            self.text('db_search', 'Find native candidates') if database else self.text('run_scan','Start scan'))
        self.btn_run.setToolTip(self.text('overlap_run_tip','Compare all pairs of selected folders by stored unique hashes and show coverage. No image reads, merges or retirement occur.') if overlap else
            self.text('db_search_tip', 'Read a page of stored-hash candidates. Physical bytes and curation still require group verification.') if database else self.text('run_scan_tip','Read current files and documented states. No database records or file bytes are changed by scanning.'))
        if overlap:self.btn_run.setEnabled(idle and 2<=len(self.overlap_panel.paths())<=32)
        self.lbl_thr_val.setText(self.text('value', 'Value: {value}').format(value=self.slider_thr.value()))
        self.btn_cancel.setEnabled(not self._closed and (self._busy or self._acting or self._native_busy))
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
        self._checked_paths.clear()
        if self.review:
            self.status.setText(self.text('rescan_required', 'Options changed. Scan again before retiring files.'))
        self._render_page()
        self._controls()
        if self.combo_mode.currentIndex()==2 and self._db_prepared is None and not self._native_busy:
            self._native_request('get_duplicate_search_status')

    def select_folder(self):
        if self._closed or self._busy or self._acting or self._native_busy:
            return
        folder = QFileDialog.getExistingDirectory(self.view, self.text('select_folder', 'Select folder'))
        if folder:
            self.folder_path = folder
            self.lbl_folder.setText(folder)
            self._configuration_changed()

    def select_recovery(self):
        if self._closed or self._busy or self._acting or self._native_busy:
            return
        folder = QFileDialog.getExistingDirectory(self.view, self.text('choose_recovery', 'Choose recovery folder'))
        if folder:
            self.recovery.setText(folder)

    def run_scan(self):
        if self._closed or self._busy or self._acting or self._native_busy or (self.worker is not None and self.worker.isRunning()):
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
            worker = VisualReviewWorker(self.folder_path, self.slider_thr.value(), token=self._token)
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
                   for worker in (self.worker, self.retirement_worker, self.native_worker))

    def _db_selected(self):
        if self.view is None or self._closed or self._busy or self._acting or self._native_busy:
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
        if (self._closed or self._busy or self._acting or self._native_busy
                or (self.native_worker is not None and self.native_worker.isRunning())):
            return
        self._token+=1
        self.overlap_panel.invalidate()
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
        if (self._closed or self._busy or self._acting or self._native_busy
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

    def on_unload(self, *unused):
        self._closed = True
        self._token += 1
        if self.worker is not None:
            self.worker.stop()
        if self.retirement_worker is not None:
            self.retirement_worker.stop()
        if self.native_worker is not None:
            self.native_worker.stop()

    def run_headless(self, params, input_data):
        raise NotImplementedError('duplicate_review_requires_explicit_approval')
