"""Bounded, receipted Native transitions for physically verified content drift.

SQL only: the worker holds read handles through commit. Historical identities
and curation survive offline; content coincidences never rebind another ID.
"""
import hashlib
import json
import math
from pathlib import PurePosixPath
import re

from core.derived_copies import _budget
from core.duplicate_snapshot import encode
from core.library_versions import _id, state, make_online
from core.library_transfers import operation, guard_ingestion, reserved
from core.path_identity_registry import prepared as identity_prepared

NAME = 'panopticon_copy_reconciliations_v1'
DDL = f'''CREATE TABLE {NAME} (
    operation_id TEXT PRIMARY KEY NOT NULL,
    request_fingerprint TEXT NOT NULL,
    records_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)'''
SHA = re.compile('[0-9a-f]{64}')
MAX_BATCH = 100
MAX_RECEIPT = 256 * 1024
FIELDS = {'copy_id', 'expected_fingerprint', 'digest', 'observation', 'identity'}
RECORD_FIELDS = {'old_id', 'new_id', 'path', 'digest', 'before_fingerprint',
                 'historical_fingerprint', 'current_fingerprint'}


def status(conn):
    row = conn.execute('SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?', (NAME,)).fetchone()
    canonical = lambda value: ' '.join(value.split()).rstrip(';')
    if row is not None and (row[:2] != ('table', NAME) or type(row[2]) is not str or
                            canonical(row[2]) != canonical(DDL)):
        raise ValueError('copy_reconciliation_schema_requires_review')
    return dict(prepared=row is not None)


def prepare(conn):
    with _budget(conn):
        if not status(conn)['prepared']:
            conn.execute(DDL)
    return True


def request(operation_id, requests):
    operation(operation_id)
    if type(requests) not in (list, tuple) or not 1 <= len(requests) <= MAX_BATCH:
        raise ValueError('invalid_copy_reconciliation_batch')
    values, ids = [], set()
    for item in requests:
        if type(item) is not dict or not FIELDS <= set(item) or set(item) - FIELDS - {'provenance'}:
            raise ValueError('invalid_copy_reconciliation_request')
        key = _id(item['copy_id'])
        if key in ids:
            raise ValueError('duplicate_copy_reconciliation_source')
        ids.add(key)
        for field in ('expected_fingerprint', 'digest'):
            if type(item[field]) is not str or SHA.fullmatch(item[field]) is None:
                raise ValueError('invalid_copy_reconciliation_digest')
        if item['identity'] is not None:
            raise ValueError('copy_reconciliation_identity_requires_review')
        observation = item['observation']
        if type(observation) is not dict or set(observation) != {'filename', 'size', 'created'}:
            raise ValueError('invalid_copy_reconciliation_observation')
        name, size, created = (observation[k] for k in ('filename', 'size', 'created'))
        if (type(name) is not str or not name or len(name) > 1024 or '/' in name or '\\' in name or '\0' in name or
                type(size) is not int or not 0 <= size <= 2**63-1 or
                type(created) not in (int, float) or not math.isfinite(created) or not 0 <= created <= 253402300799):
            raise ValueError('invalid_copy_reconciliation_observation')
        value = dict(item, observation=dict(observation))
        if 'provenance' in item:
            provenance = item['provenance']
            if type(provenance) is not dict or set(provenance) != {'origin_url', 'origin_site', 'artist'}:
                raise ValueError('invalid_copy_reconciliation_provenance')
            for field, limit in (('origin_url', 4096), ('origin_site', 128), ('artist', 1024)):
                text = provenance[field]
                if text is not None and (type(text) is not str or len(text) > limit or '\0' in text):
                    raise ValueError('invalid_copy_reconciliation_provenance')
            value['provenance'] = dict(provenance)
        values.append(value)
    raw = encode(dict(operation_id=operation_id, requests=values))
    if len(raw) > MAX_RECEIPT:
        raise ValueError('copy_reconciliation_payload_too_large')
    return values, hashlib.sha256(raw).hexdigest()


def receipt(conn, operation_id):
    operation(operation_id)
    ready = status(conn)['prepared']
    if not ready:
        return dict(prepared=False, receipt=None)
    row = conn.execute(f'SELECT request_fingerprint,records_json,recorded_at FROM {NAME} '
                       'WHERE operation_id=?', (operation_id,)).fetchone()
    if row is None:
        return dict(prepared=True, receipt=None)
    fingerprint, raw, recorded_at = row
    if (type(fingerprint) is not str or SHA.fullmatch(fingerprint) is None or
            type(raw) is not str or len(raw.encode('utf-8')) > MAX_RECEIPT):
        raise ValueError('copy_reconciliation_receipt_requires_review')
    try:
        records = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError('copy_reconciliation_receipt_requires_review') from exc
    if type(records) is not list or not 1 <= len(records) <= MAX_BATCH:
        raise ValueError('copy_reconciliation_receipt_requires_review')
    ids, new_ids = set(), set()
    from core.path_identity import local_path
    for record in records:
        if type(record) is not dict or set(record) != RECORD_FIELDS:
            raise ValueError('copy_reconciliation_receipt_requires_review')
        old, new = _id(record['old_id']), _id(record['new_id'])
        if old == new or old in ids or new in new_ids or local_path(record['path']) != record['path']:
            raise ValueError('copy_reconciliation_receipt_requires_review')
        ids.add(old); new_ids.add(new)
        for field in ('digest', 'before_fingerprint', 'historical_fingerprint', 'current_fingerprint'):
            if type(record[field]) is not str or SHA.fullmatch(record[field]) is None:
                raise ValueError('copy_reconciliation_receipt_requires_review')
    return dict(prepared=True, receipt=dict(operation_id=operation_id,
        request_fingerprint=fingerprint, phase='complete', records=records, recorded_at=recorded_at))


