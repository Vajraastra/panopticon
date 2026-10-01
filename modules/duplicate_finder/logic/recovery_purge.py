"""Vista previa y purga definitiva manual de una copia exacta recuperada.

Los handles fijan los bytes durante CAS y borrado; no existe transacción global
FS/SQLite. El diario independiente y todas las filas/recibos sobreviven al borrado.
"""
import hashlib
import os
from pathlib import Path
import threading
import uuid
from core.curation_apply import _check_cancel
from core.duplicate_policy import SHA
from core.duplicate_purge import decision
from core.duplicate_snapshot import encode
from .file_guards import plain_chain, documented
from .pinned_file import PinnedFile
from .recovery_review import RecoveryReview, read_journal
from .recovery_storage import write_journal


def _scope(path, scope):
    plain_chain(path)
    actual = path.resolve(strict=True)
    for name in scope:
        watched = Path(name)
        if not watched.is_absolute():
            raise ValueError('duplicate_retirement_scope_requires_review')
        watched = watched.resolve()
        if actual == watched or watched in actual.parents:
            raise ValueError('duplicate_recovery_inside_library')
    for parent in path.parents:
        if os.path.lexists(parent/'catalog.db') or os.path.lexists(parent/'panopticon.curation.db'):
            raise ValueError('duplicate_purge_portable_project_requires_review')


def _guards(evidence):
    source, target, keeper = (Path(evidence[key]) for key in ('source', 'destination', 'keeper'))
    for path in (source, target, keeper):
        plain_chain(path)
        if documented(path):
            raise ValueError('external_documentation_requires_review')
    # Una nueva copia operativa en la ruta antigua invalida la acción física.
    if os.path.lexists(source) or not source.parent.is_dir():
        raise ValueError('duplicate_purge_original_path_changed')
    _scope(target, evidence['scope'])


class PurgePreview:
    def __init__(self, service, *, journal, copy_id, journal_fingerprint, expected_fingerprint):
        if (type(copy_id) is not int or not -2**63 <= copy_id <= 2**63-1 or
                not isinstance(journal_fingerprint, str) or not SHA.fullmatch(journal_fingerprint) or
                not isinstance(expected_fingerprint, str) or not SHA.fullmatch(expected_fingerprint)):
            raise ValueError('invalid_duplicate_purge_request')
        self.service = service
        self.arguments = dict(journal=journal, copy_id=copy_id,
            journal_fingerprint=journal_fingerprint, expected_fingerprint=expected_fingerprint)

    def run(self, event=None):
        event = threading.Event() if event is None else event
        result = dict(complete=False, cancelled=False, error=None, evidence=None, preview_fingerprint=None)
        try:
            _check_cancel(event)
            args = self.arguments
            review = RecoveryReview(self.service, args['journal']).run(event)
            if review['cancelled']:
                raise RuntimeError('curation_cancelled')
            if not review['complete']:
                raise ValueError(review['error'] or 'duplicate_purge_review_incomplete')
            if review['journal_fingerprint'] != args['journal_fingerprint']:
                raise ValueError('duplicate_recovery_journal_changed')
            moves = [m for m in review['moves'] if m['copy_id'] == args['copy_id']]
            if len(moves) != 1 or moves[0]['observed'] != 'retired_verified':
                raise ValueError('duplicate_purge_requires_review')
            move = moves[0]
            selected = next(c for c in review['copies'] if c['copy_id'] == args['copy_id'])
            if selected['fingerprint'] != args['expected_fingerprint']:
                raise ValueError('copy_version_changed_since_review')
            path, journal, fingerprint, _ = read_journal(args['journal'], event)
            if fingerprint != args['journal_fingerprint'] or len(journal['keep']) != 1:
                raise ValueError('duplicate_purge_requires_review')
            keeper = next(c for c in review['copies'] if c['source'] == journal['keep'][0])
            ids = [args['copy_id'], keeper['copy_id']]
            native = self.service.read('get_duplicate_recovery_state', ids, cancel_event=event)
            if (not isinstance(native, list) or len(native) != 2 or [c.get('copy_id') for c in native] != ids or
                    any(n['state'] is None or n['fingerprint'] != c['fingerprint']
                        for n, c in zip(native, (selected, keeper)))):
                raise ValueError('duplicate_purge_identity_changed')
            decision(native, journal['digest'], journal['policy'])
            scope = self.service.read('get_duplicate_retirement_roots', cancel_event=event)
            evidence = dict(journal=path.as_posix(), journal_fingerprint=fingerprint,
                copy_id=args['copy_id'], source=move['source'], destination=move['destination'],
                keeper_id=keeper['copy_id'], keeper=keeper['source'], digest=journal['digest'],
                identity=move['destination_state']['identity'], keeper_identity=keeper['physical']['identity'],
                size=move['destination_state']['identity'][2], scope=scope, policy=journal['policy'],
                approved=[dict(copy_id=n['copy_id'], path=n['state']['file']['path'], fingerprint=n['fingerprint']) for n in native],
                before=native)
            _guards(evidence)
            with PinnedFile(evidence['keeper']) as kept, PinnedFile(evidence['destination'], delete=True) as victim:
                kept.verify(evidence['digest'], evidence['keeper_identity'], event)
                victim.verify(evidence['digest'], evidence['identity'], event)
                if tuple(evidence['identity'][:2]) == tuple(evidence['keeper_identity'][:2]):
                    raise ValueError('duplicate_archive_ambiguous_path')
                _guards(evidence)
            _, _, last, _ = read_journal(args['journal'], event)
            if last != fingerprint:
                raise ValueError('duplicate_recovery_journal_changed')
            _check_cancel(event)
            if len(encode(evidence)) > 2*1024*1024:
                raise ValueError('duplicate_recovery_report_limit')
            result.update(complete=True, evidence=evidence, preview_fingerprint=hashlib.sha256(encode(evidence)).hexdigest())
        except Exception as exc:
            result.update(cancelled=event.is_set(), error=None if event.is_set() else
                'duplicate_purge_io_failed' if isinstance(exc, OSError) else str(getattr(exc, 'reason', exc))[:256])
        return result


