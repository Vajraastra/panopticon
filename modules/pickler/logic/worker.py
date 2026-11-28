"""One owned Pickler operation; cancellation settles admitted publication."""
import logging
import threading
from time import monotonic
from PySide6.QtCore import QThread, Signal
from . import transfers


log = logging.getLogger(__name__)
_BATCH_LOG_INTERVAL = 10


class PicklerWorker(QThread):
    finished_signal = Signal(object, object)
    progress_signal = Signal(int, int)  # confirmed decisions, total

    def __init__(self, kind, service, args, *, journal_root=None):
        super().__init__()
        self.kind,self.service,self.args = kind,service,args
        self.journal_root = journal_root
        self._cancel_event = threading.Event()

    def stop(self):
        self._cancel_event.set(); self.requestInterruption()

    def _apply(self, decisions):
        completed = []
        result = dict(completed=completed, recovery_required=False, recovery_journal=None, failed_path=None)
        total = len(decisions)
        log.info('Pickler batch started: %d decisions', total)
        next_progress_log = monotonic() + _BATCH_LOG_INTERVAL
        for decision in decisions:
            if self._cancel_event.is_set():
                break
            source = None
            try:
                source, directory, action, reviewed_identity, reviewed_caption = decision
                transfer = transfers.transfer(self.service, source, directory, action, event=self._cancel_event,
                                              journal_root=self.journal_root, reviewed_identity=reviewed_identity,
                                              reviewed_caption=reviewed_caption)
            except Exception as exc:
                result['recovery_required'] = bool(getattr(exc, 'requires_recovery', False))
                result['recovery_journal'] = getattr(exc, 'journal_path', None)
                result['failed_path'] = str(source) if source is not None else None
                if self._cancel_event.is_set() and not result['recovery_required']:
                    break
                log.warning('Pickler batch stopped after %d/%d confirmed decisions: %s',
                            len(completed), total, exc)
                return result, str(exc)
            completed.append(transfer)
            confirmed = len(completed)
            self.progress_signal.emit(confirmed, total)
            now = monotonic()
            if confirmed < total and now >= next_progress_log:
                log.info('Pickler batch progress: %d/%d confirmed decisions', confirmed, total)
                next_progress_log = now + _BATCH_LOG_INTERVAL
        else:
            log.info('Pickler batch completed: %d/%d confirmed decisions', len(completed), total)
            return result, None
        log.info('Pickler batch cancelled: %d/%d confirmed decisions', len(completed), total)
        return result, 'pickler_cancelled'

    def run(self):
        result = error = None
        try:
            if self.kind == 'scan':
                result = transfers.scan_folder(self.args[0],self._cancel_event,archives=True)
            elif self.kind == 'images':
                result = transfers.load_images(self.args[0],self._cancel_event,archives=True)
            elif self.kind == 'review':
                result = transfers.list_recovery(self.service, event=self._cancel_event, journal_root=self.journal_root,
                                                 **({'preferred': self.args[0]} if self.args else {}))
            elif self.kind == 'undo':
                result = transfers.undo(self.service,self.args[0],event=self._cancel_event,journal_root=self.journal_root)
            elif self.kind == 'recover':
                result = transfers.recover(self.service,self.args[0],event=self._cancel_event,journal_root=self.journal_root)
            elif self.kind == 'apply':
                result, error = self._apply(self.args[0])
            else:
                result = transfers.transfer(self.service,*self.args,event=self._cancel_event,journal_root=self.journal_root)
        except Exception as exc:
            error = str(exc)
            if (self.kind in ('transfer', 'undo') and
                    error in ('path_identity_review_required', 'transfer_source_lookup_busy')):
                source = self.args[0]['current'] if self.kind == 'undo' else self.args[0]
                log.warning('Pickler %s blocked: %s | %s', self.kind, error, source)
        self.finished_signal.emit(result,error)
