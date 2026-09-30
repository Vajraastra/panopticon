"""Revisión paginada y elección explícita; sin conexiones SQLite en Qt."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QPlainTextEdit, QHeaderView)
from core.library_async import LibraryReader
from core.curation_review import CurationReviewTasks
from core.curation_apply import CurationApplyTasks

_ERROR_DEFAULTS = {
    'sidecar_changed': 'The export changed during this review',
    'sidecar_count_mismatch': 'The declared copy count does not match the available records',
    'sidecar_payload_too_large': 'One copy contains more tag data than the comparison can read',
    'curation_changed_since_review': 'The current curation changed since this page was read',
    'tag_category_conflict': 'An existing shared tag has a different category',
    'curation_hash_mismatch': 'The file bytes do not match the exported identity',
    'curation_file_changed': 'The file changed during verification',
    'curation_file_symlink': 'This file is a symbolic link and requires separate review',
    'identity_changed': 'The registered identity differs from the export',
    'identity_missing': 'This copy has no known identity',
    'restore_payload_too_large': 'This copy has too many tags to restore',
    'invalid_restore_tag': 'A tag cannot be restored without changing its format',
    'file_now_registered': 'This path was registered after the review; restart to compare its curation',
    'identity_already_known': 'The copy already has a known identity; restart the review',
    'invalid_curation_observation': 'The current file observations cannot be registered',
}


class CurationReviewDialog(QDialog):
    curation_restored = Signal(str)

    def __init__(self, db_path, folder, translate, theme, parent=None, service=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.tr_text = translate
        self.folder = str(folder)
        self._closed = False
        self._busy = False
        self._valid_page = False
        self._complete = False
        self._restoring = None
        self._token = None
        self._total = None
        self._after = 0
        self._seen = 0
        self._reports = []
        self.reader = LibraryReader(CurationReviewTasks(db_path), self)
        self.reader.result.connect(self._received)
        self.apply_reader = LibraryReader(CurationApplyTasks(service), self) if service is not None else None
        if self.apply_reader is not None:
            self.apply_reader.result.connect(self._restored)
        self.setWindowTitle(translate('lib.review.title', 'Review folder curation'))
        self.resize(950, 650)
        layout = QVBoxLayout(self)
        heading = QLabel(self.folder)
        heading.setTextFormat(Qt.PlainText)
        heading.setWordWrap(True)
        layout.addWidget(heading)
        note = QLabel(translate('lib.review.note',
            'Comparison does not apply curation. Explicitly choose the export to replace curation, '
            'register an absent copy or complete its unknown central identity. '
            'Each page reflects the central database when it is read.'))
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([translate('lib.review.path', 'Relative path'),
                                             translate('lib.review.status', 'Status')])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setToolTip(translate('lib.review.table_tip',
            'Select a copy to compare its exported and current tags, sources, ratings and hashes.'))
        self.table.itemSelectionChanged.connect(self._details)
        layout.addWidget(self.table, 2)
        detail_row = QHBoxLayout()
        self.sidecar_text = QPlainTextEdit()
        self.central_text = QPlainTextEdit()
        for editor, key, default in ((self.sidecar_text, 'sidecar', 'Exported sidecar'),
                                     (self.central_text, 'central', 'Current central database')):
            column = QVBoxLayout()
            column.addWidget(QLabel(translate('lib.review.' + key, default)))
            editor.setReadOnly(True)
            editor.setToolTip(translate('lib.review.details_tip',
                'Plain text preview limited to 16,384 characters per state. '
                'Use the compare command for the full record if this preview is incomplete.'))
            column.addWidget(editor)
            detail_row.addLayout(column)
        layout.addLayout(detail_row, 2)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.restore_status = QLabel()
        self.restore_status.setTextFormat(Qt.PlainText)
        self.restore_status.setWordWrap(True)
        layout.addWidget(self.restore_status)
        controls = QHBoxLayout()
        self.restart_button = QPushButton(translate('lib.review.restart', 'Restart review'))
        self.restart_button.setToolTip(translate('lib.review.restart_tip',
            'Read the current export again from its first page; discard this traversal.'))
        self.restart_button.clicked.connect(self.restart)
        self.next_button = QPushButton(translate('lib.review.next', 'Next page'))
        self.next_button.setToolTip(translate('lib.review.next_tip',
            'Compare up to 100 more copies without keeping earlier pages in memory.'))
        self.next_button.clicked.connect(self._request)
        self.apply_button = QPushButton(translate('lib.review.apply', 'Use exported curation'))
        self.apply_button.setToolTip(translate('lib.review.apply_tip',
            'Replace only this registered copy’s tags, sources and rating with its exported values. '
            'Requires known identity and complete previews; verifies file bytes in a worker and rejects changes since review.'))
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_selected)
        self.register_button = QPushButton(translate('lib.review.register', 'Register and restore copy'))
        self.register_button.setToolTip(translate('lib.review.register_tip',
            'Explicitly register only this absent path or complete both missing hashes and restore its exported curation. '
            'Verifies current bytes first; keeps other copies and rejects concurrent changes.'))
        self.register_button.setEnabled(False)
        self.register_button.clicked.connect(self.register_selected)
        self.close_button = QPushButton(translate('lib.review.close', 'Close'))
        self.close_button.setToolTip(translate('lib.review.close_tip',
            'Close this review. An explicitly accepted restoration continues in the database service.'))
        self.close_button.clicked.connect(self.close)
        for button in (self.restart_button, self.next_button, self.apply_button, self.register_button, self.close_button):
            controls.addWidget(button)
        layout.addLayout(controls)
        if theme is not None:
            self.setStyleSheet(theme.get_stylesheet())
            c = theme.get_color
            for button in (self.restart_button, self.next_button, self.apply_button, self.register_button, self.close_button):
                button.setStyleSheet(f"background-color: {c('bg_input')}; color: {c('text_primary')}; "
                                    f"border: 1px solid {c('border_strong')}; padding: 6px;")
            for editor in (self.sidecar_text, self.central_text):
                editor.setStyleSheet(f"background-color: {c('bg_input')}; color: {c('text_primary')}; "
                                    f"border: 1px solid {c('border')};")
        self.restart()

    def restart(self):
        if self._closed or self._busy or self._restoring is not None:
            return
        self._token = self._total = None
        self._complete = False
        self._after = self._seen = 0
        self._reports = []
        self.table.setRowCount(0)
        self._details()
        self._request()

    def _request(self):
        if self._closed or self._busy or self._restoring is not None:
            return
        self._busy = True
        self._valid_page = False
        self.restart_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.register_button.setEnabled(False)
        self.status.setText(self.tr_text('lib.review.loading', 'Reading comparison page…'))
        self.reader.request('compare_curation', folder=self.folder, after=self._after, export_id=self._token)

    def _received(self, ticket, value, error):
        if self._closed:
            return
        self._busy = False
        self.restart_button.setEnabled(True)
        if error is None:
            if self._total is not None and value['total'] != self._total:
                error = ValueError('sidecar_changed')
            elif self._seen + len(value['reports']) > value['total']:
                error = ValueError('sidecar_count_mismatch')
            elif not value['reports'] and self._seen != value['total']:
                error = ValueError('sidecar_count_mismatch')
        if error is not None:
            self.next_button.setEnabled(False)
            reason = self.tr_text('lib.review.error.' + str(error), _ERROR_DEFAULTS.get(str(error), str(error)))
            self.status.setText(self.tr_text('lib.review.error',
                'Comparison stopped: {reason}. Restart to read the current export.').format(reason=reason))
            self.apply_button.setEnabled(False)
            self.register_button.setEnabled(False)
            return
        self._token, self._total = value['export_id'], value['total']
        self._valid_page = True
        self._after = value['after']
        if not value['reports']:
            self._complete = True
            self.next_button.setEnabled(False)
            self.status.setText(self.tr_text('lib.review.complete',
                'Traversal complete: {count} copies compared.').format(count=self._seen))
            self._update_apply()
            return
        self._seen += len(value['reports'])
        self._reports = value['reports']
        self.table.setRowCount(len(self._reports))
        for row, report in enumerate(self._reports):
            self.table.setItem(row, 0, QTableWidgetItem(report['relative_path']))
            code = report['status']
            self.table.setItem(row, 1, QTableWidgetItem(self.tr_text('lib.review.state.' + code, code)))
        self.table.selectRow(0)
        self.next_button.setEnabled(True)
        self.status.setText(self.tr_text('lib.review.page',
            'Compared {seen} of {total} copies. Earlier pages are not retained.').format(
                seen=self._seen, total=self._total))
        self._update_apply()

    def _details(self):
        row = self.table.currentRow()
        for editor, key in ((self.sidecar_text, 'sidecar'), (self.central_text, 'central')):
            text = ''
            if 0 <= row < len(self._reports):
                preview = self._reports[row][key]
                text = preview['text'] or self.tr_text('lib.review.absent', 'No registered copy at this path.')
                if preview['truncated']:
                    text += '\n' + self.tr_text('lib.review.truncated',
                        'Incomplete preview; total tags: {count}. Use compare for the full record.').format(
                            count=preview['tags'])
            editor.setPlainText(text)
        self._update_apply()

    def _update_apply(self):
        if not hasattr(self, 'apply_button'):
            return
        row = self.table.currentRow()
        report = self._reports[row] if 0 <= row < len(self._reports) else None
        eligible = (not self._closed and not self._busy and self._valid_page and self._restoring is None and
            self.apply_reader is not None and report is not None and
            not report['sidecar']['truncated'] and not report['central']['truncated'])
        self.apply_button.setEnabled(bool(eligible and report['status'] in ('same', 'conflict')))
        self.register_button.setEnabled(bool(eligible and report.get('known_identity') and
            (report['status'] == 'unregistered' and report['fingerprint'] is None or
             report['status'] == 'central_identity_missing' and report['fingerprint'] is not None)))
        adopting = report is not None and report['status'] == 'central_identity_missing'
        self.register_button.setText(self.tr_text('lib.review.adopt' if adopting else 'lib.review.register',
            'Complete identity and restore' if adopting else 'Register and restore copy'))

    def apply_selected(self):
        if not self.apply_button.isEnabled():
            return
        self._start_restore('restore')

    def register_selected(self):
        if not self.register_button.isEnabled():
            return
        report = self._reports[self.table.currentRow()]
        self._start_restore('register' if report['status'] == 'unregistered' else 'adopt')

    def _start_restore(self, mode):
        report = self._reports[self.table.currentRow()]
        self._restoring = report['relative_path']
        self.restart_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.register_button.setEnabled(False)
        self.restore_status.setText(self.tr_text('lib.review.restoring',
            'Verifying and restoring {path}…').format(path=self._restoring))
        self.apply_reader.request('apply_curation', folder=self.folder, copy_id=report['copy_id'],
            relative_path=report['relative_path'], export_id=self._token, expected_fingerprint=report['fingerprint'], mode=mode)

    def _restored(self, ticket, value, error):
        if self._closed or self._restoring is None:
            return
        path, self._restoring = self._restoring, None
        self.restart_button.setEnabled(True)
        if error is not None or value is not True:
            reason = getattr(error, 'reason', str(error)) if error is not None else 'restore_not_confirmed'
            reason = self.tr_text('lib.review.error.' + reason, _ERROR_DEFAULTS.get(reason, reason))
            self.restore_status.setText(self.tr_text('lib.review.restore_error',
                'Could not restore {path}: {reason}. Restart the review if the state changed.').format(path=path, reason=reason))
            self.next_button.setEnabled(self._valid_page and not self._complete)
            self._update_apply()
        else:
            self.restore_status.setText(self.tr_text('lib.review.restored',
                'Restored exported curation for {path}. Review restarted.').format(path=path))
            self.curation_restored.emit(path)
            self.restart()

    def closeEvent(self, event):
        self._closed = True
        self.reader.close()
        if self.apply_reader is not None:
            self.apply_reader.close()
        super().closeEvent(event)
