"""Catálogo explícito por ID; consultas y verificación de bytes fuera de Qt."""
import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QPlainTextEdit, QHeaderView, QComboBox)
from core.library_async import LibraryReader
from core.copy_version_tasks import CopyVersionTasks


class CopyVersionsDialog(QDialog):
    copies_changed = Signal()

    def __init__(self, service, translate, theme=None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.tr_text = translate
        self._closed = self._busy = self._acting = False
        self._after = None
        self._copies = []
        self._selected_id = None
        self._reviewed = None
        self._has_more = False
        self.reader = LibraryReader(service, self)
        self.details_reader = LibraryReader(service, self)
        self.action_reader = LibraryReader(CopyVersionTasks(service), self)
        self.reader.result.connect(self._received)
        self.details_reader.result.connect(self._details_received)
        self.action_reader.result.connect(self._acted)
        self.finished.connect(self._shutdown)
        self.setWindowTitle(self.text('title', 'Copy versions and offline catalog'))
        self.resize(1000, 680)
        layout = QVBoxLayout(self)
        note = QLabel(self.text('note', 'Offline records keep their identity and curation. '
            'Archive only changes the catalog. Verify the file to activate the same known content; '
            'different or unknown content creates a separate version with empty curation. '
            'These actions never change image bytes or Cherry download history.'))
        note.setWordWrap(True)
        layout.addWidget(note)
        self.scope = QComboBox()
        self.scope.addItem(self.text('offline_only', 'Offline copies'), True)
        self.scope.addItem(self.text('all', 'All copy versions'), False)
        self.scope.setToolTip(self.text('scope_tip',
            'Include active copies to archive them explicitly or verify a replaced file.'))
        self.scope.currentIndexChanged.connect(self.restart)
        layout.addWidget(self.scope)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([self.text(key, default) for key, default in
            (('id', 'Copy ID'), ('path', 'Path'), ('state', 'State'), ('hash', 'Current hash'), ('rating', 'Rating'))])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for column in (0, 2, 4):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Interactive)
        self.table.setColumnWidth(3, 220)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setToolTip(self.text('table_tip',
            'Select a specific copy ID to read its complete stored state before accepting an action.'))
        self.table.itemSelectionChanged.connect(self._select)
        layout.addWidget(self.table, 2)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setToolTip(self.text('details_tip',
            'Complete stored fields, tags, sources and categories for the selected ID; nothing is truncated. '
            'Records exceeding the review limits require separate review.'))
        layout.addWidget(self.details, 2)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.action_status = QLabel()
        self.action_status.setWordWrap(True)
        self.action_status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.action_status)
        controls = QHBoxLayout()
        specs = (
            ('restart_button', 'restart', 'Restart', 'Read current records from the first page.', self.restart),
            ('next_button', 'next', 'Next page', 'Read up to 100 more versions without retaining previous pages.', self.next_page),
            ('archive_button', 'archive', 'Keep offline', 'Hide this copy from operational views; retain all stored data and file bytes.',
             lambda: self.act('archive_copy')),
            ('online_button', 'online', 'Verify and make available',
             'Hash the current file in a worker. Matching content restores this identity; different content starts a new clean version.',
             lambda: self.act('make_copy_online')),
            ('close_button', 'close', 'Close',
             'Cancel verification before admission. A database action already accepted continues after closing.', self.close))
        for attribute, key, default, tip, callback in specs:
            button = QPushButton(self.text(key, default))
            button.setToolTip(self.text(key + '_tip', tip))
            button.clicked.connect(callback)
            setattr(self, attribute, button)
            controls.addWidget(button)
        layout.addLayout(controls)
        if theme is not None:
            self.setStyleSheet(theme.get_stylesheet())
            c = theme.get_color
            for attribute, *_ in specs:
                getattr(self, attribute).setStyleSheet(
                    f"QPushButton {{ background-color: {c('bg_input')}; color: {c('text_primary')}; "
                    f"border: 1px solid {c('border_strong')}; padding: 6px; }} "
                    f"QPushButton:disabled {{ color: {c('text_dim')}; border-color: {c('border')}; }}")
            self.details.setStyleSheet(f"background-color: {c('bg_input')}; color: {c('text_primary')}; "
                                      f"border: 1px solid {c('border')};")
        self.restart()

    def text(self, key, default):
        return self.tr_text('lib.versions.' + key, default)

    def reason(self, error):
        reason = getattr(error, 'reason', str(error))
        return self.text('error.' + reason, reason)

    def _controls(self):
        idle = not self._closed and not self._busy and not self._acting
        self.scope.setEnabled(idle)
        self.table.setEnabled(idle)
        self.restart_button.setEnabled(idle)
        self.next_button.setEnabled(idle and self._has_more)
        reviewed = idle and self._reviewed is not None
        self.archive_button.setEnabled(bool(reviewed and not self._reviewed['state']['file']['is_offline']))
        self.online_button.setEnabled(bool(reviewed))

    def restart(self):
        if self._closed or self._busy or self._acting:
            return
        self._after = None
        self.next_page()

    def next_page(self):
        if self._closed or self._busy or self._acting:
            return
        self._busy = True
        self._has_more = False
        self._copies = []
        self._selected_id = self._reviewed = None
        self.details_reader.invalidate()
        self.table.setRowCount(0)
        self.details.clear()
        self._controls()
        self.status.setText(self.text('loading', 'Reading copy versions…'))
        self.reader.request('get_copy_versions_page', after_id=self._after, limit=100,
                            offline_only=self.scope.currentData())

    def _received(self, ticket, value, error):
        if self._closed:
            return
        self._busy = False
        if error is not None:
            self.status.setText(self.text('failed', 'Could not read copy versions: {reason}').format(reason=self.reason(error)))
            self._controls()
            return
        self._copies = value['copies']
        self._after = value['next_after_id']
        self._has_more = len(self._copies) == 100
        self.table.setRowCount(len(self._copies))
        for row, record in enumerate(self._copies):
            values = (str(record['id']), record['path'],
                self.text('offline' if record['is_offline'] else 'active', 'Offline' if record['is_offline'] else 'Active'),
                record['hash_mod'] or record['hash_original'] or '', str(record['rating']))
            for column, item in enumerate(values):
                cell = QTableWidgetItem(item)
                if column in (1, 3):
                    cell.setToolTip(item)
                self.table.setItem(row, column, cell)
        self.status.setText(self.text('page', '{count} versions on this page.').format(count=len(self._copies)))
        if self._copies:
            self.table.selectRow(0)
        self._controls()

    def _select(self):
        if self._closed or self._acting:
            return
        self._reviewed = None
        self._selected_id = None
        self.details.clear()
        self.details_reader.invalidate()
        row = self.table.currentRow()
        if not self._busy and 0 <= row < len(self._copies):
            self._selected_id = self._copies[row]['id']
            self.details.setPlainText(self.text('reading_state', 'Reading complete stored state…'))
            self.details_reader.request('get_copy_version_state', self._selected_id)
        self._controls()

    def _details_received(self, ticket, value, error):
        if self._closed or self._acting or self._selected_id is None:
            return
        self._reviewed = None
        if error is not None:
            self.details.setPlainText(self.text('failed', 'Could not read copy versions: {reason}').format(reason=self.reason(error)))
        elif value['state']['file']['id'] == self._selected_id:
            self._reviewed = value
            self.details.setPlainText(json.dumps(value['state'], ensure_ascii=False, indent=2, sort_keys=True))
        self._controls()

    def act(self, operation):
        button = self.archive_button if operation == 'archive_copy' else self.online_button
        if self._closed or self._acting or not button.isEnabled() or self._reviewed is None:
            return
        self._acting = True
        self.details_reader.invalidate()
        self._controls()
        self.action_status.setText(self.text('working', 'Verifying and updating copy {id}…').format(id=self._selected_id))
        self.action_reader.request(operation, copy_id=self._selected_id,
                                   expected_fingerprint=self._reviewed['fingerprint'])

    def _acted(self, ticket, value, error):
        if self._closed or not self._acting:
            return
        self._acting = False
        confirmed = error is None and isinstance(value, dict) and value.get('applied') is True
        self._reviewed = None  # Approval is consumed, even after an error.
        self._controls()
        if not confirmed:
            self.action_status.setText(self.text('action_failed',
                'Copy {id} was not confirmed: {reason}. Read its current state before retrying.').format(
                    id=self._selected_id, reason=self.reason(error) if error is not None else self.text('unconfirmed', 'Unconfirmed commit')))
            self._select()
        else:
            self.action_status.setText(self.text('done', 'Copy {id} updated. Catalog restarted.').format(id=value['copy_id']))
            self.copies_changed.emit()
            self.restart()

    def _shutdown(self, *_):
        self._closed = True
        for reader in (self.reader, self.details_reader, self.action_reader):
            reader.close()

    def closeEvent(self, event):
        self._shutdown()
        super().closeEvent(event)
