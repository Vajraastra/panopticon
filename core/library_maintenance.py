"""SQL-only identity preservation for explicitly reviewed Cherry maintenance.

The worker owns the durable plan, exclusion guard and physical byte checks.
This command never moves files, invents hashes, revives history or copies
curation between IDs. A repeated command needs a fresh review; no receipt or
implicit idempotence is inferred from a reused destination name.
"""
import re
from pathlib import PurePosixPath
from uuid import uuid4

from core.library_results import ResultBudget, collect_rows
from core.library_versions import _id, state
from core.path_identity import local_path
from core.path_identity_registry import _record, prepared, store


SHA = re.compile('[0-9a-f]{64}')
FIELDS = frozenset({'id', 'old_path', 'new_path', 'expected_fingerprint',
                    'size', 'digest', 'identity'})


def _path(value):
    normalized = local_path(value)
    if normalized != value:
        raise ValueError('maintenance_normalized_path_required')
    return normalized


def _keys(conn):
    conn.create_function('maintenance_path_key_v1', 1,
                         lambda value: value.casefold() if isinstance(value, str) else None,
                         deterministic=True)


def has_history(conn, folders):
    """Bounded conservative guard, including offline copies and prepared aliases."""
    if type(folders) not in (list, tuple) or not 1 <= len(folders) <= 256:
        raise ValueError('invalid_maintenance_folders')
    roots = {_path(folder).rstrip('/').casefold() for folder in folders}

    def within(path):
        key = local_path(path).rstrip('/').casefold()
        while key:
            if key in roots:
                return 1
            key = key.rpartition('/')[0]
        return 0

    def watched_overlap(path):
        key = local_path(path).rstrip('/').casefold()
        return int(within(path) or any(root.startswith(key + '/') for root in roots))

    # A watched scope may be indexed concurrently even while it is still empty.
    conn.create_function('maintenance_watch_overlap_v1', 1, watched_overlap)
    if conn.execute('SELECT 1 FROM watched_folders WHERE maintenance_watch_overlap_v1(path) LIMIT 1').fetchone():
        return True
    conn.create_function('maintenance_folder_member_v1', 1, within)
    if conn.execute('SELECT 1 FROM files WHERE maintenance_folder_member_v1(path) LIMIT 1').fetchone():
        return True
    # Stored spellings can differ after a case rename; a conservative refusal
    # needs no full registry migration or content/identity inference.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='path_identity_records'").fetchone():
        if conn.execute('SELECT 1 FROM path_identity_records WHERE canonical IS NOT NULL '
                        'AND maintenance_folder_member_v1(canonical) LIMIT 1').fetchone():
            return True
    return False


def copies(conn, paths):
    """Return all active/history IDs for <=100 names; never a partial result."""
    if type(paths) not in (list, tuple) or len(paths) > 100:
        raise ValueError('invalid_maintenance_paths')
    keys = sorted(set(_path(path).casefold() for path in paths))
    if not keys:
        return dict(copies=[])
    _keys(conn)
    ids = collect_rows(conn.execute(
        'SELECT id FROM files WHERE maintenance_path_key_v1(path) IN ('
        + ','.join('?' for _ in keys) + ') ORDER BY id LIMIT 257', keys),
        lambda row: row[0])
    if len(ids) > 256:
        raise ValueError('maintenance_copy_limit')
    result, budget = [], ResultBudget()
    for copy_id in ids:
        value = state(conn, copy_id)
        budget.add(value)
        result.append(value)
    return dict(copies=result)


