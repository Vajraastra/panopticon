import threading
import copy
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from .file_tasks import MetadataTasks


class MetadataWorker(QThread):
    finished_signal = Signal(int, str, object)

    def __init__(self, service, journal_root, operation, token, **arguments):
        super().__init__()
        self.tasks = MetadataTasks(service, journal_root)
        self.operation, self.token, self.arguments = operation, token, copy.deepcopy(arguments)
        self.cancel_event = threading.Event()

    def stop(self):
        self.cancel_event.set()

    def run(self):
        try:
            if self.cancel_event.is_set(): raise ValueError('metadata_cancelled')
            result = getattr(self.tasks, self.operation)(**self.arguments, event=self.cancel_event)
            if self.operation == 'load':
                pixels, width, height = result.pop('pixels')
                result['image'] = QImage(pixels, width, height, QImage.Format_RGBA8888).copy()
            result.setdefault('status', 'complete')
        except Exception as exc:
            result = dict(status='error', error=str(exc)[:1000])
        self.finished_signal.emit(self.token, self.operation, result)
