"""Retiro recuperable por grupo; revalida bytes/curación y conserva recibos Cherry.

No hay transacción común entre SQLite y el sistema de archivos. El diario se
persiste antes del commit/movimiento; los errores dejan bytes recuperables y se
reportan como parciales. Sólo rename en el mismo volumen, nunca copy+unlink.
"""
from dataclasses import asdict
import os
from pathlib import Path
import uuid

from core.curation_apply import _check_cancel, _identity
from core.duplicate_policy import DuplicatePolicy, recommend
from core.duplicate_snapshot import encode
from .exact_review import _probe
from .file_guards import plain_chain as _plain_chain, documented as _documented


def _write_journal(job, value):
    temporary = job/('journal-'+uuid.uuid4().hex+'.tmp')
    raw = encode(value)
    with temporary.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, job/'journal.json')


class DuplicateRetirementTasks:
    def __init__(self, service):
        self.service = service

    def read(self, operation, *, review, selected, recovery_folder, cancel_event=None):
        if operation != 'retire_duplicates':
            raise ValueError('invalid_duplicate_retirement_action')
        _check_cancel(cancel_event)
        if os.name != 'nt':
            raise ValueError('duplicate_retirement_requires_windows')
        if (not isinstance(review, dict) or review.get('mode') != 'hash' or not review.get('complete') or
                review.get('cancelled') or review.get('error') or len(encode(review)) > 4*1024*1024 or
                not isinstance(selected, (list, tuple)) or not 1 <= len(selected) <= 2000 or
                any(not isinstance(path, str) for path in selected) or len(set(selected)) != len(selected)):
            raise ValueError('invalid_duplicate_retirement_review')
        weights = DuplicatePolicy(**review['policy'])
        root, recovery = Path(review['folder']), Path(recovery_folder)
        if not root.is_absolute() or not recovery.is_absolute():
            raise ValueError('duplicate_retirement_path_not_absolute')
        _plain_chain(root)
        _plain_chain(recovery)
        root = root.resolve(strict=True)
        recovery = recovery.resolve()
        if recovery == root or root in recovery.parents:
            raise ValueError('duplicate_recovery_inside_collection')
        selected_set, batches = set(selected), []
        for digest, report in review['reports'].items():
            chosen = [path for path in review['groups'][digest] if path in selected_set]
            if not chosen:
                continue
            if (report['status'] != 'recommended' or not set(chosen) <= set(report['remove']) or
                    not 2 <= len(report['copies']) <= 256 or
                    [record['path'] for record in report['copies']] != review['groups'][digest]):
                raise ValueError('duplicate_archive_selection_not_safe')
            batches.append((digest, report, chosen))
        if set(path for _, _, chosen in batches for path in chosen) != selected_set:
            raise ValueError('invalid_duplicate_retirement_selection')
        scope = self.service.read('get_duplicate_retirement_roots', cancel_event=cancel_event)
        for name in scope:
            watched = Path(name)
            if not watched.is_absolute():
                raise ValueError('duplicate_retirement_scope_requires_review')
            watched = watched.resolve()
            if recovery == watched or watched in recovery.parents:
                raise ValueError('duplicate_recovery_inside_library')
        recovery.mkdir(parents=True, exist_ok=True)
        _plain_chain(recovery)
        results = []
        for digest, report, chosen in batches:
            try:
                _check_cancel(cancel_event)
                result = self._retire(root, recovery, digest, report, chosen, review['proofs'], weights, scope, cancel_event)
            except Exception as exc:
                result = dict(digest=digest, archived=False, moved=[], error=getattr(exc, 'reason', str(exc)), journal=None)
            results.append(result)
            if result['error']:
                break  # No seguir una tanda tras un resultado parcial.
        return dict(groups=results, moved=sum(len(result['moved']) for result in results),
                    complete=len(results) == len(batches) and all(result['error'] is None for result in results))

    def _retire(self, root, recovery, digest, report, chosen, proofs, weights, scope, event):
        paths = [record['path'] for record in report['copies']]
        identities = set()
        for name in paths:
            path = Path(name)
            if not path.is_absolute() or path == root or root not in path.parents:
                raise ValueError('duplicate_retirement_source_outside_collection')
            _plain_chain(path)
            if _documented(path):
                raise ValueError('external_documentation_requires_review')
            # Un destino dentro de un proyecto Cherry externo al scope tampoco es seguro.
            if any(os.path.lexists(parent/'catalog.db') and (recovery == parent or parent in recovery.parents)
                   for parent in path.parents):
                raise ValueError('duplicate_recovery_inside_cherry_project')
            proof, error = _probe(name, event)
            before = proofs.get(name)
            if (error is not None or before is None or proof['digest'] != digest or
                    tuple(proof['identity']) != tuple(before['identity']) or proof['digest'] != before['digest']):
                raise ValueError('duplicate_bytes_changed_since_review')
            inode = tuple(proof['identity'][:2])
            if inode in identities:
                raise ValueError('duplicate_archive_ambiguous_path')
            identities.add(inode)
            if proof['identity'][0] != recovery.stat().st_dev:
                raise ValueError('duplicate_recovery_requires_same_volume')
        _check_cancel(event)
        current = self.service.read('get_duplicate_snapshot', paths, cancel_event=event)
        if not isinstance(current, (list, tuple)) or len(current) != len(paths):
            raise ValueError('duplicate_group_changed_since_review')
        approved = []
        for before, now in zip(report['copies'], current):
            if (before.get('state') is None or now.get('state') is None or now['path'] != before['path'] or
                    now['fingerprint'] != before['fingerprint'] or
                    now['state']['file']['id'] != before['state']['file']['id']):
                raise ValueError('duplicate_group_changed_since_review')
            approved.append(dict(path=now['path'], copy_id=now['state']['file']['id'], fingerprint=now['fingerprint']))
        decision = recommend(current, verified_hashes=dict.fromkeys(paths, digest), policy=weights)
        if (decision['status'] != 'recommended' or decision['preferred'] != report['preferred'] or
                not set(chosen) <= set(decision['remove'])):
            raise ValueError('duplicate_archive_selection_not_safe')
        job = recovery/uuid.uuid4().hex
        job.mkdir(exist_ok=False)
        journal = dict(format='panopticon.duplicate-retirement', version=1, phase='prepared',
            digest=digest, folder=root.as_posix(), keep=decision['keep'], approved=approved, scope=scope,
            copies=current, proofs={path: proofs[path] for path in paths}, policy=asdict(weights),
            moves=[dict(source=path, destination=(job/f'{index:06d}{Path(path).suffix}').as_posix(), status='pending')
                   for index, path in enumerate(chosen, 1)], error=None)
        try:
            _write_journal(job, journal)
            _check_cancel(event)
            # Última revalidación de todos los paths; el dueño hará CAS de todo el estado.
            for name in paths:
                _plain_chain(Path(name))
                if _documented(Path(name)) or _identity(Path(name).stat()) != tuple(proofs[name]['identity']):
                    raise ValueError('duplicate_bytes_changed_since_review')
            _check_cancel(event)
            committed = self.service.submit('archive_duplicate_copies', approved, chosen,
                digest=digest, policy=asdict(weights), expected_scope=scope).result()
            if committed is not True:
                raise ValueError('duplicate_retirement_commit_unconfirmed')
        except Exception as exc:
            error = getattr(exc, 'reason', str(exc))
            journal.update(phase='rejected', error=error)
            try:
                _write_journal(job, journal)
            except OSError:
                pass
            return dict(digest=digest, archived=False, moved=[], error=error, journal=(job/'journal.json').as_posix())
        moved, error = [], None
        # Tras aceptación no cancelar a mitad del grupo. Un cierre sólo descarta UI.
        try:
            journal['phase'] = 'archived'
            _write_journal(job, journal)
            keeper = Path(decision['preferred'])
            for move in journal['moves']:
                source, destination = Path(move['source']), Path(move['destination'])
                for path in (keeper, source):
                    _plain_chain(path)
                    if _documented(path) or _identity(path.stat()) != tuple(proofs[path.as_posix()]['identity']):
                        raise ValueError('duplicate_bytes_changed_after_commit')
                _plain_chain(job)
                if os.path.lexists(destination):
                    raise ValueError('duplicate_recovery_destination_occupied')
                journal['phase'] = 'moving'
                _write_journal(job, journal)
                os.rename(source, destination)  # Windows: falla si ya existe, sin sobreescribir.
                moved.append(dict(source=source.as_posix(), destination=destination.as_posix()))
                move['status'] = 'moved'
                # Incluso ante reemplazo externo, conservar destino y diario; nunca borrar.
                proof, failure = _probe(destination.as_posix(), None)
                if failure is not None or proof['digest'] != digest:
                    raise ValueError('duplicate_recovered_bytes_require_review')
                _write_journal(job, journal)
            journal['phase'] = 'complete'
            _write_journal(job, journal)
        except Exception as exc:
            error = getattr(exc, 'reason', str(exc))
            journal.update(phase='partial', error=error)
            try:
                _write_journal(job, journal)
            except OSError:
                pass  # El último diario durable contiene el plan, incluso sin status final.
        return dict(digest=digest, archived=True, moved=moved, error=error, journal=(job/'journal.json').as_posix())