class RecoveryPurge:
    def __init__(self, service, *, preview_fingerprint, **arguments):
        if not isinstance(preview_fingerprint, str) or not SHA.fullmatch(preview_fingerprint):
            raise ValueError('invalid_duplicate_purge_request')
        self.preview = PurgePreview(service, **arguments)
        self.preview_fingerprint = preview_fingerprint
        self.service = service

    def run(self, event=None):
        event = threading.Event() if event is None else event
        result = dict(copy_id=self.preview.arguments['copy_id'], destination=None, keeper=None,
            operation_journal=None, approved=False, delete_marked=False, deleted=False,
            complete=False, cancelled=False, error=None)
        operation = None
        admitted = False
        try:
            preview = self.preview.run(event)
            if preview['cancelled']:
                raise RuntimeError('curation_cancelled')
            if not preview['complete']:
                raise ValueError(preview['error'] or 'duplicate_purge_review_incomplete')
            if preview['preview_fingerprint'] != self.preview_fingerprint:
                raise ValueError('duplicate_purge_preview_changed')
            evidence = preview['evidence']
            result.update(destination=evidence['destination'], keeper=evidence['keeper'])
            # Mantener ambos handles abiertos hasta confirmar el cierre de la víctima.
            with PinnedFile(evidence['keeper']) as kept, PinnedFile(evidence['destination'], delete=True) as victim:
                kept.verify(evidence['digest'], evidence['keeper_identity'], event)
                victim.verify(evidence['digest'], evidence['identity'], event)
                _guards(evidence)
                target = Path(evidence['journal']).parent/('purge-'+uuid.uuid4().hex+'.json')
                if os.path.lexists(target):
                    raise ValueError('duplicate_recovery_destination_occupied')
                result['operation_journal'] = target.as_posix()
                operation = dict(format='panopticon.duplicate-purge', version=1, phase='prepared',
                    evidence=evidence, approved=False, delete_marked=False, deleted=False, error=None)
                write_journal(target, operation)
                _check_cancel(event)
                future = self.service.submit('approve_duplicate_purge', evidence['approved'],
                    digest=evidence['digest'], policy=evidence['policy'], expected_scope=evidence['scope'])
                admitted = True
                if future.result() is not True:
                    raise ValueError('duplicate_purge_approval_unconfirmed')
                result['approved'] = operation['approved'] = True
                operation['phase'] = 'approved'
                write_journal(target, operation)
                # Cancelación admitida no corta la verificación/diario a mitad.
                kept.verify(evidence['digest'], evidence['keeper_identity'])
                victim.verify(evidence['digest'], evidence['identity'])
                _guards(evidence)
                _, _, current, _ = read_journal(evidence['journal'])
                if current != evidence['journal_fingerprint']:
                    raise ValueError('duplicate_recovery_journal_changed')
                _check_cancel(event)
                victim.mark_delete()
                result['delete_marked'] = operation['delete_marked'] = True
                victim.close()
                # Share exclusivo + cierre: sólo contar el borrado confirmado por ausencia.
                if os.path.lexists(evidence['destination']) or not Path(evidence['destination']).parent.is_dir():
                    raise ValueError('duplicate_purge_deletion_unconfirmed')
                result['deleted'] = operation['deleted'] = True
                kept.verify(evidence['digest'], evidence['keeper_identity'])
                operation['phase'] = 'complete'
                write_journal(target, operation)
                result['complete'] = True
        except Exception as exc:
            cancelled = event.is_set() and (not admitted or
                (str(exc) == 'curation_cancelled' and not result['delete_marked']))
            reason = None if cancelled else 'duplicate_purge_io_failed' if isinstance(exc, OSError) else str(getattr(exc, 'reason', exc))[:256]
            result.update(cancelled=cancelled, error=reason)
            if operation is not None:
                operation.update(phase='partial' if admitted else 'rejected', error=reason)
                try:
                    write_journal(Path(result['operation_journal']), operation)
                except (OSError, ValueError):
                    pass
        return result
