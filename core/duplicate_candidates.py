"""Candidatos nativos por hash almacenado; nunca prueba de bytes ni aprobación."""
from core.duplicate_policy import SHA
from core.duplicate_snapshot import encode

CURRENT = "coalesce(nullif(hash_mod,''),hash_original)"
DIGEST = f'lower({CURRENT})'
VALID = f"is_offline=0 AND typeof({CURRENT})='text' AND length({CURRENT})=64 AND {DIGEST} NOT GLOB '*[^0-9a-f]*'"


def page(conn, *, after_hash=None, limit=25):
    if (type(limit) is not int or not 1 <= limit <= 100 or
            (after_hash is not None and (not isinstance(after_hash, str) or not SHA.fullmatch(after_hash)))):
        raise ValueError('invalid_duplicate_candidates_page')
    where, parameters = VALID, []
    if after_hash is not None:
        where += f' AND {DIGEST}>?'
        parameters.append(after_hash)
    rows = conn.execute(f'SELECT {DIGEST} AS digest,count(*) FROM files WHERE {where} '
                        'GROUP BY 1 HAVING count(*)>1 ORDER BY 1 LIMIT ?',
                        (*parameters, limit+1)).fetchall()
    groups = [dict(digest=digest, copies=count, extra_copies=count-1,
                   reviewable=count <= 256) for digest, count in rows[:limit]]
    result = dict(groups=groups, has_more=len(rows)>limit,
                  next_after_hash=groups[-1]['digest'] if groups else after_hash)
    if len(encode(result)) > 32*1024:
        raise ValueError('duplicate_candidates_payload_too_large')
    return result


def copies(conn, digest, *, expected_count=None):
    if (not isinstance(digest, str) or not SHA.fullmatch(digest) or
            (expected_count is not None and (type(expected_count) is not int or expected_count < 2))):
        raise ValueError('invalid_duplicate_candidate_group')
    where = f'{VALID} AND {DIGEST}=?'
    count = conn.execute(f'SELECT count(*) FROM files WHERE {where}', (digest,)).fetchone()[0]
    if expected_count is not None and count != expected_count:
        raise ValueError('duplicate_candidate_group_changed')
    if count < 2:
        raise ValueError('duplicate_candidate_group_missing')
    if count > 256:
        raise ValueError('duplicate_group_requires_separate_review')
    rows = conn.execute(f'SELECT id,substr(path,1,4097) FROM files WHERE {where} ORDER BY id LIMIT 257',
                        (digest,)).fetchall()
    if len(rows) != count:
        raise ValueError('duplicate_candidate_group_changed')
    if any(not isinstance(path, str) or not path or len(path)>4096 for _, path in rows):
        raise ValueError('duplicate_candidates_payload_too_large')
    result = dict(digest=digest, copies=[dict(copy_id=key, path=path) for key, path in rows])
    if len(encode(result)) > 1024*1024:
        raise ValueError('duplicate_candidates_payload_too_large')
    return result
