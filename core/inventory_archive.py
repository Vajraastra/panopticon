"""Archivar sólo IDs revisados del inventario, sin transferir o borrar curación."""
import os
from core.duplicate_policy import SHA
from core.duplicate_snapshot import snapshot, encode


def archive_missing(conn, approved, *, folder, normalize, scope=None):
    if not conn.in_transaction:
        raise RuntimeError('inventory_archive_requires_transaction')
    if (not isinstance(folder, str) or not folder or len(folder) > 4096 or
            not os.path.isabs(folder) or normalize(folder) != folder or
            not isinstance(approved, (list, tuple)) or not 1 <= len(approved) <= 256 or
            len(encode(approved)) > 256*1024):
        raise ValueError('invalid_inventory_archive')
    from core.path_identity_registry import prepared
    is_prepared=prepared(conn)
    paths, ids = [], set()
    prefix = folder.rstrip('/')+'/'
    key = (lambda value: value.casefold()) if os.name == 'nt' else (lambda value: value)
    for record in approved:
        if (not isinstance(record, dict) or set(record) != {'path', 'copy_id', 'fingerprint'} or
                not isinstance(record['path'], str) or not record['path'] or len(record['path']) > 4096 or
                normalize(record['path']) != record['path'] or (not is_prepared and not key(record['path']).startswith(key(prefix))) or
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
    from core.library_transfers import reserved
    if reserved(conn, ids):
        raise ValueError('inventory_archive_transfer_pending')
    if is_prepared:
        if type(scope) is dict and set(scope)=={'catalogue_scope'}:
            from core.inventory_unavailable import require_known_missing
            require_known_missing(conn)
            from core.folder_catalogue_scope import condition
            where,params,stamp=condition(conn,folder,scope['catalogue_scope'],alias='f.',watched_required=False)
            if stamp['mode']!='observed_directory':raise ValueError('inventory_archive_scope_unavailable')
            members={row[0] for row in conn.execute('SELECT r.file_id FROM path_identity_records r '
                'JOIN files f ON f.id=r.file_id WHERE r.file_id IN ('+','.join('?' for _ in ids)+') '
                "AND r.status='unavailable' AND r.reason IN ('winerror_2','winerror_3') AND r.is_offline=0 AND f.is_offline=0 "
                'AND r.stored_path=f.path COLLATE BINARY AND '+where,[*ids,*params])}
            if members!=ids:raise ValueError('inventory_archive_scope_changed')
        else:
            from core.path_identity import local_path
            from core.folder_queries import bounds
            if (type(scope) is not dict or set(scope) != {'path','canonical','physical'}
                    or scope['path'] != local_path(folder) or scope['canonical'] != local_path(scope['canonical'])
                    or type(scope['physical']) not in (list,tuple) or len(scope['physical']) != 7
                    or any(type(v) is not int or not 0<=v<=0xffffffff for v in scope['physical'])):
                raise ValueError('path_identity_review_required')
            low,high=bounds(scope['canonical'])
            members={row[0] for row in conn.execute('SELECT r.file_id FROM path_identity_records r '
                'JOIN files f ON f.id=r.file_id WHERE r.file_id IN ('+','.join('?' for _ in ids)+') '
                "AND r.status='resolved' AND r.is_offline=0 AND f.is_offline=0 AND r.stored_path=f.path COLLATE BINARY "
                'AND r.canonical>=? COLLATE BINARY AND r.canonical<? COLLATE BINARY',[*ids,low,high])}
            if members!=ids:
                raise ValueError('inventory_archive_scope_changed')
    elif scope is not None:
        raise ValueError('path_identity_sql_state_changed')
    placeholders = ','.join('?' for _ in ids)
    cursor = conn.execute(f'UPDATE files SET is_offline=1 WHERE is_offline=0 AND id IN ({placeholders})', tuple(ids))
    if cursor.rowcount != len(ids):
        raise ValueError('inventory_archive_identity_changed')
    from core.path_identity_offline import ids as sync_offline
    sync_offline(conn,list(ids))
    # FTS, generaciones, hashes, tags y recibos permanecen; ningún trabajo de disco
    # en el owner. El worker aporta ausencia observada, sin atomicidad FS/SQLite.
    return True
