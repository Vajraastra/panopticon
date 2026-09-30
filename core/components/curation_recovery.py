"""Inspección completa y cuarentena elegida explícitamente para una carpeta capturada."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView)
from core.library_async import LibraryReader
from core.curation_recovery_tasks import CurationRecoveryTasks

_ERRORS = {
    'sidecar_export_busy': 'Another export or recovery is active; try again later.',
    'invalid_export_guard': 'The export guard requires manual review; it was preserved.',
    'unverifiable_export_lock': 'Ownership cannot be verified; the resources were preserved for manual review.',
    'unsafe_recovery_resource': 'A resource is a link or is not a regular file; review it manually.',
    'recovery_identity_changed': 'The temporary file identity changed; review it manually.',
    'recovery_changed': 'The resources changed since inspection. Inspect again before choosing quarantine.',
    'invalid_export_folder': 'The selected folder is unavailable.',
    'recovery_cancelled': 'Recovery was cancelled before it started.',
}


class CurationRecoveryDialog(QDialog):
    def __init__(self, folder, translate, theme=None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.folder = str(folder)
        self.tr_text = translate
        self._closed = self._busy = False
        self._fingerprint = None
        self._operation = None
        self.reader = LibraryReader(CurationRecoveryTasks(), self)
        self.reader.result.connect(self._received)
        self.setWindowTitle(translate('lib.recovery.title', 'Recover interrupted export'))
        self.resize(820, 470)
        layout = QVBoxLayout(self)
        self.heading = QLabel(self.folder)
        self.heading.setTextFormat(Qt.PlainText)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        note = QLabel(translate('lib.recovery.note',
            'Inspection only reads resources. Quarantine moves exactly the listed temporary files and lock '
            'into a new recovery folder with a saved plan. Images, Cherry catalogs and the published export stay in place.'))
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([translate('lib.recovery.file', 'Resource'),
                                             translate('lib.recovery.size', 'Bytes')])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setToolTip(translate('lib.recovery.table_tip',
            'Complete list from the export manifest, including its lock and existing temporary journals. No unclaimed files are moved.'))
        layout.addWidget(self.table)
        self.identity = QLabel()
        self.identity.setTextFormat(Qt.PlainText)
        self.identity.setWordWrap(True)
        self.identity.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.identity)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)
        controls = QHBoxLayout()
        self.inspect_button = QPushButton(translate('lib.recovery.inspect', 'Inspect again'))
        self.inspect_button.setToolTip(translate('lib.recovery.inspect_tip',
            'Read current resources again and discard the previous fingerprint; does not move or delete files.'))
        self.inspect_button.clicked.connect(self.inspect)
        self.quarantine_button = QPushButton(translate('lib.recovery.quarantine', 'Quarantine listed resources'))
        self.quarantine_button.setToolTip(translate('lib.recovery.quarantine_tip',
            'Revalidate the shown fingerprint under the export guard and move only the listed resources. '
            'Keeps their bytes and a recovery plan; never promotes a temporary export.'))
        self.quarantine_button.setEnabled(False)
        self.quarantine_button.clicked.connect(self.quarantine)
        self.close_button = QPushButton(translate('lib.recovery.close', 'Close'))
        self.close_button.setToolTip(translate('lib.recovery.close_tip',
            'Close this window. A quarantine already started continues in its worker; closing does not undo it.'))
        self.close_button.clicked.connect(self.close)
        for button in (self.inspect_button, self.quarantine_button, self.close_button):
            controls.addWidget(button)
        layout.addLayout(controls)
        if theme is not None:
            self.setStyleSheet(theme.get_stylesheet())
            c = theme.get_color
            for button in (self.inspect_button, self.quarantine_button, self.close_button):
                button.setStyleSheet(f"background-color: {c('bg_input')}; color: {c('text_primary')}; "
                                    f"border: 1px solid {c('border_strong')}; padding: 6px;")
        self.inspect()

    def _start(self, operation, **kwargs):
        self._busy = True
        self._operation = operation
        self._fingerprint = None
        self.inspect_button.setEnabled(False)
        self.quarantine_button.setEnabled(False)
        self.status.setText(self.tr_text('lib.recovery.working', 'Checking export resources…')
                            if operation == 'inspect_recovery' else
                            self.tr_text('lib.recovery.moving', 'Moving reviewed resources to quarantine…'))
        self.reader.request(operation, folder=self.folder, **kwargs)

    def inspect(self):
        if self._closed or self._busy:
            return
        self.table.setRowCount(0)
        self.identity.clear()
        self._start('inspect_recovery')

    def quarantine(self):
        if self._closed or self._busy or not self.quarantine_button.isEnabled():
            return
        self._start('quarantine_recovery', expected_fingerprint=self._fingerprint)

    def _received(self, ticket, value, error):
        if self._closed or not self._busy:
            return
        operation, self._operation = self._operation, None
        self._busy = False
        self.inspect_button.setEnabled(True)
        if error is not None:
            reason = str(error)
            if reason.startswith('recovery_incomplete:'):
                reason = self.tr_text('lib.recovery.partial',
                    'Quarantine is incomplete. Preserved plan and moved bytes: {folder}. '
                    'Inspect again to review remaining resources.').format(folder=reason.split(':', 1)[1])
            else:
                reason = self.tr_text('lib.recovery.error.' + reason, _ERRORS.get(reason, reason))
            self.status.setText(self.tr_text('lib.recovery.failed', 'Could not complete recovery: {reason}').format(reason=reason))
            return
        if operation == 'quarantine_recovery':
            self.status.setText(self.tr_text('lib.recovery.done',
                'Resources preserved in {folder}. Inspect again before another recovery.').format(folder=value['folder']))
        elif value['status'] == 'clean':
            self.status.setText(self.tr_text('lib.recovery.clean',
                'No interrupted export lock found. Unclaimed resources are not inspected or moved.'))
        else:
            self.table.setRowCount(len(value['files']))
            for row, record in enumerate(value['files']):
                self.table.setItem(row, 0, QTableWidgetItem(record['name']))
                self.table.setItem(row, 1, QTableWidgetItem(str(record['size'])))
            self.identity.setText(self.tr_text('lib.recovery.identity',
                'Export: {token}\nFingerprint: {fingerprint}').format(token=value['token'], fingerprint=value['fingerprint']))
            self._fingerprint = value['fingerprint']
            self.quarantine_button.setEnabled(True)
            self.status.setText(self.tr_text('lib.recovery.ready',
                'Review all {count} listed resources before choosing quarantine.').format(count=len(value['files'])))

    def closeEvent(self, event):
        self._closed = True
        self.reader.close()
        super().closeEvent(event)
