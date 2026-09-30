"""Verificación física de un grupo DB completo; sólo genera una propuesta."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from core.curation_apply import _check_cancel, _identity
from core.duplicate_policy import DuplicatePolicy, recommend
from core.duplicate_snapshot import encode
from .exact_review import EXTENSIONS, _probe
from .file_guards import plain_chain, documented


def review_group(service, digest, *, expected_count=None, policy=None,
                 progress=None, cancel_event=None):
    policy = DuplicatePolicy() if policy is None else policy
    if not isinstance(policy, DuplicatePolicy):
        raise ValueError('invalid_duplicate_review_configuration')
    event = threading.Event() if cancel_event is None else cancel_event
    _check_cancel(event)
    group = service.read('get_duplicate_candidate_copies', digest,
                         expected_count=expected_count, cancel_event=event)
    candidates = group['copies']
    if (group.get('digest') != digest or not 2 <= len(candidates) <= 256 or
            (expected_count is not None and len(candidates) != expected_count) or
            any(set(record) != {'copy_id', 'path'} or type(record['copy_id']) is not int or
                not isinstance(record['path'], str) or not record['path'] or len(record['path'])>4096
                for record in candidates) or
            len({record['copy_id'] for record in candidates}) != len(candidates) or
            len({record['path'] for record in candidates}) != len(candidates)):
        raise ValueError('duplicate_candidate_group_changed')
    paths = [record['path'] for record in candidates]
    proofs, issues, issue_count = {}, [], 0
    def problem(path, reason):
        nonlocal issue_count
        issue_count += 1
        if len(issues) < 20:
            issues.append(dict(path=path, reason=str(reason)))
    def probe(path):
        _check_cancel(event)
        try:
            if not Path(path).is_absolute() or not path.lower().endswith(EXTENSIONS):
                raise ValueError('unsupported_duplicate_candidate_path')
            plain_chain(Path(path))
            return _probe(path, event)
        except (OSError, ValueError) as error:
            return None, str(error)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for index, (path, (proof, error)) in enumerate(zip(paths, pool.map(probe, paths)), 1):
            _check_cancel(event)
            if proof is None:
                problem(path, error)
            elif proof['digest'] != digest:
                problem(path, 'catalog_identity_mismatch')
            else:
                proofs[path] = proof
            if progress:
                progress(index, len(paths), path)
    _check_cancel(event)
    records = service.read('get_duplicate_snapshot', paths, cancel_event=event)
    if len(records) != len(paths) or any(record.get('path') != path for record, path in zip(records, paths)):
        raise ValueError('duplicate_candidate_group_changed')
    sidecars, verified, identities = {}, {}, set()
    ambiguous = False
    for candidate, record in zip(candidates, records):
        _check_cancel(event)
        path = candidate['path']
        state = record.get('state')
        if state is None or state['file']['id'] != candidate['copy_id']:
            problem(path, 'duplicate_candidate_group_changed')
        proof = proofs.get(path)
        if proof is None:
            continue
        try:
            plain_chain(Path(path))
            if _identity(Path(path).stat()) != tuple(proof['identity']):
                raise ValueError('copy_version_file_changed')
            record['external_documentation'] = documented(Path(path), sidecars)
        except (OSError, ValueError) as error:
            problem(path, error)
            continue
        verified[path] = digest
        identity = tuple(proof['identity'][:2])
        ambiguous |= identity in identities
        identities.add(identity)
    report = recommend(records, verified_hashes=verified, policy=policy)
    if ambiguous:
        report = dict(status='review', reason='ambiguous_path_identity', preferred=None,
                      keep=tuple(paths), remove=())
    result = dict(groups={digest:paths}, reports={digest:dict(report,copies=records)},
                  proofs=proofs, issues=issues, issue_count=issue_count, complete=issue_count==0,
                  scope='database')
    if len(encode(result))>4*1024*1024:
        raise ValueError('duplicate_review_payload_limit')
    _check_cancel(event)
    return result
