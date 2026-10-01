"""Restauración explícita por ID; bytes recuperables antes del commit nativo.

Sin overwrite/copy+unlink ni compensación automática. Tras admitir un movimiento
o Future, terminar verificación/commit/diario incluso ante cancelación/cierre Qt.
"""
import os
from pathlib import Path
import stat
import threading
import uuid
from core.curation_apply import _check_cancel,_identity
from core.duplicate_policy import SHA
from .exact_review import _probe
from .file_guards import plain_chain,documented
from .recovery_review import RecoveryReview,read_journal
from .recovery_storage import write_journal


def _verify(path,digest,identity):
    plain_chain(path)
    proof,error=_probe(path.as_posix(),None)
    if (error is not None or proof['digest']!=digest or tuple(proof['identity'])!=tuple(identity) or documented(path)):
        raise ValueError('duplicate_restore_bytes_changed')
    return proof


class RecoveryRestore:
    def __init__(self,service,*,journal,copy_id,journal_fingerprint,expected_fingerprint):
        if (type(copy_id) is not int or not -2**63<=copy_id<=2**63-1 or
                not isinstance(journal_fingerprint,str) or not SHA.fullmatch(journal_fingerprint) or
                not isinstance(expected_fingerprint,str) or not SHA.fullmatch(expected_fingerprint)):
            raise ValueError('invalid_duplicate_restore_request')
        self.service=service
        self.journal=journal
        self.copy_id=copy_id
        self.journal_fingerprint=journal_fingerprint
        self.expected_fingerprint=expected_fingerprint

    def run(self,event=None):
        event=threading.Event() if event is None else event
        result=dict(copy_id=self.copy_id,source=None,destination=None,operation_journal=None,
            moved=False,committed=False,complete=False,cancelled=False,error=None)
        operation=None
        admitted=False
        try:
            _check_cancel(event)
            review=RecoveryReview(self.service,self.journal).run(event)
            if review['cancelled']:raise RuntimeError('curation_cancelled')
            if not review['complete']:raise ValueError(review['error'] or 'duplicate_restore_review_incomplete')
            if review['journal_fingerprint']!=self.journal_fingerprint:
                raise ValueError('duplicate_recovery_journal_changed')
            moves=[move for move in review['moves'] if move['copy_id']==self.copy_id]
            if len(moves)!=1 or moves[0]['observed'] not in ('retired_verified','archived_source_present'):
                raise ValueError('duplicate_restore_requires_review')
            move=moves[0]
            copy=next(copy for copy in review['copies'] if copy['copy_id']==self.copy_id)
            if copy['fingerprint']!=self.expected_fingerprint:
                raise ValueError('copy_version_changed_since_review')
            path,journal,fingerprint,_=read_journal(self.journal,event)
            if fingerprint!=self.journal_fingerprint:raise ValueError('duplicate_recovery_journal_changed')
            source,destination=Path(move['source']),Path(move['destination'])
            result.update(source=source.as_posix(),destination=destination.as_posix())
            digest=journal['digest']
            physically_moved=move['observed']=='retired_verified'
            evidence=move['destination_state'] if physically_moved else copy['physical']
            incoming=destination if physically_moved else source
            plain_chain(source);plain_chain(destination);plain_chain(path.parent)
            try:parent=source.parent.stat()
            except OSError as exc:raise ValueError('duplicate_restore_parent_unavailable') from exc
            if not stat.S_ISDIR(parent.st_mode):raise ValueError('duplicate_restore_parent_unavailable')
            if physically_moved and parent.st_dev!=evidence['identity'][0]:
                raise ValueError('duplicate_recovery_requires_same_volume')
            if documented(source) or documented(destination):raise ValueError('external_documentation_requires_review')
            _verify(incoming,digest,evidence['identity'])
            native=self.service.read('get_duplicate_recovery_state',[self.copy_id],cancel_event=event)
            if (len(native)!=1 or native[0]['fingerprint']!=self.expected_fingerprint or
                    native[0]['state'] is None or native[0]['state']['file']['is_offline']!=1 or native[0]['active_copy_id'] is not None):
                raise ValueError('duplicate_restore_identity_changed')
            _check_cancel(event)
            target=path.parent/('restore-'+uuid.uuid4().hex+'.json')
            if os.path.lexists(target):raise ValueError('duplicate_recovery_destination_occupied')
            result['operation_journal']=target.as_posix()
            operation=dict(format='panopticon.duplicate-restore',version=1,phase='prepared',
                retirement_journal=path.as_posix(),retirement_fingerprint=fingerprint,
                copy_id=self.copy_id,source=source.as_posix(),destination=destination.as_posix(),digest=digest,
                identity=evidence['identity'],before=native[0],moved=False,committed=False,error=None)
            write_journal(target,operation)
            _check_cancel(event)
            # Revalidar inmediatamente antes del efecto; Windows rename no sobrescribe.
            plain_chain(source);plain_chain(destination);plain_chain(path.parent)
            _verify(incoming,digest,evidence['identity'])
            if physically_moved:
                if os.path.lexists(source):raise ValueError('duplicate_restore_path_occupied')
                os.rename(destination,source)
                admitted=True
                result['moved']=operation['moved']=True
                operation['phase']='moved'
                write_journal(target,operation)
            # Después del rename no cancelar ni revertir bytes: el owner hará CAS del ID.
            _verify(source,digest,evidence['identity'])
            if not physically_moved:_check_cancel(event)
            future=self.service.submit('restore_duplicate_copy',self.copy_id,expected_fingerprint=self.expected_fingerprint,
                path=source.as_posix(),digest=digest)
            admitted=True
            if future.result() is not True:raise ValueError('duplicate_restore_commit_unconfirmed')
            result['committed']=operation['committed']=True
            operation['phase']='complete'
            write_journal(target,operation)
            result['complete']=True
        except Exception as exc:
            cancelled=event.is_set() and not admitted
            reason=None if cancelled else 'duplicate_restore_io_failed' if isinstance(exc,OSError) else str(getattr(exc,'reason',exc))[:256]
            result.update(cancelled=cancelled,error=reason)
            if operation is not None:
                operation.update(phase='partial' if admitted else 'rejected',error=reason)
                try:write_journal(Path(result['operation_journal']),operation)
                except (OSError,ValueError):pass
        return result
