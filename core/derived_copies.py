"""Alta Native de derivados; el productor verifica bytes y conserva su diario.

Sólo SQL del dueño. Un recibo confirma una operación histórica, nunca disponibilidad
actual ni atomicidad entre publicación en disco y registro. No heredar curación.
"""
from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import PurePosixPath
import sqlite3
import time
from uuid import UUID

from core.duplicate_snapshot import encode
from core.generation_history import COLUMNS, FIELDS, SHA, path_keys, path_status
from core.library_versions import _id, _reviewed

NAME = 'panopticon_derived_copies_v1'
MAX_REQUEST = 256 * 1024
BUDGET = 10
PRODUCERS = frozenset(('image_optimizer', 'image_cropper', 'image_converter'))
DDL = f'''CREATE TABLE {NAME} (
    operation_id TEXT PRIMARY KEY NOT NULL,
    copy_id INTEGER NOT NULL UNIQUE REFERENCES files(id),
    source_copy_id INTEGER NOT NULL REFERENCES files(id),
    source_fingerprint TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    result_sha256 TEXT NOT NULL,
    source_path TEXT NOT NULL,
    result_path TEXT NOT NULL,
    producer TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)'''
RECEIPT_FIELDS = ('operation_id', 'copy_id', 'source_copy_id', 'source_fingerprint',
                  'source_sha256', 'result_sha256', 'source_path', 'result_path',
                  'producer', 'request_fingerprint', 'recorded_at')


def status(conn):
    row = conn.execute('SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?', (NAME,)).fetchone()
    canonical = lambda value: ' '.join(value.split()).strip().rstrip(';')
    if row is not None and (row[:2] != ('table', NAME) or not isinstance(row[2], str) or
                            canonical(row[2]) != canonical(DDL)):
        raise ValueError('derived_schema_name_conflict')
    return dict(prepared=row is not None)


@contextmanager
def _budget(conn):
    if not conn.in_transaction:
        raise RuntimeError('derived_write_requires_transaction')
    deadline = time.monotonic() + BUDGET
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    def check():
        if time.monotonic() >= deadline:
            raise ValueError('derived_write_budget')
    try:
        yield check
        if time.monotonic() >= deadline:
            raise ValueError('derived_write_budget')
    except sqlite3.OperationalError as exc:
        if time.monotonic() >= deadline:
            raise ValueError('derived_write_budget') from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)


def prepare(conn):
    """DDL aditivo explícito; no crear tablas desde lectores ni altas."""
    with _budget(conn):
        if not status(conn)['prepared']:
            conn.execute(DDL)
        status(conn)
    return True


def _operation(value):
    try:
        if not isinstance(value, str) or len(value) != 36:
            raise ValueError
        parsed = UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('invalid_derived_operation_id') from exc
    return value


def receipt(conn, operation_id):
    _operation(operation_id)
    if not status(conn)['prepared']:
        return dict(prepared=False, receipt=None)
    # No materializar TEXT arbitrariamente grande si un escritor externo lo alteró.
    expressions = [name if name in ('copy_id', 'source_copy_id') else f'substr({name},1,4097)'
                   for name in RECEIPT_FIELDS]
    row = conn.execute(f'SELECT {",".join(expressions)} FROM {NAME} WHERE operation_id=?',
                       (operation_id,)).fetchone()
    if row is None:
        return dict(prepared=True, receipt=None)
    value = dict(zip(RECEIPT_FIELDS, row))
    _id(value['copy_id'])
    _id(value['source_copy_id'])
    for name in ('source_fingerprint', 'source_sha256', 'result_sha256', 'request_fingerprint'):
        if not isinstance(value[name], str) or not SHA.fullmatch(value[name]):
            raise ValueError('invalid_derived_receipt')
    if (value['operation_id'] != operation_id or value['producer'] not in PRODUCERS or
            any(not isinstance(value[name], str) or not value[name] or len(value[name]) > 4096 or
                '\x00' in value[name] for name in ('source_path', 'result_path', 'recorded_at'))):
        raise ValueError('invalid_derived_receipt')
    return dict(prepared=True, receipt=value)


