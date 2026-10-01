"""Mantener QThread/owner hasta terminar una restauración admitida."""
import threading
from PySide6.QtCore import QThread,Signal
from .recovery_restore import RecoveryRestore


class RecoveryRestoreWorker(QThread):
    finished_signal=Signal(dict)

    def __init__(self,service,*,token=1,**arguments):
        super().__init__()
        if type(token) is not int or not 1<=token<=2**63-1:raise ValueError('invalid_duplicate_review_token')
        self.task=RecoveryRestore(service,**arguments)
        self.token=token
        self._cancel=threading.Event()

    def run(self):
        result=self.task.run(self._cancel)
        result['token']=self.token
        self.finished_signal.emit(result)

    def stop(self):self._cancel.set()
