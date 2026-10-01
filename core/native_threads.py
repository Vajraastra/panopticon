"""Nonblocking QThread join and retention through final OS-thread cleanup.

Qt 6.8: finished/isRunning do not synchronize all native cleanup effects.
https://doc.qt.io/qt-6.8/qthread.html#isRunning
On Windows/Linux/macOS wait(0) succeeds only after the OS thread exits.
"""
from PySide6.QtCore import QObject, QThread, QTimer, QEvent, Qt, Slot
from shiboken6 import delete as delete_qobject


_NATIVE_THREADS = {}


def native_done(worker):
    if worker is None:
        return True
    if isinstance(worker, QThread):
        return worker.wait(0)
    # Compatibility for existing synchronous worker adapters.
    return not worker.isRunning()


class _Retainer(QObject):
    def __init__(self, worker, owner):
        super().__init__()
        self.worker = worker
        self.owner = owner
        self._poll_pending = False
        self._delete_requested = False
        worker.installEventFilter(self)
        worker.finished.connect(self._finished, Qt.QueuedConnection)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.DeferredDelete:
            if not native_done(self.worker):
                # QThread destruction can otherwise join unfinished cleanup on GUI.
                self._delete_requested = True
                self.poll()
                return True
            # Remove the registry entry before Qt invalidates the wrapper.
            self._delete_requested = False
            self.poll()
        return False

    @Slot()
    def _finished(self):
        self.poll()

    def poll(self):
        worker = self.worker
        if _NATIVE_THREADS.get(worker) is not self:
            return True
        if not native_done(worker):
            if not self._poll_pending:
                self._poll_pending = True
                QTimer.singleShot(25, self, self._retry)
            return False
        worker.removeEventFilter(self)
        del _NATIVE_THREADS[worker]
        self.owner = None
        if self._delete_requested:
            # The consumed DeferredDelete cannot be scheduled again with
            # deleteLater (Qt still records it as requested). Complete that
            # existing request on its creator thread, after wait(0) joined.
            delete_qobject(worker)
        self.deleteLater()
        return True

    def _retry(self):
        self._poll_pending = False
        self.poll()


def start_worker(worker, owner):
    """Call on the GUI/creator thread, immediately before starting a QThread."""
    previous = _NATIVE_THREADS.get(worker)
    if previous is not None and not previous.poll():
        raise RuntimeError('native_worker_still_retained')
    if worker.thread() is not QThread.currentThread():
        raise RuntimeError('native_worker_wrong_owner_thread')
    retainer = _Retainer(worker, owner)
    _NATIVE_THREADS[worker] = retainer
    try:
        worker.start()
    except Exception:
        retainer.poll()
        raise


def owner_threads_ready(owner):
    retainers = tuple(retainer for retainer in _NATIVE_THREADS.values()
                      if retainer.owner is owner)
    # Poll every thread, so completed siblings release their references too.
    results = [retainer.poll() for retainer in retainers]
    return all(results)


def native_threads_ready():
    return all([retainer.poll() for retainer in tuple(_NATIVE_THREADS.values())])
