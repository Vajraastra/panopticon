"""Confirmación por alcance capturado; el worker termina antes de abrir el modal."""
import weakref
from PySide6.QtCore import QObject, Slot, Qt
from PySide6.QtWidgets import QMessageBox
from ..logic.folder_archive import FolderArchiveWorker

_LIVE_WORKERS = set()


def retain_worker(worker):
    _LIVE_WORKERS.add(worker)
    worker.finished.connect(lambda worker=worker: _LIVE_WORKERS.discard(worker))


class FolderArchiveController(QObject):
    def __init__(self, module):
        super().__init__(module)
        self.module = weakref.ref(module)
        self.worker = None
        self.result = None
        self.token = 0
        self.closed = False

    def start(self, path):
        module = self.module()
        if self.closed or module is None or not module._can_write_folder():
            return
        self.token += 1
        module._folder_write = (self.token, 'remove_watched_folder', path)
        module.btn_add.setEnabled(False)
        module.btn_remove.setEnabled(False)
        module._auto_scan_after_write = module._auto_scan_timer.isActive()
        module._auto_scan_timer.stop()
        module._update_export_controls()
        module.lbl_folder_write.setText(module.tr('lib.folder.archive_preview',
            'Checking indexed copies under: {path}').format(path=path))
        self._start_worker('preview', path)

    def _start_worker(self, operation, path, fingerprint=None):
        module = self.module()
        if self.closed or module is None or (self.worker and self.worker.isRunning()):
            return
        try:
            worker = FolderArchiveWorker(module.db.service, operation=operation, folder=path,
                expected_fingerprint=fingerprint, token=self.token)
            self.worker, self.result = worker, None
            retain_worker(worker)
            worker.finished_signal.connect(self._received)
            worker.finished.connect(self._finished)
            worker.start()
        except Exception as exc:
            if self.worker is not None and not self.worker.isRunning():
                _LIVE_WORKERS.discard(self.worker)
                self.worker.deleteLater()
                self.worker = None
            module._on_folder_written(self.token, None, exc)

    @Slot(dict)
    def _received(self, result):
        if self.sender() is self.worker and result['token'] == self.token:
            self.result = result

    def _valid(self, path):
        module = self.module()
        return (not self.closed and module is not None and not module._closed and
            module._folder_write == (self.token, 'remove_watched_folder', path) and
            module._scan_request is None and not any(worker and worker.isRunning() for worker in
                (module.indexer_thread, module.backfill_thread, module._reindex_thread)))

    @Slot()
    def _finished(self):
        worker = self.sender()
        if worker is not self.worker:
            return
        result, self.worker, self.result = self.result, None, None
        worker.deleteLater()
        module = self.module()
        if self.closed or module is None or module._closed:
            return
        path = worker.folder
        if result is None or result['error']:
            module._on_folder_written(self.token, None,
                (result or {}).get('error') or 'folder_archive_result_missing')
            return
        if result['cancelled']:
            self._cancel(path)
            return
        if result['operation'] == 'archive':
            value = result['value']
            module._on_folder_written(self.token, value['committed'], None)
            if value['committed']:
                module.lbl_folder_write.setText(module.tr('lib.folder.archived',
                    '{count} copies confirmed offline. Stopped watching: {path}').format(
                        count=value['archived'], path=path))
            return
        report = result['value']['preview']
        # Nada se escribe hasta revisar conteos. La selección durante el modal invalida la acción.
        if not self._valid(path) or not self._selected(path) or not self.confirm(report):
            self._cancel(path)
            return
        if not self._valid(path) or not self._selected(path):
            self._cancel(path)
            return
        module.lbl_folder_write.setText(module.tr('lib.folder.archiving',
            'Archiving indexed copies under: {path}').format(path=path))
        self._start_worker('archive', path, report['fingerprint'])

    def _selected(self, path):
        module = self.module()
        selected = module.folder_list.currentItem()
        return selected is not None and selected.text() == path

    def _cancel(self, path):
        module = self.module()
        if module is None or module._closed:
            return
        module._on_folder_written(self.token, None, 'folder_archive_cancelled')

    def confirm(self, report):
        module = self.module()
        box = QMessageBox(module.view)
        box.setWindowTitle(module.tr('lib.folder.archive_title', 'Review folder archival'))
        box.setTextFormat(Qt.PlainText)
        text = module.tr('lib.folder.archive_confirm',
            'Stop watching this folder and mark its indexed copies offline?\n{path}\n\n'
            '{active} active copies will become offline.\n{offline} copies already offline.\n'
            '{total} identities in the reviewed scope.\n\n'
            'Includes subfolders. Preserves files on disk, IDs, curation, generation metadata '
            'and Cherry download receipts. Offline copies are excluded from normal views. '
            'Reactivating them requires explicit review by copy ID.').format(**report, path=report['folder'])
        if report['nested_watched']:
            text += '\n\n' + module.tr('lib.folder.archive_nested',
                'These nested folders stay watched, but their indexed copies are included '
                'in this archival:\n{paths}').format(paths='\n'.join(report['nested_watched']))
        box.setText(text)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        for button, key, label, tip in (
                (QMessageBox.Yes, 'archive_yes', 'Archive and stop watching',
                 'Confirm this scope. Membership is checked again before the database commit.'),
                (QMessageBox.No, 'archive_no', 'Cancel',
                 'Close without changing watched folders or copy availability.')):
            box.button(button).setText(module.tr('lib.folder.' + key, label))
            box.button(button).setToolTip(module.tr('lib.folder.' + key + '_tip', tip))
        try:
            return box.exec() == QMessageBox.Yes
        finally:
            try: box.deleteLater()
            except RuntimeError: pass

    def close(self):
        self.closed = True
        if self.worker and self.worker.isRunning():
            self.worker.stop()

    def shutdown_ready(self):
        return self.worker is None or not self.worker.isRunning()