def _request(copy_id, expected_fingerprint, operation_id, source_path, path,
             destination_paths, producer, source_digest, result_digest, generation,
             observation, *, normalize):
    _id(copy_id)
    _operation(operation_id)
    for value in (expected_fingerprint, source_digest, result_digest):
        if not isinstance(value, str) or not SHA.fullmatch(value):
            raise ValueError('invalid_derived_digest')
    if not isinstance(producer, str) or producer not in PRODUCERS:
        raise ValueError('invalid_derived_producer')
    path_keys([source_path, path], normalize=normalize)
    keys = path_keys(destination_paths, normalize=normalize)
    source_path, path = normalize(source_path), normalize(path)
    if not os.path.isabs(source_path) or not os.path.isabs(path):
        raise ValueError('invalid_derived_absolute_path')
    if path.casefold() not in keys or source_path.casefold() in keys:
        raise ValueError('invalid_derived_destination_paths')
    if not isinstance(generation, dict) or set(generation) != set(FIELDS):
        raise ValueError('invalid_derived_generation')
    for key, value in generation.items():
        if key in ('width', 'height'):
            if type(value) is not int or not 1 <= value <= 32768:
                raise ValueError('invalid_derived_generation')
        elif not isinstance(value, str) or len(value) > 65536 or '\x00' in value:
            raise ValueError('invalid_derived_generation')
    if not isinstance(observation, dict) or set(observation) != {'filename', 'size', 'created'}:
        raise ValueError('invalid_derived_observation')
    name, size, created = (observation[key] for key in ('filename', 'size', 'created'))
    if (not isinstance(name, str) or not name or len(name) > 1024 or '\x00' in name or
            name != PurePosixPath(path).name or type(size) is not int or not 0 <= size <= 2**63-1 or
            type(created) not in (int, float) or not math.isfinite(created) or not 0 <= created <= 253402300799):
        raise ValueError('invalid_derived_observation')
    # Canonicalizar aliases evita que un cambio de orden altere el recibo.
    aliases = tuple(sorted(set(keys)))
    payload = dict(copy_id=copy_id, expected_fingerprint=expected_fingerprint, operation_id=operation_id,
        source_path=source_path, path=path, destination_paths=aliases, producer=producer,
        source_digest=source_digest, result_digest=result_digest, generation=generation, observation=observation)
    raw = encode(payload)
    if len(raw) > MAX_REQUEST:
        raise ValueError('derived_request_limit')
    return source_path, path, aliases, hashlib.sha256(raw).hexdigest()


def _check(conn, copy_id, expected_fingerprint, source_path, source_digest, aliases, destinations=None, check=lambda: None):
    source = _reviewed(conn, copy_id, expected_fingerprint)
    if source['is_offline'] != 0:
        raise ValueError('derived_source_offline')
    if source['path'] != source_path:
        raise ValueError('derived_source_path_changed')
    known = source['hash_mod'] or source['hash_original']
    if known is not None and (not isinstance(known, str) or not SHA.fullmatch(known.lower()) or
                              known.lower() != source_digest):
        raise ValueError('derived_source_hash_changed')
    occupied = (destinations.occupied(aliases, check) if destinations is not None else
                path_status(conn, aliases, normalize=lambda value: value)['occupied'])
    if occupied:
        raise ValueError('derived_destination_occupied')


def approve(conn, copy_id, *, expected_fingerprint, operation_id, source_path,
            path, destination_paths, producer, source_digest, result_digest,
            generation, observation, normalize, destinations=None):
    """Preflight explícito sin DML; no reserva ruta ni revisión entre llamadas."""
    with _budget(conn) as check:
        source_path, path, aliases, fingerprint = _request(copy_id, expected_fingerprint,
            operation_id, source_path, path, destination_paths, producer, source_digest,
            result_digest, generation, observation, normalize=normalize)
        saved = receipt(conn, operation_id)
        if not saved['prepared']:
            raise ValueError('derived_schema_not_prepared')
        if saved['receipt'] is not None:
            raise ValueError('derived_operation_already_committed')
        _check(conn, copy_id, expected_fingerprint, source_path, source_digest, aliases, destinations, check)
    return True


def register(conn, copy_id, *, expected_fingerprint, operation_id, source_path,
             path, destination_paths, producer, source_digest, result_digest,
             generation, observation, normalize, update_fts, destinations=None):
    """Alta + FTS + recibo en una transacción; sin lectura/hashing de disco.

    SHA y observaciones son evidencia del worker autorizado. El origen debe estar
    indexado y online; una ruta sin identidad requiere otra operación explícita.
    True con recibo previo sólo confirma el commit histórico, sin reaplicar estado.
    """
    with _budget(conn) as check:
        source_path, path, aliases, fingerprint = _request(copy_id, expected_fingerprint,
            operation_id, source_path, path, destination_paths, producer, source_digest,
            result_digest, generation, observation, normalize=normalize)
        saved = receipt(conn, operation_id)
        if not saved['prepared']:
            raise ValueError('derived_schema_not_prepared')
        if saved['receipt'] is not None:
            if saved['receipt']['request_fingerprint'] != fingerprint:
                raise ValueError('derived_operation_conflict')
            return True
        _check(conn, copy_id, expected_fingerprint, source_path, source_digest, aliases, destinations, check)
        values = (path, observation['filename'], observation['size'], observation['created'],
                  result_digest, *(generation[name] for name in FIELDS))
        key = conn.execute('INSERT INTO files(path,filename,file_size,created_date,hash_original,'
                           + ','.join(COLUMNS) + ') VALUES(' + ','.join('?' for _ in values) + ')',
                           values).lastrowid
        update_fts(key)
        conn.execute(f'INSERT INTO {NAME} (' + ','.join(RECEIPT_FIELDS[:-1]) + ') VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (operation_id, key, copy_id, expected_fingerprint, source_digest, result_digest,
                      source_path, path, producer, fingerprint))
        if destinations is not None:
            destinations.inserted(path)
    return True
