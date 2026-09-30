"""Curación completa por bloques para dedup; sin conexiones expuestas a módulos."""
import hashlib
import json

MAX_BYTES = 1024 * 1024
MAX_TAGS = 4096


def encode(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise ValueError('unsupported_duplicate_snapshot') from exc


def snapshot(conn, paths, normalize):
    if (not isinstance(paths, (list, tuple)) or len(paths) > 256 or
            any(not isinstance(path, str) or not path or len(path) > 4096 for path in paths)):
        raise ValueError('invalid_duplicate_snapshot_paths')
    normalized = tuple(dict.fromkeys(normalize(path) for path in paths))
    if not normalized:
        return []
    names = [row[1] for row in conn.execute('PRAGMA table_info(files)')]
    if len(names) > 64:
        raise ValueError('unsupported_duplicate_snapshot')
    quoted = ['"' + name.replace('"', '""') + '"' for name in names]
    placeholders = ','.join('?' for _ in normalized)
    where = f'is_offline=0 AND path IN ({placeholders})'
    # Antes de materializar: bytes totales del bloque y longitudes individuales.
    size = '+'.join('coalesce(length(cast(' + column + ' as blob)),0)' for column in quoted)
    excessive = ' OR '.join('length(' + column + ')>65536' for column in quoted)
    total, too_large = conn.execute(f'SELECT coalesce(sum({size}),0),coalesce(max({excessive}),0) '
                                   f'FROM files WHERE {where}', normalized).fetchone()
    if total > MAX_BYTES or too_large:
        raise ValueError('duplicate_snapshot_payload_too_large')
    records = {}
    consumed = 0
    for row in conn.execute('SELECT ' + ','.join(quoted) + f' FROM files WHERE {where}', normalized):
        file = dict(zip(names, row))
        consumed += len(encode(file))
        if consumed > MAX_BYTES:
            raise ValueError('duplicate_snapshot_payload_too_large')
        records[file['id']] = dict(state=dict(file=file, tags=[]))
    if records:
        ids = tuple(records)
        placeholders = ','.join('?' for _ in ids)
        for index, (key, name, source, category) in enumerate(conn.execute(
                'SELECT ft.file_id,substr(t.name,1,257),substr(ft.source,1,257),substr(t.category,1,257) '
                'FROM file_tags ft JOIN tags t ON t.id=ft.tag_id '
                f'WHERE ft.file_id IN ({placeholders}) ORDER BY ft.file_id,t.name LIMIT ?', (*ids, MAX_TAGS + 1))):
            tags = records[key]['state']['tags']
            if (index >= MAX_TAGS or len(tags) >= 256 or
                    any(value is not None and (not isinstance(value, str) or len(value) > 256) for value in (name, source, category))):
                raise ValueError('duplicate_snapshot_payload_too_large')
            tag = dict(name=name, source=source, category=category)
            consumed += len(encode(tag))
            if consumed > MAX_BYTES:
                raise ValueError('duplicate_snapshot_payload_too_large')
            tags.append(tag)
    by_path = {}
    for record in records.values():
        record['fingerprint'] = hashlib.sha256(encode(record['state'])).hexdigest()
        by_path[record['state']['file']['path']] = record
    result = [dict(path=path, **by_path.get(path, dict(state=None, fingerprint=None))) for path in normalized]
    if len(encode(result)) > MAX_BYTES:
        raise ValueError('duplicate_snapshot_payload_too_large')
    return result
