"""Coalesce revision notifications until the consumer is idle; no GUI SQL."""
from PySide6.QtCore import QObject, QTimer, Signal


class LibraryRevisionWatcher(QObject):
    changed = Signal(object)

    def __init__(self, service, parent=None, *, busy=None, initial=None):
        super().__init__(parent)
        self.service = service
        self.busy = busy or (lambda: False)
        self._observed = service.revision_snapshot() if initial is None else initial
        self._pending = None
        self._closed = False
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll)
        self.destroyed.connect(lambda: self.close())
        self.timer.start()

    def poll(self):
        if self._closed:
            return
        value = self.service.revision_snapshot()
        if value != self._observed:
            self._observed, self._pending = value, value
        if self._pending is not None and not self.busy():
            value, self._pending = self._pending, None
            self.changed.emit(value)

    def close(self):
        self._closed = True
        self._pending = None
        try:
            self.timer.stop()
        except RuntimeError:
            pass