def _active_aliases(repo):
    """One bounded scan per owner revision, rather than one scan per file.

Our batches keep the active path set unchanged. Other commands or external
commits invalidate this optimization, including Unicode case aliases.
"""
    conn = repo.conn
    stamp = (conn.total_changes, conn.execute('PRAGMA data_version').fetchone()[0])
    if getattr(repo, '_copy_reconciliation_alias_stamp', None) != stamp:
        conn.create_function('reconciliation_path_key_v1', 1, lambda path: path.casefold(), deterministic=True)
        aliases = set()
        used = 0
        for (path,) in conn.execute('SELECT reconciliation_path_key_v1(path) FROM files '
                'WHERE is_offline=0 GROUP BY reconciliation_path_key_v1(path) HAVING count(*)>1'):
            used += len(path.encode('utf-8')) + 128
            if used > 4 * 1024 * 1024:
                raise ValueError('copy_reconciliation_alias_review_limit')
            aliases.add(path)
        repo._copy_reconciliation_aliases = frozenset(aliases)
        repo._copy_reconciliation_alias_stamp = stamp
    return repo._copy_reconciliation_aliases


def reconcile(repo, operation_id, requests):
    repo._copy_reconciliation_alias_pending = False
    with _budget(repo.conn) as check:
        values, fingerprint = request(operation_id, requests)
        previous = receipt(repo.conn, operation_id)
        if not previous['prepared']:
            raise ValueError('copy_reconciliation_schema_not_prepared')
        if previous['receipt'] is not None:
            if previous['receipt']['request_fingerprint'] != fingerprint:
                raise ValueError('copy_reconciliation_operation_conflict')
            return True  # A historical acknowledgement cannot replay curation.
        if identity_prepared(repo.conn):
            raise ValueError('copy_reconciliation_prepared_identity_requires_review')
        from core.derived_paths import DerivedPaths
        if DerivedPaths(repo.conn)._unknown_triggers():
            raise ValueError('copy_reconciliation_triggers_require_review')
        before, paths = {}, set()
        for item in values:
            current = state(repo.conn, item['copy_id'])
            row = current['state']['file']
            known = row['hash_mod'] or row['hash_original']
            if (current['fingerprint'] != item['expected_fingerprint'] or row['is_offline'] or
                    PurePosixPath(row['path']).name != item['observation']['filename'] or
                    type(known) is not str or not SHA.fullmatch(known.lower()) or known.lower() == item['digest']):
                raise ValueError('copy_reconciliation_source_changed')
            if row['path'].casefold() in paths:
                raise ValueError('copy_reconciliation_source_ambiguous')
            paths.add(row['path'].casefold())
            before[item['copy_id']] = current
        if paths.intersection(_active_aliases(repo)):
            raise ValueError('copy_reconciliation_source_ambiguous')
        guard_ingestion(repo.conn, [v['state']['file']['path'] for v in before.values()])
        if reserved(repo.conn, list(before)):
            raise ValueError('copy_reconciliation_transfer_pending')
        records = []
        for item in values:
            key = item['copy_id']
            original = before[key]
            make_online(repo.conn, key, expected_fingerprint=item['expected_fingerprint'],
                        digest=item['digest'], observation=item['observation'], update_fts=repo._fts_sync_file)
            historic = state(repo.conn, key)
            expected = dict(original['state']['file'], is_offline=1)
            if historic['state'] != dict(file=expected, tags=original['state']['tags']):
                raise ValueError('copy_reconciliation_history_changed')
            new_id = repo.get_file_id(expected['path'])
            if new_id is None or new_id == key:
                raise ValueError('copy_reconciliation_new_identity_missing')
            if 'provenance' in item:
                # Explicit evidence from the freshly verified producer catalog;
                # none of this comes from the unrelated historical identity.
                provenance = item['provenance']
                repo.conn.execute('UPDATE files SET origin_url=?,origin_site=?,artist=? WHERE id=?',
                    (provenance['origin_url'], provenance['origin_site'], provenance['artist'], new_id))
                repo._fts_sync_file(new_id)
            current = state(repo.conn, new_id)
            records.append(dict(old_id=key, new_id=new_id, path=expected['path'], digest=item['digest'],
                before_fingerprint=original['fingerprint'], historical_fingerprint=historic['fingerprint'],
                current_fingerprint=current['fingerprint']))
            check()
        raw = encode(records).decode('utf-8')
        if len(raw.encode('utf-8')) > MAX_RECEIPT:
            raise ValueError('copy_reconciliation_receipt_too_large')
        repo.conn.execute(f'INSERT INTO {NAME}(operation_id,request_fingerprint,records_json) VALUES(?,?,?)',
                          (operation_id, fingerprint, raw))
        repo._copy_reconciliation_alias_stamp = (repo.conn.total_changes,
            repo.conn.execute('PRAGMA data_version').fetchone()[0])
        repo._copy_reconciliation_alias_pending = True
    return True


def finish(repo, committed):
    """Account for FTS commit work without swallowing an external commit."""
    if committed and getattr(repo, '_copy_reconciliation_alias_pending', False):
        # Keep the pre-COMMIT data_version: another writer can commit as soon
        # as our transaction releases its lock. Re-reading it would hide that.
        _, version = repo._copy_reconciliation_alias_stamp
        repo._copy_reconciliation_alias_stamp = (repo.conn.total_changes, version)
    elif not committed:
        repo._copy_reconciliation_alias_stamp = None
    repo._copy_reconciliation_alias_pending = False
