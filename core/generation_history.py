"""CF8: historial por copia y actualización actual mediante el dueño Native.

Sólo SQL: el worker verifica/publica bytes y conserva su diario de recuperación.
Una aprobación no reserva archivos ni garantiza atomicidad entre disco y SQLite.
"""
import hashlib
import math
from pathlib import PurePosixPath
import re
import sqlite3
import time
from uuid import UUID

from core.duplicate_snapshot import encode, snapshot
from core.library_versions import _reviewed, _id

NAME = 'panopticon_generation_history_v1'
INDEX = NAME + '_copy'
DDL = f'''CREATE TABLE {NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    copy_id INTEGER NOT NULL REFERENCES files(id),
    operation_id TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    result_sha256 TEXT NOT NULL,
    previous_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)'''
INDEX_DDL = f'CREATE INDEX {INDEX} ON {NAME}(copy_id,id)'
SHA = re.compile('[0-9a-f]{64}')
FIELDS = ('width', 'height', 'tool', 'model', 'positive', 'negative', 'seed')
COLUMNS = ('width', 'height', 'meta_tool', 'meta_model', 'meta_positive', 'meta_negative', 'meta_seed')
OBSERVED = frozenset(('tool', 'model', 'positive', 'negative', 'seed', 'steps', 'cfg', 'sampler', 'vae', 'loras'))
MAX_RECORD = 256 * 1024
MAX_PAGE = 1024 * 1024


def status(conn):
    present = []
    for name, kind, table, ddl in ((NAME, 'table', NAME, DDL), (INDEX, 'index', NAME, INDEX_DDL)):
        row = conn.execute('SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        canonical = lambda sql: ' '.join(sql.split()).strip().rstrip(';')
        if row is not None and (row[:2] != (kind, table) or not isinstance(row[2], str) or
                                canonical(row[2]) != canonical(ddl)):
            raise ValueError('generation_history_name_conflict')
        present.append(row is not None)
    if present == [False, True]:
        raise ValueError('generation_history_name_conflict')
    return dict(prepared=all(present))


def prepare(conn):
    if not conn.in_transaction:
        raise RuntimeError('metadata_write_requires_transaction')
    deadline = time.monotonic()+30
    timed_out = False
    def interrupt():
        nonlocal timed_out
        timed_out = time.monotonic() >= deadline
        return int(timed_out)
    conn.set_progress_handler(interrupt, 1000)
    try:
        status(conn)  # Rechazar un objeto ajeno antes de cualquier DDL.
        for name, ddl in ((NAME, DDL), (INDEX, INDEX_DDL)):
            if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone() is None:
                conn.execute(ddl)
            if time.monotonic() >= deadline: raise ValueError('generation_history_prepare_budget')
        if not status(conn)['prepared']:
            raise ValueError('generation_history_not_prepared')
        return True
    except sqlite3.OperationalError as exc:
        if timed_out: raise ValueError('generation_history_prepare_budget') from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)


def edit_state(conn, path, *, normalize):
    if not isinstance(path, str) or not path or len(path) > 4096 or '\x00' in path:
        raise ValueError('invalid_metadata_path')
    records = snapshot(conn, [path], normalize)
    record = records[0]
    if record['state'] is None:
        raise ValueError('metadata_copy_not_indexed')
    return record


def path_keys(path, *, normalize):
    paths = [path] if isinstance(path, str) else path
    if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 32 or any(
            not isinstance(value, str) or not value or len(value) > 4096 or '\x00' in value for value in paths):
        raise ValueError('invalid_metadata_path')
    return tuple(normalize(value).casefold() for value in paths)


def path_status(conn, path, *, normalize):
    keys = path_keys(path, normalize=normalize)
    conn.create_function('metadata_casefold_v1', 1, lambda value: value.casefold() if isinstance(value, str) else None)
    row = conn.execute('SELECT 1 FROM files WHERE metadata_casefold_v1(path) IN ('+','.join('?' for _ in keys)+') LIMIT 1',
                       keys).fetchone()
    return dict(occupied=row is not None)


def _operation(operation_id):
    try:
        if not isinstance(operation_id, str) or len(operation_id) != 36:
            raise ValueError
        parsed = UUID(operation_id)
        if parsed.version != 4 or str(parsed) != operation_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('invalid_metadata_operation_id') from exc
    return operation_id


def receipt(conn, operation_id):
    """Bounded historical acknowledgement, independent of current copy state."""
    _operation(operation_id)
    if not status(conn)['prepared']:
        return dict(prepared=False, receipt=None)
    row = conn.execute(f'SELECT id,copy_id,substr(operation_id,1,37),'
        f'substr(request_fingerprint,1,65),substr(source_sha256,1,65),'
        f'substr(result_sha256,1,65),substr(recorded_at,1,65) '
        f'FROM {NAME} WHERE operation_id=?', (operation_id,)).fetchone()
    if row is None:
        return dict(prepared=True, receipt=None)
    key, copy_id, job, fingerprint, source, result, recorded = row
    _id(key)
    _id(copy_id)
    if (job != operation_id or any(not isinstance(value,str) or not SHA.fullmatch(value)
            for value in (fingerprint,source,result)) or not isinstance(recorded,str) or
            not recorded or len(recorded)>64 or '\x00' in recorded):
        raise ValueError('generation_history_invalid_receipt')
    return dict(prepared=True, receipt=dict(id=key, copy_id=copy_id, operation_id=job,
        request_fingerprint=fingerprint, source_sha256=source, result_sha256=result,
        recorded_at=recorded))



