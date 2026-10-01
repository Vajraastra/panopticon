"""Auditoría de diario fuera de Qt, terminal único y cancelación cooperativa."""
import threading
import time
from PySide6.QtCore import QThread,Signal
from .recovery_review import RecoveryReview


class RecoveryReviewWorker(QThread):
    finished_signal=Signal(dict)
    progress=Signal(int,int,str)

    def __init__(self,service,journal,*,token=1):
        super().__init__()
        if type(token) is not int or not 1<=token<=2**63-1:raise ValueError('invalid_duplicate_review_token')
        self.task=RecoveryReview(service,journal)
        self.token=token
        self._cancel=threading.Event()
        self._last_progress=float('-inf')

    def _progress(self,current,total,path):
        now=time.monotonic()
        if current==total or now-self._last_progress>=.1:
            self._last_progress=now
            self.progress.emit(current,total,path)

    def run(self):
        result=self.task.run(self._cancel,self._progress)
        result['token']=self.token
        self.finished_signal.emit(result)

    def stop(self):self._cancel.set()
