"""Core-owned Cherry ingestion independent of any module view."""
from contextlib import closing
import logging
from pathlib import Path
from queue import Queue, Empty, Full
import threading

from PySide6.QtCore import QObject, Signal, Slot, Qt
from core.cherry_ingest_queue import PendingFolders, valid_path
from core.cherry_ingest_scan import reconcile
from core.library_writer_guard import writer_guard

log = logging.getLogger(__name__)


class CherryIngestor(QObject):
    completed = Signal(str, int)
    failed = Signal(str)
    scan_started = Signal(str)
    scan_finished = Signal(str, str, int, int)  # folder, outcome, accepted, conflicts

    def __init__(self, service, event_bus, parent=None, *, autostart=True):
        super().__init__(parent)
        self.service, self.event_bus = service, event_bus
        self.queue_path = Path(service.db_path + '.cherry-ingest.db')
        self._notices = Queue(maxsize=128)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._rescan = threading.Event()
        self._rescan.set()  # Recover the commit-to-notice crash gap on every start.
        self._last_error = None
        self._closed = False
        self._admission = threading.Lock()
        self.completed.connect(self._completed, Qt.QueuedConnection)
        self.failed.connect(self._failed, Qt.QueuedConnection)
        self._thread = threading.Thread(target=self._run, name='PanopticonCherryIngest', daemon=True)
        if autostart:
            self.start()

    def start(self):
        """Allow the GUI to connect before even a fast startup scan runs."""
        with self._admission:
            if self._closed or self._thread.ident is not None:
                return
            self.event_bus.subscribe('cherry_catalog_changed', self.notice)
            try:
                self._thread.start()
            except BaseException:
                self.event_bus.unsubscribe('cherry_catalog_changed', self.notice)
                raise

    def notice(self, folder):
        """Nonblocking GUI admission; overflow requests a complete watched rescan."""
        try:
            folder = valid_path(folder)
        except ValueError:
            log.warning('Cherry ingest rejected an invalid folder notice')
            return False
        with self._admission:
            if self._closed:
                return False
            try:
                self._notices.put_nowait(folder)
            except Full:
                self._rescan.set()
            self._wake.set()
        return True

    def request_shutdown(self):
        with self._admission:
            if not self._closed:
                self._closed = True
                self.event_bus.unsubscribe('cherry_catalog_changed', self.notice)
                self._stop.set()
                self._wake.set()

    def shutdown_ready(self):
        return not self._thread.is_alive()

    def _persist_notices(self, pending):
        # Persist before scanning. A persistence failure stops the worker and is
        # reported; the watched startup rescan also covers unpersisted notices.
        notices = []
        while len(notices) < 128:
            try:
                notices.append(self._notices.get_nowait())
            except Empty:
                break
        if notices:
            pending.mark(notices)
        if self._rescan.is_set():
            self._rescan.clear()
            try:
                pending.mark(self.service.read('get_watched_folders'))
            except BaseException:
                self._rescan.set()
                raise

    def _scan(self, pending, folder, revision):
        accepted = conflicts = 0
        outcome = 'failed'

        def on_batch(batch_accepted, batch_conflicts):
            nonlocal accepted, conflicts
            accepted += batch_accepted
            conflicts += batch_conflicts

        log.info('Cherry catalog scan started: %s', folder)
        self.scan_started.emit(folder)
        try:
            try:
                accepted = reconcile(self.service, folder, self._stop, on_batch=on_batch)
            except Exception as exc:
                pending.failed(folder, revision, exc)
                if self._stop.is_set():
                    outcome = 'cancelled'
                else:
                    self.failed.emit(str(exc)[:512])
            else:
                pending.acknowledge(folder, revision)
                outcome = 'complete'
                self.completed.emit(folder, accepted)
        finally:
            log.info('Cherry catalog scan finished: %s | %s | accepted=%d | conflicts=%d',
                     folder, outcome, accepted, conflicts)
            self.scan_finished.emit(folder, outcome, accepted, conflicts)

    def _run(self):
        from core.library_batch import BatchCancelled, commit_control
        try:
            if commit_control(self.service, 'prepare_ingest_reviews', self._stop) is not True:
                raise RuntimeError('ingest_review_preparation_unconfirmed')
            with writer_guard(self.queue_path), closing(PendingFolders(self.queue_path)) as pending:
                while True:
                    self._wake.clear()
                    self._persist_notices(pending)
                    if self._stop.is_set():
                        break  # Keep pending work durable; do not replay on close.
                    rows = pending.page()
                    if not rows:
                        self._wake.wait(1)
                        continue
                    for folder, revision in rows:
                        if self._stop.is_set():
                            break
                        self._scan(pending, folder, revision)
                        # A new notice during a pass gets a new revision/pass.
                        self._persist_notices(pending)
        except Exception as exc:
            if self._stop.is_set() and isinstance(exc, BatchCancelled):
                return
            log.exception('Cherry ingest stopped; startup reconciliation is still required')
            try:
                self.failed.emit(str(exc)[:512])
            except RuntimeError:
                pass

    @Slot(str, int)
    def _completed(self, folder, count):
        self._last_error = None
        if not self._closed:
            self.event_bus.publish('library_ingested', dict(folder=folder, count=count))

    @Slot(str)
    def _failed(self, error):
        if error != self._last_error:
            self._last_error = error
            log.warning('Cherry ingestion pending: %s', error)
