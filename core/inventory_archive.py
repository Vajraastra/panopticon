"""Archivar sólo IDs revisados del inventario, sin transferir o borrar curación."""
import os
from core.duplicate_policy import SHA
from core.duplicate_snapshot import snapshot, encode


def archive_missing(conn, approved, *, folder, normalize):
    if not conn.in_transaction:
        raise RuntimeError('inventory_archive_requires_transaction')
    if (not isinstance(folder, str) or not folder or len(folder) > 4096 or
            not os.path.isabs(folder) or normalize(folder) != folder or
            not isinstance(approved, (list, tuple)) or not 1 <= len(approved) <= 256 or
            len(encode(approved)) > 256*1024):
        raise ValueError('invalid_inventory_archive')
    paths, ids = [], set()
    prefix = folder.rstrip('/')+'/'
    key = (lambda value: value.casefold()) if os.name == 'nt' else (lambda value: value)
    for record in approved:
        if (not isinstance(record, dict) or set(record) != {'path', 'copy_id', 'fingerprint'} or
                not isinstance(record['path'], str) or not record['path'] or len(record['path']) > 4096 or
                normalize(record['path']) != record['path'] or not key(record['path']).startswith(key(prefix)) or
                type(record['copy_id']) is not int or not -2**63 <= record['copy_id'] <= 2**63-1 or record['copy_id'] in ids or
                not isinstance(record['fingerprint'], str) or not SHA.fullmatch(record['fingerprint'])):
            raise ValueError('invalid_inventory_archive')
        paths.append(record['path']); ids.add(record['copy_id'])
    if len(set(paths)) != len(paths):
        raise ValueError('invalid_inventory_archive')
    current = snapshot(conn, paths, normalize)
    for before, now in zip(approved, current):
        if (now['state'] is None or now['fingerprint'] != before['fingerprint'] or
                now['state']['file']['id'] != before['copy_id']):
            raise ValueError('inventory_archive_identity_changed')
    placeholders = ','.join('?' for _ in ids)
    cursor = conn.execute(f'UPDATE files SET is_offline=1 WHERE is_offline=0 AND id IN ({placeholders})', tuple(ids))
    if cursor.rowcount != len(ids):
        raise ValueError('inventory_archive_identity_changed')
    # FTS, generaciones, hashes, tags y recibos permanecen; ningún trabajo de disco
    # en el owner. El worker aporta ausencia observada, sin atomicidad FS/SQLite.
    return True
