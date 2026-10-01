"""Acciones explícitas separadas: preparar evidencia o ejecutar la purga manual."""
import threading
from PySide6.QtCore import QThread, Signal
from .recovery_purge import PurgePreview, RecoveryPurge


class RecoveryPurgeWorker(QThread):
    finished_signal = Signal(dict)

    def __init__(self, service, *, operation, token=1, **arguments):
        super().__init__()
        if operation not in ('preview', 'purge') or type(token) is not int or not 1 <= token <= 2**63-1:
            raise ValueError('invalid_duplicate_purge_request')
        self.task = (PurgePreview if operation == 'preview' else RecoveryPurge)(service, **arguments)
        self.operation = operation
        self.token = token
        self._cancel = threading.Event()

    def run(self):
        result = self.task.run(self._cancel)
        result.update(token=self.token, operation=self.operation)
        self.finished_signal.emit(result)

    def stop(self):
        self._cancel.set()
