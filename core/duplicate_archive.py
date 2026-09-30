"""Admisión central de descartes exactos revisados. Sólo archiva identidades."""
from core.duplicate_policy import DuplicatePolicy, SHA, recommend
from core.duplicate_snapshot import snapshot


def roots(conn):
    rows = conn.execute('SELECT substr(path,1,4097) FROM watched_folders ORDER BY path LIMIT 257').fetchall()
    if len(rows) > 256 or any(not isinstance(row[0], str) or not row[0] or len(row[0]) > 4096 for row in rows):
        raise ValueError('duplicate_retirement_scope_limit')
    return [row[0] for row in rows]


def archive(conn, approved, selected, *, digest, policy, expected_scope, normalize_path, database_scope=False):
    if not conn.in_transaction:
        raise RuntimeError('duplicate_archive_requires_transaction')
    if (not isinstance(approved, (list, tuple)) or not 2 <= len(approved) <= 256 or
            not isinstance(selected, (list, tuple)) or not 1 <= len(selected) < len(approved) or
            not isinstance(digest, str) or not SHA.fullmatch(digest) or
            not isinstance(policy, dict) or set(policy) != set(DuplicatePolicy.__dataclass_fields__)):
        raise ValueError('invalid_duplicate_archive')
    weights = DuplicatePolicy(**policy)
    if type(database_scope) is not bool:
        raise ValueError('invalid_duplicate_archive_scope')
    if (not isinstance(expected_scope, (list, tuple)) or len(expected_scope) > 256 or
            any(not isinstance(path, str) or not path or len(path)>4096 for path in expected_scope) or
            list(expected_scope) != roots(conn)):
        raise ValueError('duplicate_retirement_scope_changed')
    paths, ids = [], set()
    for record in approved:
        if not isinstance(record, dict) or set(record) != {'path', 'copy_id', 'fingerprint'}:
            raise ValueError('invalid_duplicate_archive')
        path, copy_id, fingerprint = (record[key] for key in ('path', 'copy_id', 'fingerprint'))
        if (not isinstance(path, str) or not path or len(path) > 4096 or normalize_path(path) != path or
                type(copy_id) is not int or not -2**63 <= copy_id <= 2**63-1 or copy_id in ids or
                not isinstance(fingerprint, str) or not SHA.fullmatch(fingerprint)):
            raise ValueError('invalid_duplicate_archive')
        paths.append(path)
        ids.add(copy_id)
    if len(set(path.casefold() for path in paths)) != len(paths):
        raise ValueError('duplicate_archive_ambiguous_path')
    if (any(not isinstance(path, str) for path in selected) or len(set(selected)) != len(selected) or
            not set(selected) <= set(paths)):
        raise ValueError('invalid_duplicate_archive_selection')
    if database_scope:
        from core.duplicate_index import status
        from core.duplicate_candidates import copies as group_copies
        if not status(conn)['prepared']:
            raise ValueError('duplicate_search_not_prepared')
        members = group_copies(conn, digest, expected_count=len(approved))['copies']
        if {(record['copy_id'], record['path']) for record in members} != {
                (record['copy_id'], record['path']) for record in approved}:
            raise ValueError('duplicate_candidate_group_changed')
    # Una sola transacción y consultas por bloque, incluyendo la copia que queda.
    current = snapshot(conn, paths, normalize_path)
    for before, now in zip(approved, current):
        if (now['state'] is None or now['fingerprint'] != before['fingerprint'] or
                now['state']['file']['id'] != before['copy_id']):
            raise ValueError('duplicate_group_changed_since_review')
    result = recommend(current, verified_hashes=dict.fromkeys(paths, digest), policy=weights)
    if result['status'] != 'recommended' or not set(selected) <= set(result['remove']):
        raise ValueError('duplicate_archive_selection_not_safe')
    # Sin borrar filas, FTS, curación, identidad ni historial. No toca DBs de Cherry.
    retiring_ids = [record['copy_id'] for record in approved if record['path'] in selected]
    placeholders = ','.join('?' for _ in retiring_ids)
    cursor = conn.execute(f'UPDATE files SET is_offline=1 WHERE is_offline=0 AND id IN ({placeholders})', retiring_ids)
    if cursor.rowcount != len(retiring_ids):
        raise ValueError('duplicate_group_changed_since_review')
    return True
