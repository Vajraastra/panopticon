"""Identidad central por copia/versión; sólo SQL del dueño o lector central.

El archivo se verifica en CopyVersionTasks. Ninguna transición modifica bytes
ni hereda curación/origen/generación cuando cambia el contenido de una ruta.
"""
import hashlib
import json
import math
from pathlib import PurePosixPath
import re

MAX_FIELD = 65536


def _id(value):
    if type(value) is not int or not -2**63 <= value <= 2**63 - 1:
        raise ValueError('invalid_copy_version_id')
    return value


def state(conn, copy_id):
    _id(copy_id)
    names = [row[1] for row in conn.execute('PRAGMA table_info(files)')]
    columns = ','.join('"' + name.replace('"', '""') + '"' for name in names)
    lengths = ' OR '.join('length("' + name.replace('"', '""') + '")>?' for name in names)
    if conn.execute(f'SELECT 1 FROM files WHERE id=? AND ({lengths})',
                    (copy_id, *([MAX_FIELD] * len(names)))).fetchone():
        raise ValueError('copy_version_payload_too_large')
    row = conn.execute(f'SELECT {columns} FROM files WHERE id=?', (copy_id,)).fetchone()
    if row is None:
        raise ValueError('copy_version_missing')
    tags = [dict(name=name, source=source, category=category) for name, source, category in conn.execute(
        'SELECT substr(t.name,1,257),substr(ft.source,1,257),substr(t.category,1,257) '
        'FROM file_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.file_id=? ORDER BY t.name LIMIT 257', (copy_id,))]
    if len(tags) > 256 or any(len(value) > 256 for tag in tags for value in tag.values() if value is not None):
        raise ValueError('copy_version_payload_too_large')
    value = dict(file=dict(zip(names, row)), tags=tags)
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise ValueError('unsupported_copy_version_payload') from exc
    if len(raw) > 1024 * 1024:
        raise ValueError('copy_version_payload_too_large')
    return dict(state=value, fingerprint=hashlib.sha256(raw).hexdigest())


def page(conn, *, after_id=None, limit=100, offline_only=True):
    if (after_id is not None and (type(after_id) is not int or not -2**63 <= after_id <= 2**63-1)) or type(limit) is not int or not 1 <= limit <= 256 or type(offline_only) is not bool:
        raise ValueError('invalid_copy_versions_page')
    where = 'is_offline=1' if offline_only else '1'
    params = []
    if after_id is not None:
        where += ' AND id>?'
        params.append(after_id)
    rows = conn.execute('SELECT id,substr(path,1,4097),substr(hash_original,1,65),substr(hash_mod,1,65),rating,is_offline '
                        f'FROM files WHERE {where} ORDER BY id LIMIT ?', (*params, limit)).fetchall()
    if any(not isinstance(row[1], str) or len(row[1]) > 4096 for row in rows):
        raise ValueError('copy_version_payload_too_large')
    if any(value is not None and (not isinstance(value, str) or len(value) > 64) for row in rows for value in row[2:4]):
        raise ValueError('copy_version_payload_too_large')
    keys = ('id', 'path', 'hash_original', 'hash_mod', 'rating', 'is_offline')
    return dict(copies=[dict(zip(keys, row)) for row in rows],
                next_after_id=rows[-1][0] if rows else after_id)


def _reviewed(conn, copy_id, expected_fingerprint):
    if not conn.in_transaction:
        raise RuntimeError('copy_version_requires_transaction')
    if not isinstance(expected_fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', expected_fingerprint):
        raise ValueError('invalid_copy_version_fingerprint')
    current = state(conn, copy_id)
    if current['fingerprint'] != expected_fingerprint:
        raise ValueError('copy_version_changed_since_review')
    return current['state']['file']


def archive(conn, copy_id, *, expected_fingerprint):
    row = _reviewed(conn, copy_id, expected_fingerprint)
    if row['is_offline'] == 0:
        conn.execute('UPDATE files SET is_offline=1 WHERE id=?', (copy_id,))
    from core.path_identity_offline import ids
    ids(conn,[copy_id])
    return True


def make_online(conn, copy_id, *, expected_fingerprint, digest, observation, update_fts):
    row = _reviewed(conn, copy_id, expected_fingerprint)
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('invalid_copy_version_hash')
    if not isinstance(observation, dict) or set(observation) != {'filename', 'size', 'created'}:
        raise ValueError('invalid_copy_version_observation')
    name, size, created = (observation[key] for key in ('filename', 'size', 'created'))
    if (not isinstance(name, str) or name != PurePosixPath(row['path']).name or not name or len(name) > 1024 or
            type(size) is not int or not 0 <= size <= 2**63-1 or type(created) not in (int, float) or
            not math.isfinite(created) or not 0 <= created <= 253402300799):
        raise ValueError('invalid_copy_version_observation')
    occupied = conn.execute('SELECT id FROM files WHERE path=? AND is_offline=0 AND id<>?',
                            (row['path'], copy_id)).fetchone()
    if occupied:
        raise ValueError('copy_version_path_occupied')
    known = row['hash_mod'] or row['hash_original']
    if isinstance(known, str) and digest == known.lower():
        if row['is_offline']:
            conn.execute('UPDATE files SET is_offline=0 WHERE id=?', (copy_id,))
        return True
    # Contenido distinto/desconocido: conservar fila anterior y crear identidad limpia.
    conn.execute('UPDATE files SET is_offline=1 WHERE id=? AND is_offline=0', (copy_id,))
    from core.path_identity_offline import ids
    ids(conn,[copy_id])
    key = conn.execute('INSERT INTO files(path,filename,file_size,created_date,hash_original) VALUES(?,?,?,?,?)',
                       (row['path'], name, size, created, digest)).lastrowid
    update_fts(key)
    return True
