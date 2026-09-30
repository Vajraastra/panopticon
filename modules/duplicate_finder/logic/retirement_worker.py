"""Adaptador QThread: termina el grupo aceptado antes de cerrar el catálogo."""
from copy import deepcopy
import threading

from PySide6.QtCore import QThread, Signal
from .retirement import DuplicateRetirementTasks


class DuplicateRetirementWorker(QThread):
    finished_signal = Signal(int, object, object)

    def __init__(self, service, *, review, selected, recovery_folder, token):
        super().__init__()
        self.service = service
        self.arguments = deepcopy(dict(review=review, selected=selected,
                                       recovery_folder=recovery_folder))
        self.token = token
        self._cancel = threading.Event()

    def run(self):
        result = error = None
        try:
            result = DuplicateRetirementTasks(self.service).read(
                'retire_duplicates', **self.arguments, cancel_event=self._cancel)
        except Exception as exc:
            error = exc
        self.finished_signal.emit(self.token, result, error)

    def stop(self):
        # No wait/terminate: la tarea termina cualquier grupo cuyo commit aceptó.
        self._cancel.set()
