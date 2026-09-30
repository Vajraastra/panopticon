"""Revisión secuencial no modal; nunca aplica curación ni comparte conexiones."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QPlainTextEdit, QHeaderView)
from core.library_async import LibraryReader
from core.curation_review import CurationReviewTasks

_ERROR_DEFAULTS = {
    'sidecar_changed': 'The export changed during this review',
    'sidecar_count_mismatch': 'The declared copy count does not match the available records',
    'sidecar_payload_too_large': 'One copy contains more tag data than the comparison can read',
}


class CurationReviewDialog(QDialog):
    def __init__(self, db_path, folder, translate, theme, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.tr_text = translate
        self.folder = str(folder)
        self._closed = False
        self._busy = False
        self._token = None
        self._total = None
        self._after = 0
        self._seen = 0
        self._reports = []
        self.reader = LibraryReader(CurationReviewTasks(db_path), self)
        self.reader.result.connect(self._received)
        self.setWindowTitle(translate('lib.review.title', 'Review folder curation'))
        self.resize(950, 650)
        layout = QVBoxLayout(self)
        heading = QLabel(self.folder)
        heading.setTextFormat(Qt.PlainText)
        heading.setWordWrap(True)
        layout.addWidget(heading)
        note = QLabel(translate('lib.review.note',
            'Read-only comparison. Conflicts keep both values. No curation is applied. '
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
        controls = QHBoxLayout()
        self.restart_button = QPushButton(translate('lib.review.restart', 'Restart review'))
        self.restart_button.setToolTip(translate('lib.review.restart_tip',
            'Read the current export again from its first page; discard this traversal.'))
        self.restart_button.clicked.connect(self.restart)
        self.next_button = QPushButton(translate('lib.review.next', 'Next page'))
        self.next_button.setToolTip(translate('lib.review.next_tip',
            'Compare up to 100 more copies without keeping earlier pages in memory.'))
        self.next_button.clicked.connect(self._request)
        self.close_button = QPushButton(translate('lib.review.close', 'Close'))
        self.close_button.setToolTip(translate('lib.review.close_tip',
            'Close this comparison without changing tags, ratings or the export.'))
        self.close_button.clicked.connect(self.close)
        for button in (self.restart_button, self.next_button, self.close_button):
            controls.addWidget(button)
        layout.addLayout(controls)
        if theme is not None:
            self.setStyleSheet(theme.get_stylesheet())
            c = theme.get_color
            for button in (self.restart_button, self.next_button, self.close_button):
                button.setStyleSheet(f"background-color: {c('bg_input')}; color: {c('text_primary')}; "
                                    f"border: 1px solid {c('border_strong')}; padding: 6px;")
            for editor in (self.sidecar_text, self.central_text):
                editor.setStyleSheet(f"background-color: {c('bg_input')}; color: {c('text_primary')}; "
                                    f"border: 1px solid {c('border')};")
        self.restart()

    def restart(self):
        if self._closed or self._busy:
            return
        self._token = self._total = None
        self._after = self._seen = 0
        self._reports = []
        self.table.setRowCount(0)
        self._details()
        self._request()

    def _request(self):
        if self._closed or self._busy:
            return
        self._busy = True
        self.restart_button.setEnabled(False)
        self.next_button.setEnabled(False)
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
            return
        self._token, self._total = value['export_id'], value['total']
        self._after = value['after']
        if not value['reports']:
            self.next_button.setEnabled(False)
            self.status.setText(self.tr_text('lib.review.complete',
                'Traversal complete: {count} copies compared; no changes applied.').format(count=self._seen))
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

    def closeEvent(self, event):
        self._closed = True
        self.reader.close()
        super().closeEvent(event)
