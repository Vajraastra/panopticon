"""Optional exact coverage hint from a known transaction, published AFTER commit.

Unlike reader population, this hint is a proved count transition. Never publish
uncommitted state, and never read SQL after releasing the write transaction: an
external commit in that interval must retain its different token and force a miss.
"""
import logging
from core.path_identity_clock import SQL, token

log = logging.getLogger(__name__)


def candidate(conn, before, coverage, *, inserted, changed, known_bindings):
    if (not known_bindings or not coverage['prepared'] or coverage['pending']
            or coverage['unresolved'] or before is None):
        return None
    if conn.execute("SELECT 1 FROM sqlite_temp_master WHERE type='trigger' LIMIT 1").fetchone():
        return None
    cursor = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
    try:
        for name, statement in cursor:
            if SQL.get(name) != statement:
                return None
    finally:
        cursor.close()
    after = token(conn)
    if after != (before[0], before[1] + inserted + changed):
        return None
    # All old bindings retain canonical membership; new canonical names each
    # add exactly one ID and one resolved record. Conflict groups cannot change.
    result = dict(coverage)
    result['total'] += inserted
    return after, result


def finish(repo, committed):
    transition = repo._path_ingest_transition
    repo._path_ingest_transition = None
    if committed and transition is not None and repo._path_coverage_cache is not None:
        try:
            repo._path_coverage_cache.put(*transition)
        except Exception:
            # An optional cache failure cannot turn a durable accepted write
            # into a reported failure. Its next read recomputes authoritative SQL.
            log.debug('Discarded optional committed path coverage hint', exc_info=True)
