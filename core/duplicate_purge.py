"""CAS de una purga física; no borra ni modifica identidades o curación nativa."""
from copy import deepcopy
import hashlib
from core.duplicate_archive import roots
from core.duplicate_policy import DuplicatePolicy, SHA, recommend
from core.duplicate_snapshot import encode, recovery_states


def decision(records, digest, policy):
    """La copia offline participa sólo en la comparación de información en memoria."""
    weights = DuplicatePolicy(**policy)
    copies = []
    for record in records:
        value = deepcopy(record['state'])
        if value is None:
            raise ValueError('duplicate_purge_identity_changed')
        value['file']['is_offline'] = 0
        copies.append(dict(path=value['file']['path'], state=value,
            fingerprint=hashlib.sha256(encode(value)).hexdigest()))
    result = recommend(copies, verified_hashes=dict.fromkeys((c['path'] for c in copies), digest), policy=weights)
    if (result['status'] != 'recommended' or result['preferred'] != copies[1]['path'] or
            copies[0]['path'] not in result['remove']):
        raise ValueError('duplicate_purge_information_requires_review')


def approve(conn, approved, *, digest, policy, expected_scope, normalize):
    if not conn.in_transaction:
        raise RuntimeError('duplicate_purge_requires_transaction')
    if (not isinstance(approved, (list, tuple)) or len(approved) != 2 or
            not isinstance(digest, str) or not SHA.fullmatch(digest) or
            not isinstance(policy, dict) or set(policy) != set(DuplicatePolicy.__dataclass_fields__) or
            not isinstance(expected_scope, (list, tuple)) or list(expected_scope) != roots(conn)):
        raise ValueError('invalid_duplicate_purge_approval')
    ids = []
    for record in approved:
        if (not isinstance(record, dict) or set(record) != {'copy_id', 'path', 'fingerprint'} or
                type(record['copy_id']) is not int or not -2**63 <= record['copy_id'] <= 2**63-1 or
                not isinstance(record['path'], str) or not record['path'] or len(record['path']) > 4096 or
                normalize(record['path']) != record['path'] or
                not isinstance(record['fingerprint'], str) or not SHA.fullmatch(record['fingerprint'])):
            raise ValueError('invalid_duplicate_purge_approval')
        ids.append(record['copy_id'])
    if len(set(ids)) != 2:
        raise ValueError('invalid_duplicate_purge_approval')
    current = recovery_states(conn, ids)
    for before, now, offline in zip(approved, current, (1, 0)):
        if (now['state'] is None or now['fingerprint'] != before['fingerprint'] or
                now['state']['file']['path'] != before['path'] or now['state']['file']['is_offline'] != offline or
                now['active_copy_id'] != (None if offline else now['copy_id'])):
            raise ValueError('duplicate_purge_identity_changed')
    decision(current, digest, policy)
    # El owner serializa la aprobación, pero FS/SQLite no comparten transacción.
    # Ningún DELETE/UPDATE, FTS, historial o recibo Cherry forma parte de esta acción.
    return True
