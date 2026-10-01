"""Terminal Qt único y progreso pHash explícito; nunca esperar desde GUI."""
from copy import deepcopy
import threading
import time

from PySide6.QtCore import QThread,Signal
from .perceptual_backfill import PerceptualBackfill


class PerceptualBackfillWorker(QThread):
    finished_signal=Signal(dict)
    progress=Signal(int,int,str)

    def __init__(self,service,folders,*,token=1,pixel_limit=10_000_000,max_images=100_000):
        super().__init__()
        if type(token) is not int or not 1<=token<=2**63-1:raise ValueError('invalid_duplicate_review_token')
        self.task=PerceptualBackfill(service,deepcopy(folders),pixel_limit=pixel_limit,max_images=max_images)
        self.token=token
        self._cancel=threading.Event()
        self._last_progress=float('-inf')

    def _progress(self,current,total,path):
        now=time.monotonic()
        if current==total or now-self._last_progress>=.1:
            self._last_progress=now
            self.progress.emit(current,total,path)

    def run(self):
        result=self.task.run(event=self._cancel,progress=self._progress)
        result['token']=self.token
        self.finished_signal.emit(result)

    def stop(self):
        self._cancel.set()
