"""Restauración explícita por copia, con estado esperado y categoría global protegida."""
import hashlib
import json
import math
import re
from pathlib import PurePosixPath

MAX_TAGS = 256
MAX_FIELD = 256
FIELDS = ('hash_original', 'hash_mod', 'rating', 'tags')


def _payload(value):
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError('invalid_restore_state')
    for key in ('hash_original', 'hash_mod'):
        digest = value[key]
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch('[0-9a-fA-F]{64}', digest)):
            raise ValueError('invalid_restore_hash')
    if type(value['rating']) is not int or not 0 <= value['rating'] <= 5:
        raise ValueError('invalid_restore_rating')
    tags = value['tags']
    if not isinstance(tags, (list, tuple)) or len(tags) > MAX_TAGS:
        raise ValueError('restore_payload_too_large')
    names = set()
    result = []
    for tag in tags:
        if not isinstance(tag, dict) or set(tag) != {'name', 'source', 'category'}:
            raise ValueError('invalid_restore_tag')
        name = tag['name']
        if (not isinstance(name, str) or not name or len(name) > MAX_FIELD or
                name != name.strip().lower() or name in names):
            raise ValueError('invalid_restore_tag')
        if any(field is not None and (not isinstance(field, str) or len(field) > MAX_FIELD)
               for field in (tag['source'], tag['category'])):
            raise ValueError('invalid_restore_tag')
        names.add(name)
        result.append(dict(tag))
    return dict(hash_original=value['hash_original'], hash_mod=value['hash_mod'],
                rating=value['rating'], tags=sorted(result, key=lambda tag: tag['name']))


def fingerprint(state):
    """Incluye ID: borrar/recrear una ruta invalida una aprobación anterior."""
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def read_state(conn, path):
    row = conn.execute('SELECT id,hash_original,hash_mod,rating FROM files WHERE is_offline=0 AND path=?', (path,)).fetchone()
    if row is None:
        raise ValueError('file_not_registered')
    tags = []
    for name, source, category in conn.execute(
            'SELECT substr(t.name,1,257),substr(ft.source,1,257),substr(t.category,1,257) '
            'FROM file_tags ft JOIN tags t ON t.id=ft.tag_id '
            'WHERE ft.file_id=? ORDER BY t.name LIMIT 257', (row[0],)):
        tags.append(dict(name=name, source=source, category=category))
    value = _payload(dict(hash_original=row[1], hash_mod=row[2], rating=row[3] or 0, tags=tags))
    value['id'] = row[0]
    return dict(state=value, fingerprint=fingerprint(value))


