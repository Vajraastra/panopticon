"""Adaptadores Qt para consultas nativas y verificación física de un grupo."""
from copy import deepcopy
import threading

from PySide6.QtCore import QThread, Signal
from core.curation_apply import _check_cancel
from core.duplicate_policy import SHA
from .database_review import review_group
from .review_worker import ExactReviewWorker, _ProgressService


class DatabaseGroupReviewWorker(ExactReviewWorker):
    def __init__(self, folder, service, digest, *, expected_count=None, policy=None, token=1):
        super().__init__(folder, service, policy=policy, token=token)
        if not isinstance(digest,str) or not SHA.fullmatch(digest):
            raise ValueError('invalid_duplicate_candidate_group')
        self.digest, self.expected_count = digest, expected_count

    def _scan(self):
        return review_group(_ProgressService(self.service,self.phase), self.digest,
            expected_count=self.expected_count, policy=self.policy,
            progress=self._progress, cancel_event=self._cancel)


class NativeDuplicateQueryWorker(QThread):
    finished_signal = Signal(dict)

    def __init__(self, service, operation, *, token=1, **arguments):
        super().__init__()
        if operation not in ('get_duplicate_candidates_page','get_duplicate_search_status','prepare_duplicate_search','get_folder_overlap'):
            raise ValueError('invalid_duplicate_native_operation')
        if type(token) is not int or not 1 <= token <= 2**63-1:
            raise ValueError('invalid_duplicate_review_token')
        allowed=({'after_hash','limit'} if operation=='get_duplicate_candidates_page' else
            {'folders','threshold'} if operation=='get_folder_overlap' else set())
        if not set(arguments)<=allowed:
            raise ValueError('invalid_duplicate_native_arguments')
        self.service, self.operation, self.token = service, operation, token
        self.arguments = deepcopy(arguments)
        self._cancel = threading.Event()

    def run(self):
        value=error=None
        submitted=False
        cancelled=False
        try:
            _check_cancel(self._cancel)
            if self.operation=='prepare_duplicate_search':
                future=self.service.submit(self.operation)
                submitted=True
                value=future.result()
                if value is not True:
                    raise ValueError('duplicate_index_prepare_unconfirmed')
            else:
                read=self.service.read_worker if self.operation=='get_folder_overlap' else self.service.read
                value=read(self.operation,**self.arguments,cancel_event=self._cancel)
        except Exception as exc:
            cancelled=self._cancel.is_set() and not submitted
            if not cancelled:
                error=getattr(exc,'reason',str(exc))
        self.finished_signal.emit(dict(token=self.token,operation=self.operation,value=value,
                                       error=error,cancelled=cancelled))

    def stop(self):
        # Cancelar lecturas/preparación no admitida; conservar commit aceptado.
        self._cancel.set()
