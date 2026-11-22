"""SQL-only bounded source-name pages; no curation or disk probes.

One PK pass includes every current/offline file and its prepared canonical
spelling. This reserves present identities, not every name in old receipts.
The Native owner/revision fence is required around a multi-page legacy read.
"""
import threading

from core.path_identity import local_path
from core.path_identity_registry import prepared
from core.path_identity_clock import token

PAGE_SIZE = 256
MAX_ID = 2**63 - 1
SNAPSHOT_BUDGET_SECONDS = 20


def aliases(values):
    if type(values) not in (list, tuple) or not 1 <= len(values) <= 32:
        raise ValueError('invalid_source_names_roots')
    return sorted({local_path(item).rstrip('/').casefold() for item in values})


def inside(path, roots):
    key = local_path(path).casefold()
    return any(key.startswith(root + '/') for root in roots)


def page(conn, roots, *, after_id=0, snapshot=None):
    roots = aliases(roots)
    if type(after_id) is not int or not 0 <= after_id <= MAX_ID:
        raise ValueError('invalid_source_names_cursor')
    ready = prepared(conn)
    clock = token(conn)
    clock = list(clock) if clock is not None else None
    if snapshot is None:
        if after_id != 0:
            raise ValueError('source_names_snapshot_required')
        snapshot = dict(max_id=conn.execute('SELECT COALESCE(MAX(id),0) FROM files').fetchone()[0],
                        clock=clock, registry=ready, roots=roots)
    else:
        if (type(snapshot) is not dict or set(snapshot) != {'max_id','clock','registry','roots'} or
                type(snapshot['max_id']) is not int or not 0 <= snapshot['max_id'] <= MAX_ID or
                type(snapshot['registry']) is not bool or snapshot['roots'] != roots):
            raise ValueError('invalid_source_names_snapshot')
        if snapshot['clock'] != clock or snapshot['registry'] != ready:
            raise ValueError('source_names_snapshot_changed')
    maximum = snapshot['max_id']
    if after_id > maximum:
        raise ValueError('invalid_source_names_cursor')
    # LIMIT applies to scanned IDs, not matches. A sparse scope must advance
    # through a disjoint page instead of repeating a whole-library UDF scan.
    canonical = ',r.canonical' if ready else ',NULL'
    join = ' LEFT JOIN path_identity_records r ON r.file_id=f.id' if ready else ''
    rows = conn.execute('SELECT f.id,f.path' + canonical + ' FROM files f' + join +
                        ' WHERE f.id>? AND f.id<=? ORDER BY f.id LIMIT 256',
                        (after_id, maximum)).fetchall()
    records = []
    for copy_id, stored, resolved in rows:
        names = sorted({local_path(name) for name in (stored,resolved) if name and inside(name,roots)})
        if names:
            records.append(dict(id=copy_id, paths=names))
    next_after = rows[-1][0] if rows else maximum
    return dict(snapshot=snapshot, records=records, after_id=next_after,
                done=len(rows) < PAGE_SIZE or next_after == maximum, scanned=len(rows))


def repository_method(self, roots, **kwargs):
    return page(self.conn, roots, **kwargs)


class SourceNamesReader:
    """Page view of one worker-owned read-only transaction."""

    def __init__(self, conn, *, check, error):
        self._conn = conn
        self._check = check
        self._error = error
        self._thread = threading.get_ident()

    def get_source_names_page(self, roots, *, after_id=0, snapshot=None):
        if self._conn is None:
            raise self._error('get_source_names_page', 'source_names_reader_closed')
        if threading.get_ident() != self._thread:
            raise self._error('get_source_names_page', 'source_names_reader_wrong_thread')
        self._check()
        result = page(self._conn, roots, after_id=after_id, snapshot=snapshot)
        self._check()
        return result

    def close(self):
        self._conn = None
