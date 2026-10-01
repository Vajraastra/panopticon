"""Defer auxiliary dialog destruction until its native readers finish."""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog

from core.library_async import LibraryReader


class ReaderDialog(QDialog):
    """Closing cancels pre-admission work; admitted actions continue in readers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._close_poll_pending = False
        self._close_result = None

    def _shutdown(self, *_):
        self._closed = True
        self.setEnabled(False)
        for reader in self.findChildren(LibraryReader):
            reader.close()

    def shutdown_ready(self):
        return all(reader.shutdown_ready() for reader in self.findChildren(LibraryReader))

    def _retry(self, callback):
        if self._close_poll_pending:
            return
        self._close_poll_pending = True

        def retry():
            self._close_poll_pending = False
            callback()

        QTimer.singleShot(25, self, retry)

    def closeEvent(self, event):
        self._shutdown()
        if not self.shutdown_ready():
            event.ignore()
            self._retry(self.close)
            return
        super().closeEvent(event)

    def done(self, result):
        # accept/reject/Escape do not pass through closeEvent.
        if self._close_result is None:
            self._close_result = result
        self._shutdown()
        if not self.shutdown_ready():
            self._retry(lambda: self.done(self._close_result))
            return
        super().done(self._close_result)
