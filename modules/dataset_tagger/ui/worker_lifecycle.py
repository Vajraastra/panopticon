"""GUI-owned lifetimes for Tagger workers and their auxiliary windows."""
from functools import partial

from PySide6.QtCore import QObject, QTimer, Qt, Slot
from PySide6.QtWidgets import QDialog


_LIVE_SCOPES = set()


class WorkerScope(QObject):
    """Retain native threads, reject late results and close without GUI waits."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.closed = False
        self.workers = {}
        self._completed = set()

    def connect(self, worker, signal, callback):
        signal.connect(partial(self._deliver, worker, callback), Qt.QueuedConnection)

    def _deliver(self, worker, callback, *args):
        if not self.closed and worker in self.workers:
            callback(*args)

    def start(self, worker, attribute):
        if self.closed:
            raise RuntimeError("Tagger worker admission is closed")
        worker.setParent(self)
        self.workers[worker] = attribute
        _LIVE_SCOPES.add(self)
        worker.finished.connect(self._finished, Qt.QueuedConnection)
        try:
            worker.start()
        except Exception:
            self._completed.add(worker)
            self._reap()
            raise

    @Slot()
    def _finished(self):
        self._completed.add(self.sender())
        self._reap()

    def _reap(self):
        for worker, attribute in tuple(self.workers.items()):
            if not self.closed and worker not in self._completed:
                continue
            if worker.isRunning():
                continue
            del self.workers[worker]
            self._completed.discard(worker)
            if getattr(self.owner, attribute, None) is worker:
                setattr(self.owner, attribute, None)
            worker.deleteLater()
            if not self.closed:
                self.owner._worker_settled(attribute)
        if not self.workers:
            _LIVE_SCOPES.discard(self)
        elif self._completed:
            QTimer.singleShot(25, self._reap)

    def close(self):
        if self.closed:
            return
        self.closed = True
        for worker in tuple(self.workers):
            cancel = getattr(worker, "cancel", None)
            if cancel is not None:
                cancel()
            worker.requestInterruption()

    def ready(self):
        self._reap()
        return not self.workers


class WorkerViewLifecycle:
    """Mixin for widgets/dialogs whose destruction must await native workers."""

    def _init_worker_lifecycle(self):
        self._closing = False
        self._close_poll_pending = False
        self._pending_dialog_result = None
        self._workers = WorkerScope(self)

    def _worker_settled(self, attribute):
        pass

    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        self.setEnabled(False)
        self._workers.close()
        # Close proposal dialogs before their nested exec() resumes a callback.
        for dialog in self.findChildren(QDialog):
            dialog.reject()

    def shutdown_ready(self):
        return self._workers.ready()

    def _retry_close(self, callback):
        if self._close_poll_pending:
            return
        self._close_poll_pending = True

        def retry():
            self._close_poll_pending = False
            callback()

        QTimer.singleShot(25, self, retry)

    def closeEvent(self, event):
        self.shutdown()
        if not self.shutdown_ready():
            event.ignore()
            self._retry_close(self.close)
            return
        super().closeEvent(event)

    def done(self, result):
        # QDialog.accept/reject/Escape bypass closeEvent.
        if self._pending_dialog_result is None:
            self._pending_dialog_result = result
        self.shutdown()
        if not self.shutdown_ready():
            self._retry_close(lambda: self.done(self._pending_dialog_result))
            return
        super().done(self._pending_dialog_result)