def historical_record(conn, operation_id):
    """One bounded history record by its unique operation; no DDL or pagination."""
    value = receipt(conn, operation_id)
    if value['receipt'] is None:
        return dict(prepared=value['prepared'], record=None)
    row = conn.execute(f'SELECT length(cast(previous_json AS blob)), '
                       f'CASE WHEN length(cast(previous_json AS blob))<=? THEN previous_json END '
                       f'FROM {NAME} WHERE operation_id=?', (MAX_RECORD, operation_id)).fetchone()
    if row is None or type(row[0]) is not int or not 0 < row[0] <= MAX_RECORD or not isinstance(row[1],str):
        raise ValueError('generation_history_record_payload_limit')
    import json
    def pairs(values):
        result = {}
        for key, item in values:
            if key in result:
                raise ValueError('generation_history_invalid_previous')
            result[key] = item
        return result
    def nonfinite(_):
        raise ValueError('generation_history_invalid_previous')
    previous = json.loads(row[1], object_pairs_hook=pairs, parse_constant=nonfinite)
    if (not isinstance(previous,dict) or set(previous)!={'native','observed'} or
            not isinstance(previous['native'],dict) or
            set(previous['native'])!=set((*COLUMNS,'hash_original','hash_mod')) or
            not isinstance(previous['observed'],dict) or set(previous['observed'])-OBSERVED or
            any(not isinstance(item,str) or len(item)>65536 or '\x00' in item for item in previous['observed'].values())):
        raise ValueError('generation_history_invalid_previous')
    native = previous['native']
    for column in COLUMNS[:2]:
        if native[column] is not None and (type(native[column]) is not int or not 0 <= native[column] <= 32768):
            raise ValueError('generation_history_invalid_previous')
    for column in (*COLUMNS[2:],'hash_original','hash_mod'):
        item = native[column]
        if item is not None and (not isinstance(item,str) or len(item)>65536 or '\x00' in item):
            raise ValueError('generation_history_invalid_previous')
    for column in ('hash_original','hash_mod'):
        if native[column] is not None and not SHA.fullmatch(native[column].lower()):
            raise ValueError('generation_history_invalid_previous')
    record = dict(**value['receipt'], previous=previous)
    if len(encode(record))>MAX_RECORD+4096:
        raise ValueError('generation_history_record_payload_limit')
    return dict(prepared=True, record=record)


def _request(copy_id, expected_fingerprint, operation_id, source_digest, result_digest,
             generation, observation, source_generation):
    _id(copy_id)
    for value in (expected_fingerprint, source_digest, result_digest):
        if not isinstance(value, str) or not SHA.fullmatch(value):
            raise ValueError('invalid_metadata_digest')
    _operation(operation_id)
    if not isinstance(generation, dict) or set(generation) != set(FIELDS):
        raise ValueError('invalid_metadata_generation')
    for key, value in generation.items():
        if key in ('width', 'height'):
            if type(value) is not int or not 1 <= value <= 32768:
                raise ValueError('invalid_metadata_generation')
        elif not isinstance(value, str) or len(value) > 65536 or '\x00' in value:
            raise ValueError('invalid_metadata_generation')
    if not isinstance(source_generation, dict) or set(source_generation) - OBSERVED:
        raise ValueError('invalid_metadata_source_generation')
    if any(not isinstance(value, str) or len(value) > 65536 or '\x00' in value
           for value in source_generation.values()):
        raise ValueError('invalid_metadata_source_generation')
    if not isinstance(observation, dict) or set(observation) != {'filename', 'size', 'created'}:
        raise ValueError('invalid_metadata_observation')
    name, size, created = (observation[key] for key in ('filename', 'size', 'created'))
    if (not isinstance(name, str) or not name or len(name) > 1024 or '\x00' in name or
            type(size) is not int or not 0 <= size <= 2**63-1 or
            type(created) not in (int, float) or not math.isfinite(created) or not 0 <= created <= 253402300799):
        raise ValueError('invalid_metadata_observation')
    payload = dict(copy_id=copy_id, expected_fingerprint=expected_fingerprint, operation_id=operation_id,
        source_digest=source_digest, result_digest=result_digest, generation=generation,
        observation=observation, source_generation=source_generation)
    raw = encode(payload)
    if len(raw) > MAX_RECORD:
        raise ValueError('metadata_payload_limit')
    return hashlib.sha256(raw).hexdigest()


