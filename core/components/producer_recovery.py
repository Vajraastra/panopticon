"""Explicit, single-journal recovery using the application's existing Native owner."""
import threading

from PySide6.QtCore import QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QVBoxLayout)
from shiboken6 import isValid

from core.native_threads import native_done, owner_threads_ready, start_worker
from core.paths import CachePaths
from core.producer_journal_review import review
from core.producer_reconciliation import reconcile


PREFIX = 'tool.producer_recovery.'


def available_action(report):
    """UI eligibility only; reconciliation repeats authoritative checks in its worker."""
    if not report or not report['stable']:
        return None
    if report['receipt_matches_request']:
        return 'acknowledge_historical_receipt'
    if report['kind'] == 'metadata_export' and report['status'] == 'export_bytes_verified':
        return 'record_export_review'
    source = report['source']
    if (report['status'] != 'published_bytes_without_receipt' or
            report['receipt']['receipt'] is not None or not report['receipt']['prepared'] or
            not report['source_revision_matches'] or not source or source.get('missing') or
            source['is_offline'] != 0):
        return None
    if report['kind'] == 'derived':
        return ('register_derived_copy' if report['source_bytes_match'] and
                not report['output_path_occupied'] else None)
    if report['kind'] == 'metadata_edit' and report['backup_matches_source']:
        return 'commit_metadata_edit'
    return None


class ProducerRecoveryWorker(QThread):
    finished_signal = Signal(object)

    def __init__(self, service, path, fingerprint=None):
        super().__init__()
        self.service, self.path, self.fingerprint = service, str(path), fingerprint
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        result = None
        try:
            if self.fingerprint is None:
                value = review(self.service.db_path, self.path, event=self.cancel_event)
            else:
                value = reconcile(self.service, self.path,
                                  expected_review_fingerprint=self.fingerprint, event=self.cancel_event)
            result = dict(value=value, error=None)
        except Exception as error:
            result = dict(value=None, error=str(error)[:1000])
        finally:
            self.finished_signal.emit(result)