def relocate(repo, values):
    """CAS <=100 existing IDs after the worker verified their final locations."""
    conn = repo.conn
    if not conn.in_transaction:
        raise RuntimeError('maintenance_requires_transaction')
    if type(values) not in (list, tuple) or not 1 <= len(values) <= 100:
        raise ValueError('invalid_maintenance_relocation')
    validated, ids, targets, expected_states = [], set(), set(), {}
    budget = ResultBudget()
    for value in values:
        if type(value) is not dict or set(value) != FIELDS:
            raise ValueError('invalid_maintenance_relocation')
        copy_id = _id(value['id'])
        old, new = _path(value['old_path']), _path(value['new_path'])
        size, digest, fingerprint = (value[key] for key in
                                     ('size', 'digest', 'expected_fingerprint'))
        if (copy_id in ids or new.casefold() in targets or old == new or
                type(size) is not int or not 0 <= size <= 2**63 - 1 or
                type(digest) is not str or SHA.fullmatch(digest) is None or
                type(fingerprint) is not str or SHA.fullmatch(fingerprint) is None):
            raise ValueError('invalid_maintenance_relocation')
        identity = value['identity']
        if (type(identity) is not dict or set(identity) != {'canonical', 'physical'} or
                identity['canonical'] != new):
            raise ValueError('invalid_maintenance_identity')
        proof = dict(file_id=copy_id, stored_path=new, is_offline=0, status='resolved',
                     canonical=identity['canonical'], physical=identity['physical'], reason=None)
        _record(proof)
        current = state(conn, copy_id)
        budget.add(current)
        row = current['state']['file']
        if (current['fingerprint'] != fingerprint or row['path'] != old or
                row['is_offline'] != 0 or row['file_size'] != size):
            raise ValueError('maintenance_source_changed')
        known = row['hash_mod'] or row['hash_original']
        if known is not None and (type(known) is not str or known.lower() != digest):
            raise ValueError('maintenance_source_digest_changed')
        ids.add(copy_id)
        targets.add(new.casefold())
        expected = dict(row, path=new, filename=PurePosixPath(new).name,
                        hash_original=row['hash_original'] if row['hash_original'] is not None else digest)
        expected_states[copy_id] = dict(file=expected, tags=current['state']['tags'])
        validated.append((value, proof))

    from core.library_transfers import guard_ingestion, reserved
    if reserved(conn, ids):
        raise ValueError('maintenance_transfer_pending')
    guard_ingestion(conn, [value[key] for value, _ in validated
                          for key in ('old_path', 'new_path')])
    if getattr(repo, '_maintenance_relocation_operation', None) is not None:
        # Only the bound Native ledger can authorize this indexed scope lookup.
        # Its helper validates the live owner and transaction; failure never
        # falls back to assuming that a destination is empty.
        from core.library_maintenance_operations import destination_occupants
        occupied = destination_occupants(repo, sorted(targets))
    else:
        _keys(conn)
        occupied = collect_rows(conn.execute(
            'SELECT id FROM files WHERE is_offline=0 AND maintenance_path_key_v1(path) IN ('
            + ','.join('?' for _ in targets) + ') LIMIT 101', sorted(targets)), lambda row: row[0])
        if prepared(conn):
            canonical_occupants = collect_rows(conn.execute(
                "SELECT file_id FROM path_identity_records WHERE is_offline=0 AND status='resolved' "
                'AND maintenance_path_key_v1(canonical) IN ('
                + ','.join('?' for _ in targets) + ') LIMIT 101', sorted(targets)), lambda row: row[0])
            occupied = list(set(occupied) | set(canonical_occupants))
    if len(occupied) > 100 or any(copy_id not in ids for copy_id in occupied):
        raise ValueError('maintenance_destination_occupied')

    # Free every active source binding inside this transaction before assigning
    # final names, including cycles. Intermediate paths never become visible.
    for value, _ in validated:
        temporary = 'panopticon-maintenance-sql:' + str(uuid4())
        if conn.execute('SELECT 1 FROM files WHERE path=?', (temporary,)).fetchone():
            raise ValueError('maintenance_temporary_collision')
        conn.execute('UPDATE files SET path=? WHERE id=?', (temporary, value['id']))
    for value, _ in validated:
        conn.execute('UPDATE files SET path=?,filename=?,file_size=?,hash_original=coalesce(hash_original,?) '
                     'WHERE id=?', (value['new_path'], PurePosixPath(value['new_path']).name,
                                   value['size'], value['digest'], value['id']))
        repo._fts_sync_file(value['id'])
    if prepared(conn):
        store(conn, [proof for _, proof in validated])
    for copy_id, expected in expected_states.items():
        if state(conn, copy_id)['state'] != expected:
            raise ValueError('maintenance_result_changed')
    return True