def restore(conn, path, *, expected_fingerprint, incoming, update_fts, force_fts=False):
    if not conn.in_transaction:
        raise RuntimeError('restore_requires_transaction')
    if not isinstance(expected_fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', expected_fingerprint):
        raise ValueError('invalid_restore_fingerprint')
    value = _payload(incoming)
    current = read_state(conn, path)
    if current['fingerprint'] != expected_fingerprint:
        raise ValueError('curation_changed_since_review')
    state = current['state']
    identity = (value['hash_original'], value['hash_mod'])
    if not any(identity):
        raise ValueError('identity_missing')
    if identity != (state['hash_original'], state['hash_mod']):
        raise ValueError('identity_changed')
    if all(state[key] == value[key] for key in FIELDS):
        if force_fts:
            update_fts(state['id'])
        return True
    new = {tag['name']: tag for tag in value['tags']}
    old = {tag['name']: tag for tag in state['tags']}
    known = {}
    if new:
        marks = ','.join('?' for _ in new)
        for key, name, category in conn.execute(f'SELECT id,name,category FROM tags WHERE name IN ({marks})', tuple(new)):
            if category != new[name]['category']:
                raise ValueError('tag_category_conflict')
            known[name] = key
    # Sólo crea tags ausentes; nunca cambia la categoría compartida de un tag.
    for name, tag in new.items():
        if name not in known:
            known[name] = conn.execute('INSERT INTO tags(name,category) VALUES(?,?)',
                                      (name, tag['category'])).lastrowid
    removed = tuple(set(old) - set(new))
    if removed:
        marks = ','.join('?' for _ in removed)
        conn.execute(f'DELETE FROM file_tags WHERE file_id=? AND tag_id IN '
                     f'(SELECT id FROM tags WHERE name IN ({marks}))', (state['id'], *removed))
    for name, tag in new.items():
        if name not in old:
            conn.execute('INSERT INTO file_tags(file_id,tag_id,source) VALUES(?,?,?)',
                         (state['id'], known[name], tag['source']))
        elif old[name]['source'] != tag['source']:
            conn.execute('UPDATE file_tags SET source=? WHERE file_id=? AND tag_id=?',
                         (tag['source'], state['id'], known[name]))
    if state['rating'] != value['rating']:
        conn.execute('UPDATE files SET rating=? WHERE id=?', (value['rating'], state['id']))
    if force_fts or set(old) != set(new):
        update_fts(state['id'])
    return True


def register_copy(conn, path, *, incoming, observation, update_fts):
    """Alta explícita sólo si sigue ausente; la verificación de bytes vive en worker."""
    if not conn.in_transaction:
        raise RuntimeError('restore_requires_transaction')
    value = _payload(incoming)
    if not (value['hash_original'] or value['hash_mod']):
        raise ValueError('identity_missing')
    if not isinstance(observation, dict) or set(observation) != {'filename', 'size', 'created'}:
        raise ValueError('invalid_curation_observation')
    name, size, created = (observation[key] for key in ('filename', 'size', 'created'))
    if (not isinstance(name, str) or not name or len(name) > 1024 or name != PurePosixPath(path).name or
            type(size) is not int or not 0 <= size <= 2**63-1 or type(created) not in (int, float) or
            not 0 <= created <= 253402300799 or not math.isfinite(created)):
        raise ValueError('invalid_curation_observation')
    if conn.execute('SELECT 1 FROM files WHERE is_offline=0 AND path=?', (path,)).fetchone() is not None:
        raise ValueError('file_now_registered')
    if conn.execute('SELECT 1 FROM files WHERE is_offline=1 AND path=?', (path,)).fetchone() is not None:
        raise ValueError('version_identity_required')
    conn.execute('INSERT INTO files(path,filename,file_size,created_date,hash_original,hash_mod) '
                 'VALUES(?,?,?,?,?,?)', (path, name, size, created, value['hash_original'], value['hash_mod']))
    expected = read_state(conn, path)['fingerprint']
    return restore(conn, path, expected_fingerprint=expected, incoming=value,
                   update_fts=update_fts, force_fts=True)


def adopt_identity(conn, path, *, expected_fingerprint, incoming, update_fts):
    """Completa identidad sólo si ambos hashes siguen NULL y la copia no cambió."""
    if not conn.in_transaction:
        raise RuntimeError('restore_requires_transaction')
    if not isinstance(expected_fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', expected_fingerprint):
        raise ValueError('invalid_restore_fingerprint')
    value = _payload(incoming)
    if not (value['hash_original'] or value['hash_mod']):
        raise ValueError('identity_missing')
    current = read_state(conn, path)
    if current['fingerprint'] != expected_fingerprint:
        raise ValueError('curation_changed_since_review')
    if (current['state']['hash_original'], current['state']['hash_mod']) != (None, None):
        raise ValueError('identity_already_known')
    conn.execute('UPDATE files SET hash_original=?,hash_mod=? WHERE id=?',
                 (value['hash_original'], value['hash_mod'], current['state']['id']))
    expected = read_state(conn, path)['fingerprint']
    return restore(conn, path, expected_fingerprint=expected, incoming=value, update_fts=update_fts)