def _check(conn, copy_id, expected_fingerprint, source_digest, observation):
    row = _reviewed(conn, copy_id, expected_fingerprint)
    if row['is_offline'] != 0:
        raise ValueError('metadata_copy_offline')
    if observation['filename'] != PurePosixPath(row['path']).name:
        raise ValueError('metadata_filename_changed')
    known = row['hash_mod'] or row['hash_original']
    if known is not None and (not isinstance(known, str) or
            not SHA.fullmatch(known.lower()) or known.lower() != source_digest):
        raise ValueError('metadata_source_hash_changed')
    return row


def approve(conn, copy_id, *, expected_fingerprint, operation_id, source_digest,
            result_digest, generation, observation, source_generation):
    _request(copy_id, expected_fingerprint, operation_id, source_digest, result_digest,
             generation, observation, source_generation)
    if not status(conn)['prepared']:
        raise ValueError('generation_history_not_prepared')
    if conn.execute(f'SELECT 1 FROM {NAME} WHERE operation_id=?', (operation_id,)).fetchone():
        raise ValueError('metadata_operation_already_committed')
    _previous(_check(conn, copy_id, expected_fingerprint, source_digest, observation), source_generation)
    return True


def _previous(row, source_generation):
    raw = encode(dict(native={key: row[key] for key in (*COLUMNS, 'hash_original', 'hash_mod')},
                      observed=source_generation))
    if len(raw) > MAX_RECORD:
        raise ValueError('metadata_payload_limit')
    return raw


def commit(conn, copy_id, *, expected_fingerprint, operation_id, source_digest,
           result_digest, generation, observation, source_generation, update_fts):
    if not conn.in_transaction:
        raise RuntimeError('metadata_write_requires_transaction')
    request_fingerprint = _request(copy_id, expected_fingerprint, operation_id, source_digest,
                                  result_digest, generation, observation, source_generation)
    if not status(conn)['prepared']:
        raise ValueError('generation_history_not_prepared')
    receipt = conn.execute(f'SELECT copy_id,request_fingerprint FROM {NAME} WHERE operation_id=?',
                           (operation_id,)).fetchone()
    if receipt is not None:
        if receipt != (copy_id, request_fingerprint):
            raise ValueError('metadata_operation_conflict')
        return True  # Recibo confirmado; nunca reaplicar sobre una versión posterior.
    row = _check(conn, copy_id, expected_fingerprint, source_digest, observation)
    raw = _previous(row, source_generation)
    conn.execute(f'INSERT INTO {NAME}(copy_id,operation_id,request_fingerprint,source_sha256,result_sha256,previous_json) '
        'VALUES(?,?,?,?,?,?)', (copy_id, operation_id, request_fingerprint, source_digest, result_digest, raw.decode('utf-8')))
    conn.execute('UPDATE files SET '+','.join(column+'=?' for column in COLUMNS)+
        ',file_size=?,created_date=?,hash_original=coalesce(hash_original,?),hash_mod=? WHERE id=?',
        (*[generation[key] for key in FIELDS], observation['size'], observation['created'], source_digest, result_digest, copy_id))
    update_fts(copy_id)
    return True


def page(conn, copy_id, *, after_id=0, limit=25):
    _id(copy_id)
    if type(after_id) is not int or not 0 <= after_id <= 2**63-1 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid_generation_history_page')
    if not status(conn)['prepared']:
        return dict(prepared=False, records=[], next_after_id=after_id, has_more=False)
    query = f'SELECT * FROM {NAME} WHERE copy_id=? AND id>? ORDER BY id LIMIT ?'
    params = (copy_id, after_id, limit+1)
    # Medir bytes en SQLite antes de traer JSON grandes al proceso.
    total, maximum = conn.execute('SELECT coalesce(sum(length(cast(previous_json AS blob))),0),'
                                 'coalesce(max(length(cast(previous_json AS blob))),0) FROM ('+query+')', params).fetchone()
    if total > MAX_PAGE-65536 or maximum > MAX_RECORD:
        raise ValueError('generation_history_page_payload_limit')
    rows = conn.execute(f'SELECT id,substr(operation_id,1,37),substr(source_sha256,1,65),'
                        f'substr(result_sha256,1,65),previous_json,substr(recorded_at,1,65) FROM ({query})', params).fetchall()
    if any(len(job) != 36 or not SHA.fullmatch(source) or not SHA.fullmatch(result) or len(recorded) > 64
           for _, job, source, result, _, recorded in rows):
        raise ValueError('generation_history_invalid_record')
    import json
    result = dict(prepared=True, records=[dict(id=key, operation_id=job, source_sha256=source,
        result_sha256=result, previous=json.loads(previous), recorded_at=recorded)
        for key, job, source, result, previous, recorded in rows[:limit]],
        next_after_id=rows[min(len(rows), limit)-1][0] if rows else after_id, has_more=len(rows)>limit)
    if len(encode(result)) > MAX_PAGE:
        raise ValueError('generation_history_page_payload_limit')
    return result
