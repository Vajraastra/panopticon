"""One owned Pickler operation; cancellation settles admitted publication."""
import logging
import threading
from PySide6.QtCore import QThread, Signal
from . import transfers


log = logging.getLogger(__name__)


class PicklerWorker(QThread):
    finished_signal = Signal(object, object)

    def __init__(self, kind, service, args, *, journal_root=None):
        super().__init__()
        self.kind,self.service,self.args = kind,service,args
        self.journal_root = journal_root
        self._cancel_event = threading.Event()

    def stop(self):
        self._cancel_event.set(); self.requestInterruption()

    def run(self):
        result = error = None
        try:
            if self.kind == 'scan':
                result = transfers.scan_folder(self.args[0],self._cancel_event)
            elif self.kind == 'images':
                result = transfers.load_images(self.args[0],self._cancel_event)
            elif self.kind == 'review':
                result = transfers.list_recovery(self.service,event=self._cancel_event,journal_root=self.journal_root)
            elif self.kind == 'undo':
                result = transfers.undo(self.service,self.args[0],event=self._cancel_event,journal_root=self.journal_root)
            elif self.kind == 'recover':
                result = transfers.recover(self.service,self.args[0],event=self._cancel_event,journal_root=self.journal_root)
            else:
                result = transfers.transfer(self.service,*self.args,event=self._cancel_event,journal_root=self.journal_root)
        except Exception as exc:
            error = str(exc)
            if (self.kind in ('transfer', 'undo') and
                    error in ('path_identity_review_required', 'transfer_source_lookup_busy')):
                source = self.args[0]['current'] if self.kind == 'undo' else self.args[0]
                log.warning('Pickler %s blocked: %s | %s', self.kind, error, source)
        self.finished_signal.emit(result,error)
