"""Additive path evidence; never rewrite files.path or merge copy identities."""
import json
from core.library_results import collect_rows, ReadResultError
from core.path_identity import local_path

MARKER = 'path_identity_v1'
TABLE = 'path_identity_records'
STATUSES = frozenset({'resolved', 'offline', 'unsupported', 'unavailable', 'changed'})
TABLE_SQL = ('CREATE TABLE path_identity_records ('
        'file_id INTEGER PRIMARY KEY REFERENCES files(id), '
        'stored_path TEXT NOT NULL COLLATE BINARY, '
        'is_offline INTEGER NOT NULL CHECK(is_offline IN (0,1)), '
        "status TEXT NOT NULL CHECK(status IN ('resolved','offline','unsupported','unavailable','changed')), "
        'canonical TEXT COLLATE BINARY, physical TEXT, reason TEXT, '
        "CHECK((status='resolved' AND canonical IS NOT NULL AND physical IS NOT NULL AND is_offline=0) "
        "OR (status!='resolved' AND canonical IS NULL AND physical IS NULL)))")
INDEX_SQL = ('CREATE INDEX idx_path_identity_canonical ON path_identity_records(canonical,file_id)')


def prepared(conn):
    row = conn.execute('SELECT value FROM panopticon_schema WHERE name=?', (MARKER,)).fetchone()
    if row is None:
        if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (TABLE,)).fetchone():
            raise ValueError('path_identity_unknown_schema')
        return False
    if row != ('1',):
        raise ValueError('path_identity_unknown_schema')
    # Unknown constraints, foreign keys, collation or indexes require review.
    if (conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() != (TABLE_SQL,) or
            conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_path_identity_canonical'").fetchone() != (INDEX_SQL,)):
        raise ValueError('path_identity_unknown_schema')
    return True


def needs_prepare(conn):
    from core.path_identity_clock import token
    ready = prepared(conn)
    return token(conn) is None or not ready


def prepare(conn):
    if not prepared(conn):
        conn.execute(TABLE_SQL)
        conn.execute(INDEX_SQL)
        conn.execute('INSERT INTO panopticon_schema(name,value) VALUES(?,?)', (MARKER, '1'))
    from core.path_identity_clock import prepare as prepare_clock
    prepare_clock(conn)
    return True


def _record(record):
    if not isinstance(record, dict) or set(record) != {
            'file_id', 'stored_path', 'is_offline', 'status', 'canonical', 'physical', 'reason'}:
        raise ValueError('invalid_path_identity_record')
    file_id, path, offline, status = (record[k] for k in ('file_id','stored_path','is_offline','status'))
    if (type(file_id) is not int or not -(2**63) <= file_id < 2**63 or
            not isinstance(path, str) or not path or len(path) > 4096 or '\0' in path or
            type(offline) is not int or offline not in (0, 1) or status not in STATUSES or
            (offline == 1) != (status == 'offline')):
        raise ValueError('invalid_path_identity_record')
    canonical, physical, reason = (record[k] for k in ('canonical','physical','reason'))
    if status == 'resolved':
        if (canonical != local_path(canonical) or not isinstance(physical, (tuple, list)) or
                len(physical) != 7 or any(type(v) is not int or not 0 <= v <= 0xffffffff for v in physical)
                or reason is not None):
            raise ValueError('invalid_path_identity_record')
        physical = json.dumps(physical, separators=(',', ':'))
    elif canonical is not None or physical is not None:
        raise ValueError('invalid_path_identity_record')
    if reason is not None and (not isinstance(reason, str) or len(reason) > 256):
        raise ValueError('invalid_path_identity_record')
    return (file_id, path, offline, status, canonical, physical, reason)


def store(conn, values):
    """Atomic ≤100 row SQL CAS, after worker-side physical revalidation.

    No filesystem calls here. Concurrent curations remain untouched; changed
    path/availability or missing IDs reject the entire batch. No unique key is
    imposed on canonical names: legacy conflicts remain separately reviewable.
    """
    if not prepared(conn):
        raise ValueError('path_identity_not_prepared')
    if not isinstance(values, (tuple, list)) or len(values) > 100:
        raise ValueError('invalid_path_identity_batch')
    rows = [_record(value) for value in values]
    ids = [row[0] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate_path_identity_record')
    for row in rows:
        if conn.execute('SELECT path,is_offline FROM files WHERE id=?', (row[0],)).fetchone() != row[1:3]:
            raise ValueError('path_identity_sql_state_changed')
    for row in rows:
        previous = conn.execute('SELECT file_id,stored_path,is_offline,status,canonical,physical,reason '
                                'FROM path_identity_records WHERE file_id=?', (row[0],)).fetchone()
        if previous != row:
            conn.execute('INSERT INTO path_identity_records VALUES(?,?,?,?,?,?,?) '
                'ON CONFLICT(file_id) DO UPDATE SET stored_path=excluded.stored_path, '
                'is_offline=excluded.is_offline,status=excluded.status,canonical=excluded.canonical, '
                'physical=excluded.physical,reason=excluded.reason', row)
    return True


def status(conn, cache=None):
    if not prepared(conn):
        total = conn.execute('SELECT COUNT(*) FROM files').fetchone()[0]
        return dict(prepared=False, total=total, pending=total, unresolved=0, conflict_groups=0)
    from core.path_identity_clock import token
    key = token(conn)
    # Rolled-back epochs can be reused by a later different transaction. Only
    # committed, coherent RO snapshots may populate shared coverage; never a
    # writer's uncommitted state or independent autocommit SELECT snapshots.
    if not conn.in_transaction or conn.execute('PRAGMA query_only').fetchone() != (1,) or conn.execute('PRAGMA read_uncommitted').fetchone() != (0,):
        cache = None
    cached = cache.get(key) if cache is not None else None
    if cached is not None:
        return cached
    row = conn.execute('SELECT COUNT(*), SUM(CASE WHEN r.file_id IS NULL OR '
        'r.stored_path!=f.path COLLATE BINARY OR r.is_offline!=f.is_offline THEN 1 ELSE 0 END), '
        "SUM(CASE WHEN f.is_offline=0 AND r.status!='resolved' THEN 1 ELSE 0 END) "
        'FROM files f LEFT JOIN path_identity_records r ON r.file_id=f.id').fetchone()
    conflicts = conn.execute('SELECT COUNT(*) FROM (SELECT r.canonical '
        'FROM path_identity_records r JOIN files f ON f.id=r.file_id '
        "WHERE r.status='resolved' AND f.is_offline=0 AND r.is_offline=0 AND f.path=r.stored_path COLLATE BINARY "
        'GROUP BY r.canonical HAVING COUNT(*)>1)').fetchone()[0]
    value = dict(prepared=True, total=row[0], pending=row[1] or 0,
                 unresolved=row[2] or 0, conflict_groups=conflicts)
    if cache is not None:
        cache.put(key, value)
    return value


def matches(conn, canonical, *, limit=100):
    canonical = local_path(canonical)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ReadResultError('invalid_path_identity_page')
    if not prepared(conn):
        raise ReadResultError('path_identity_not_prepared')
    rows = collect_rows(conn.execute('SELECT f.id,f.path,f.rating FROM path_identity_records r '
        'JOIN files f ON f.id=r.file_id WHERE r.canonical=? COLLATE BINARY '
        "AND r.status='resolved' AND r.is_offline=0 AND f.is_offline=0 AND f.path=r.stored_path COLLATE BINARY "
        'ORDER BY f.id LIMIT ?', (canonical, limit + 1)))
    if len(rows) > limit:
        raise ReadResultError('path_identity_matches_too_large')
    return dict(canonical=canonical, records=rows, ambiguous=len(rows)>1)


def resolution(conn, canonical, cache=None):
    coverage = status(conn, cache)
    result = dict(canonical=local_path(canonical), records=[], file_id=None, coverage=coverage)
    if not coverage['prepared']:
        result['status'] = 'not_prepared'
        return result
    result.update(matches(conn, canonical))
    if coverage['pending']:
        result['status'] = 'incomplete'
    elif coverage['unresolved']:
        result['status'] = 'unresolved'
    elif result['ambiguous']:
        result['status'] = 'ambiguous'
    elif not result['records']:
        result['status'] = 'missing'
    else:
        result['status'] = 'unique'
        result['file_id'] = result['records'][0][0]
    return result
