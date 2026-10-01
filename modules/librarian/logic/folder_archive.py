"""Preview y confirmación capturada, separados; nunca cambia archivos o Cherry."""
import os
import threading
from core.curation_apply import _check_cancel
from core.duplicate_policy import SHA
from PySide6.QtCore import QThread, Signal


class FolderArchiveTasks:
    def __init__(self, service):
        self.service = service

    def read(self, operation, *, folder, expected_fingerprint=None, cancel_event=None):
        if operation not in ('preview','archive'):
            raise ValueError('invalid_folder_archive_action')
        if not isinstance(folder,str) or not folder or len(folder)>4096 or not os.path.isabs(folder):
            raise ValueError('invalid_folder_archive_scope')
        folder=os.path.normpath(folder).replace('\\','/')
        _check_cancel(cancel_event)
        value=self.service.read('get_folder_archive_preview',folder,cancel_event=cancel_event)
        if operation=='preview':return dict(preview=value,committed=False,archived=0)
        if not isinstance(expected_fingerprint,str) or not SHA.fullmatch(expected_fingerprint):
            raise ValueError('invalid_folder_archive_fingerprint')
        if value['fingerprint']!=expected_fingerprint:
            raise ValueError('folder_archive_members_changed')
        _check_cancel(cancel_event)
        future=self.service.submit('archive_watched_folder_reviewed',folder,expected_fingerprint=expected_fingerprint)
        # Una vez admitido, esperar confirmación/error; cerrar Qt no cancela commit.
        if future.result() is not True:
            raise ValueError('folder_archive_commit_unconfirmed')
        return dict(preview=value,committed=True,archived=value['active'])


class FolderArchiveWorker(QThread):
    finished_signal=Signal(dict)

    def __init__(self,service,*,operation,folder,expected_fingerprint=None,token=1):
        super().__init__()
        if type(token) is not int or not 1<=token<=2**63-1 or operation not in ('preview','archive'):
            raise ValueError('invalid_folder_archive_action')
        self.task=FolderArchiveTasks(service)
        self.operation,self.folder,self.fingerprint,self.token=operation,folder,expected_fingerprint,token
        self._cancel=threading.Event()

    def run(self):
        value=None;error=None;cancelled=False
        try:
            value=self.task.read(self.operation,folder=self.folder,expected_fingerprint=self.fingerprint,cancel_event=self._cancel)
        except Exception as exc:
            error=str(getattr(exc,'reason',exc))[:256]
            cancelled=self._cancel.is_set() and error in ('curation_cancelled','cancelled')
            if cancelled:error=None
        self.finished_signal.emit(dict(operation=self.operation,folder=self.folder,token=self.token,
            value=value,error=error,cancelled=cancelled))

    def stop(self):
        self._cancel.set()
