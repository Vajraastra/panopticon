"""Owned review writes; admitted saves finish without waiting on the GUI."""
import threading
from PySide6.QtCore import QThread, Signal


class ReviewWorker(QThread):
    result = Signal(object, str)

    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.stop = threading.Event()

    def cancel(self):
        self.stop.set()

    def run(self):
        try:
            value = self.operation(self.stop.is_set)
            self.result.emit(value, '')
        except Exception as exc:
            self.result.emit(None, str(exc))
