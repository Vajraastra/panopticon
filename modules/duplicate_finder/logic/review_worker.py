"""QThread de revisión exacta; terminal único, cancelación y progreso acotado."""
from dataclasses import asdict
import threading
import time

from PySide6.QtCore import QThread, Signal
from core.duplicate_policy import DuplicatePolicy
from .exact_review import ExactDuplicateReview, ReviewLimits


class _ProgressService:
    def __init__(self, service, phase):
        self.service, self.phase = service, phase

    def read(self, *args, **kwargs):
        self.phase.emit('curation')
        return self.service.read(*args, **kwargs)


class ExactReviewWorker(QThread):
    mode = 'hash'
    progress = Signal(int, int, str)
    phase = Signal(str)
    finished_signal = Signal(dict)

    def __init__(self, folder, service, *, policy=None, limits=None, token=1):
        super().__init__()
        if type(token) is not int or not 1 <= token <= 2**63 - 1:
            raise ValueError('invalid_duplicate_review_token')
        policy = DuplicatePolicy() if policy is None else policy
        limits = ReviewLimits() if limits is None else limits
        if not isinstance(policy, DuplicatePolicy) or not isinstance(limits, ReviewLimits):
            raise ValueError('invalid_duplicate_review_configuration')
        self.folder = folder
        self.service = service
        self.policy = DuplicatePolicy(**asdict(policy))
        self.limits = ReviewLimits(**asdict(limits))
        self.token = token
        self._cancel = threading.Event()
        self._last_progress = float('-inf')

    def _progress(self, current, total, path):
        now = time.monotonic()
        if current == total or now - self._last_progress >= .1:
            self._last_progress = now
            self.phase.emit(self.mode)
            self.progress.emit(current, total, path)

    def run(self):
        result = dict(groups={}, reports={}, proofs={}, issues=[], issue_count=0,
                      complete=False, cancelled=False, error=None)
        try:
            self.phase.emit('inventory')
            result.update(self._scan())
        except Exception as error:
            result['cancelled'] = self._cancel.is_set() or str(error) == 'curation_cancelled'
            if not result['cancelled']:
                result['error'] = getattr(error, 'reason', str(error))
        result.update(folder=self.folder, mode=self.mode, token=self.token, policy=asdict(self.policy))
        self.finished_signal.emit(result)

    def _scan(self):
        service = _ProgressService(self.service, self.phase) if self.service is not None else None
        return ExactDuplicateReview(service, self.policy, self.limits).scan(self.folder, self._progress, self._cancel)

    def stop(self):
        # Nunca wait()/terminate() en el hilo de GUI. La admisión/hash comprueban el evento.
        self._cancel.set()


class VisualReviewWorker(ExactReviewWorker):
    mode = 'visual'

    def __init__(self, folder, threshold, *, token=1,service=None):
        super().__init__(folder, service, token=token)
        if type(threshold) is not int or not 1 <= threshold <= 20:
            raise ValueError('invalid_duplicate_visual_threshold')
        self.threshold = threshold

    def _scan(self):
        from .visual_review import scan
        self.phase.emit('visual')
        return scan(self.folder, self.threshold, self._progress, self._cancel,service=self.service)
