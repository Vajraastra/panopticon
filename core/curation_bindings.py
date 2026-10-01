"""Vista compacta de identidades/revisiones completas; sin nuevos índices o schema."""
from core.duplicate_snapshot import MAX_BYTES, encode
from core.duplicate_policy import SHA


def valid(row, path):
    if not isinstance(row, dict) or row.get('path') != path or 'id' not in row:
        return False
    key, fingerprint = row['id'], row.get('fingerprint')
    if key is None:
        return fingerprint is None and row.get('tags') is None
    return (type(key) is int and -2**63 <= key <= 2**63-1 and
        isinstance(fingerprint,str) and SHA.fullmatch(fingerprint) is not None and
        isinstance(row.get('tags'),tuple))


def compact(records, ordered_paths):
    by_path = {}
    for record in records:
        state = record['state']
        if state is None:
            row = dict(id=None,path=record['path'],rating=None,tags=None,fingerprint=None)
        else:
            file = state['file']
            row = dict(id=file['id'],path=record['path'],rating=file['rating'] or 0,
                tags=tuple(tag['name'] for tag in state['tags']),fingerprint=record['fingerprint'])
        by_path[record['path']] = row
    sizes = {path:len(encode(row)) for path,row in by_path.items()}
    result, consumed = [], 2
    for path in ordered_paths:
        consumed += sizes[path]+1
        if consumed > MAX_BYTES:
            raise ValueError('duplicate_snapshot_payload_too_large')
        result.append(dict(by_path[path]))
    return result