class ProducerRecoveryDialog(QDialog):
    def __init__(self, service, tr, parent=None):
        super().__init__(parent)
        self.service, self.tr = service, tr
        self._worker = None
        self._terminal = None
        self._report = None
        self._path = None
        self._closed = False
        self._choosing = False
        self._reap_pending = False
        self._close_poll_pending = False
        self._close_result = None
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(self.t('title', 'Recovery journals'))
        self.resize(820, 610)
        # A bound slot on this receiver is disconnected during its destruction.
        self.destroyed.connect(lambda: self._destroyed())

        layout = QVBoxLayout(self)
        intro = QLabel(self.t('intro', 'Select one producer journal to inspect its current files, '
                              'library identity and historical receipt.'))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.path_label = QLabel(self.t('no_selection', 'No journal selected.'))
        self.path_label.setTextFormat(Qt.PlainText)
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.path_label)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setToolTip(self.t('details_tip', 'Current evidence for this operation; '
                                     'a historical receipt does not restore changed or missing files.'))
        layout.addWidget(self.details, 1)
        self.status_label = QLabel(self.t('idle', 'Select a journal, then review it.'))
        self.status_label.setTextFormat(Qt.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        row = QHBoxLayout()
        self.choose_button = self.button('choose', 'Select journal', 'choose_tip',
            'Select one journal.json from Metadata Hub, Cropper or Image Optimizer recovery.', self.choose)
        self.review_button = self.button('review', 'Review current evidence', 'review_tip',
            'Hash the current files and read Native identity, curation and receipts without changing them.', self.review_current)
        self.action_button = self.button('action', 'Record reviewed action', 'action_tip',
            'Available after review. Rechecks the exact evidence before recording an acknowledgement or completing one pending Native write.', self.apply_review)
        self.close_button = self.button('close', 'Close', 'close_tip',
            'Cancel work before admission; accepted library work finishes before this window closes.', self.reject)
        for button in (self.choose_button, self.review_button, self.action_button, self.close_button):
            row.addWidget(button)
        layout.addLayout(row)
        self._controls()

    def t(self, key, default):
        return self.tr(PREFIX + key, default)

    def button(self, key, default, tip_key, tip, callback):
        button = QPushButton(self.t(key, default))
        button.setToolTip(self.t(tip_key, tip))
        button.clicked.connect(callback)
        return button

    def _alive(self):
        return isValid(self) and not self._closed

    def _controls(self):
        if not self._alive():
            return
        idle = self._worker is None and not self._choosing
        self.choose_button.setEnabled(idle)
        self.review_button.setEnabled(idle and self._path is not None)
        action = available_action(self._report)
        self.action_button.setEnabled(idle and action is not None)
        self.action_button.setText(self.t('action.' + action, 'Record reviewed action') if action
                                   else self.t('action', 'Record reviewed action'))

    def choose(self):
        if not self._alive() or self._worker is not None or self._choosing:
            return
        self._choosing = True
        self._controls()
        try:
            path, _ = QFileDialog.getOpenFileName(self, self.t('choose_title', 'Select producer journal'),
                str(CachePaths.get_cache_root()), self.t('filter', 'Producer journal (journal.json)'))
        finally:
            self._choosing = False
        # A native file chooser runs a nested event loop: shutdown may already have begun.
        if self._alive() and path:
            self.select_journal(path)
        self._controls()

    def select_journal(self, path):
        if not self._alive() or self._worker is not None or self._choosing:
            return False
        self._path, self._report = str(path), None
        self.path_label.setText(self._path)
        self.details.clear()
        self.status_label.setText(self.t('idle', 'Select a journal, then review it.'))
        self._controls()
        return True

    def review_current(self):
        self._start()

    def apply_review(self):
        if available_action(self._report) is not None:
            self._start(self._report['review_fingerprint'])

    def _start(self, fingerprint=None):
        if not self._alive() or self._worker is not None or self._choosing or self._path is None:
            return
        worker = ProducerRecoveryWorker(self.service, self._path, fingerprint)
        self._worker, self._terminal, self._report = worker, None, None
        worker.finished_signal.connect(self._completed, Qt.QueuedConnection)
        worker.finished.connect(self._native_finished, Qt.QueuedConnection)
        self.status_label.setText(self.t('applying', 'Rechecking and recording the reviewed action…')
                                  if fingerprint else self.t('reviewing', 'Reviewing current evidence…'))
        self._controls()
        try:
            start_worker(worker, self)
        except Exception as error:
            self._terminal = dict(value=None, error=str(error)[:1000])
            self._reap()

    @Slot(object)
    def _completed(self, result):
        if self._worker is None or self.sender() is not self._worker:
            return
        if self._terminal is None:
            self._terminal = result
        self._reap()

    @Slot()
    def _native_finished(self):
        if self.sender() is self._worker:
            self._reap()

    def _reap(self):
        worker = self._worker
        if worker is None:
            return
        if not native_done(worker):
            if not self._reap_pending and isValid(self):
                self._reap_pending = True
                QTimer.singleShot(25, self, self._retry_reap)
            return
        owner_threads_ready(self)
        result, self._terminal = self._terminal, None
        self._worker = None
        worker.deleteLater()
        if not self._alive():
            return
        if result is None or result['error'] is not None:
            self.status_label.setText(self.t('failed', 'Review/action requires attention: {reason}').format(
                reason=result['error'] if result else self.t('missing_result', 'No terminal result received.')))
        elif worker.fingerprint is None:
            self._report = result['value']
            self.details.setPlainText(self._describe(self._report))
            self.status_label.setText(self.t('reviewed', 'Review finished: {status}').format(
                status=self._status(self._report['status'])))
        else:
            value = result['value']
            lines = [self.t('record', 'Evidence record: {path}').format(path=value['record'])]
            if value['status'] == 'complete':
                lines.append(self.t('recorded', 'Action recorded. Current state: {status}').format(
                    status=self._status(value['current_status'])))
            else:
                lines.append(self.t('failed', 'Review/action requires attention: {reason}').format(reason=value['error']))
            lines.append(self.t('native_confirmed', 'Historical Native receipt confirmed: {value}').format(
                value=self._yes(value['native_confirmed'])))
            self.details.appendPlainText('\n' + '\n'.join(lines))
            self.status_label.setText(lines[1])
        self._controls()

    def _retry_reap(self):
        self._reap_pending = False
        self._reap()

    def _yes(self, value):
        return self.t('yes', 'Yes') if value else self.t('no', 'No')

    def _status(self, status):
        return self.t('status.' + status, status)

    def _describe(self, report):
        lines = [self.t('operation', 'Operation: {id}').format(id=report['operation_id']),
                 self.t('kind', 'Producer operation: {kind}').format(
                     kind=self.t('kind.' + report['kind'], report['kind'])),
                 self.t('current', 'Current state: {status}').format(status=self._status(report['status'])),
                 self.t('receipt', 'Matching historical receipt: {value}').format(
                     value=self._yes(report['receipt_matches_request']))]
        for role in ('source', 'output', 'backup'):
            file = report['files'].get(role)
            if file is None:
                continue
            lines.extend(['', self.t('role.' + role, role) + ': ' + file['path'],
                          self.t('file_status', 'File: {status}').format(
                              status=self.t('file.' + file['status'], file['status']))])
            if file.get('sha256'):
                lines.append('SHA-256: ' + file['sha256'])
            row = report.get(role)
            if row:
                if row.get('missing'):
                    lines.append(self.t('missing_id', 'Native ID {id} is no longer present.').format(id=row['copy_id']))
                else:
                    lines.append(self.t('identity', 'Native ID {id} · offline: {offline} · rating: {rating} · tags: {tags}').format(
                        id=row['copy_id'], offline=self._yes(row['is_offline']), rating=row['rating'], tags=row['tag_count']))
                    lines.append(self.t('native_path', 'Current Native path: {path}').format(path=row['path']))
                    lines.append(self.t('native_hash', 'Current Native SHA-256: {hash}').format(hash=row['current_sha256']))
        lines.append('')
        for key in ('source_revision_matches', 'source_bytes_match', 'result_bytes_match',
                    'backup_matches_source', 'current_native_matches', 'current_generation_matches',
                    'output_path_occupied'):
            if report[key] is not None:
                lines.append(self.t('check.' + key, key) + ': ' + self._yes(report[key]))
        action = available_action(report)
        lines.extend(['', self.t('effect.' + action, 'Review conflicts or changed evidence before proceeding.')
                      if action else self.t('unavailable', 'Review conflicts or changed evidence before proceeding.')])
        return '\n'.join(lines)

    def _destroyed(self, *_):
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            # The common retainer defers destruction through actual native join.
            self._worker.deleteLater()

    def request_shutdown(self):
        self._closed, self._report = True, None
        if isValid(self):
            self.setEnabled(False)
        if self._worker is not None:
            self._worker.cancel()

    def shutdown_ready(self):
        return owner_threads_ready(self) and native_done(self._worker)

    def _retry_close(self, callback):
        if not self._close_poll_pending:
            self._close_poll_pending = True
            def retry():
                self._close_poll_pending = False
                callback()
            QTimer.singleShot(25, self, retry)

    def closeEvent(self, event):
        self.request_shutdown()
        if not self.shutdown_ready():
            event.ignore()
            self._retry_close(self.close)
            return
        super().closeEvent(event)

    def done(self, result):
        if self._close_result is None:
            self._close_result = result
        self.request_shutdown()
        if not self.shutdown_ready():
            self._retry_close(lambda: self.done(self._close_result))
            return
        super().done(self._close_result)
