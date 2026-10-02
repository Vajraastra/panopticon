"""Native receipts for explicit Pickler transfers; SQL only, no filesystem I/O."""
import hashlib
import re
from uuid import UUID

from core.derived_copies import _budget
from core.duplicate_snapshot import encode
from core.library_versions import _id, state
from core.path_identity import local_path
from core.path_identity_registry import prepared as identity_prepared, store as store_identity

NAME = 'panopticon_transfers_v1'
SOURCE_INDEX = NAME + '_source_pending'
DEST_INDEX = NAME + '_destination_pending'
PATHS = 'panopticon_transfer_paths_v1'
PATH_DDL = f'''CREATE TABLE {PATHS} (
    operation_id TEXT NOT NULL REFERENCES {NAME}(operation_id),
    path TEXT NOT NULL COLLATE BINARY,
    PRIMARY KEY(operation_id,path)
)'''
DDL = f'''CREATE TABLE {NAME} (
    operation_id TEXT PRIMARY KEY NOT NULL,
    source_copy_id INTEGER NOT NULL REFERENCES files(id),
    result_copy_id INTEGER REFERENCES files(id),
    action TEXT NOT NULL CHECK(action IN ('move','copy','discard')),
    source_path TEXT NOT NULL,
    result_path TEXT NOT NULL,
    digest TEXT NOT NULL,
    caption_digest TEXT,
    source_fingerprint TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    result_fingerprint TEXT,
    phase TEXT NOT NULL CHECK(phase IN ('approved','complete','aborted')),
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)'''
INDEXES = [(SOURCE_INDEX, f"CREATE UNIQUE INDEX {SOURCE_INDEX} ON {NAME}(source_copy_id) WHERE phase='approved'"),
           (DEST_INDEX, f"CREATE UNIQUE INDEX {DEST_INDEX} ON {NAME}(result_path COLLATE BINARY) WHERE phase='approved'")]
FIELDS = ('operation_id','source_copy_id','result_copy_id','action','source_path','result_path',
          'digest','caption_digest','source_fingerprint','request_fingerprint','result_fingerprint','phase','recorded_at')
SHA = re.compile('[0-9a-f]{64}')


def status(conn):
    expected = [(NAME, 'table', DDL), *((name, 'index', ddl) for name, ddl in INDEXES), (PATHS,'table',PATH_DDL)]
    present = []
    canonical = lambda value:' '.join(value.split()).rstrip(';')
    for name, kind, ddl in expected:
        row = conn.execute('SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is not None and (row[:2] != (kind, PATHS if name==PATHS else NAME) or type(row[2]) is not str or canonical(row[2]) != canonical(ddl)):
            raise ValueError('transfer_schema_requires_review')
        present.append(row is not None)
    if (any(present[:3]) and not all(present[:3])) or (present[3] and not all(present[:3])):
        raise ValueError('transfer_schema_requires_review')
    return dict(prepared=all(present))


def prepare(conn):
    with _budget(conn):
        if not status(conn)['prepared']:
            if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(NAME,)).fetchone() is None:
                conn.execute(DDL)
                for _, ddl in INDEXES:conn.execute(ddl)
            conn.execute(PATH_DDL)
        if not status(conn)['prepared']:
            raise ValueError('transfer_schema_requires_review')
    return True


def operation(value):
    if type(value) is not str:
        raise ValueError('invalid_transfer_operation')
    try:
        parsed = UUID(value)
        if str(parsed) != value or parsed.version != 4:
            raise ValueError()
    except ValueError as exc:
        raise ValueError('invalid_transfer_operation') from exc
    return value


def receipt(conn, operation_id):
    operation(operation_id)
    ready = status(conn)['prepared']
    if not ready:
        return dict(prepared=False, receipt=None)
    row = conn.execute('SELECT '+','.join(FIELDS)+f' FROM {NAME} WHERE operation_id=?', (operation_id,)).fetchone()
    if row is not None:
        value = dict(zip(FIELDS, row))
        if value['phase'] not in ('approved', 'complete', 'aborted') or value['action'] not in ('move', 'copy', 'discard'):
            raise ValueError('transfer_receipt_requires_review')
        for key in ('source_path', 'result_path'):
            local_path(value[key])
        for key in ('digest','caption_digest','source_fingerprint','request_fingerprint','result_fingerprint'):
            item = value[key]
            if ((item is None and key in ('digest','source_fingerprint','request_fingerprint')) or
                    (item is not None and (type(item) is not str or not SHA.fullmatch(item)))):
                raise ValueError('transfer_receipt_requires_review')
        _id(value['source_copy_id'])
        if value['result_copy_id'] is not None:
            _id(value['result_copy_id'])
        if len(encode(value)) > 65536:
            raise ValueError('transfer_receipt_requires_review')
    else:
        value = None
    return dict(prepared=True, receipt=value)


def request(value):
    required = {'operation_id','source_copy_id','action','source_path','result_path',
                'digest','caption_digest','source_fingerprint','destination_paths'}
    if type(value) is not dict or not required <= set(value) or set(value) - required - {'source_paths'}:
        raise ValueError('invalid_transfer_request')
    value = dict(value)
    operation(value['operation_id'])
    _id(value['source_copy_id'])
    if value['action'] not in ('move','copy','discard'):
        raise ValueError('invalid_transfer_action')
    for key in ('source_path','result_path'):
        value[key] = local_path(value[key])
    if value['source_path'] == value['result_path']:
        raise ValueError('transfer_same_path')
    aliases = value['destination_paths']
    if type(aliases) not in (list, tuple) or not 1 <= len(aliases) <= 32:
        raise ValueError('invalid_transfer_destinations')
    value['destination_paths'] = sorted(set(local_path(path) for path in aliases))
    if value['result_path'] not in value['destination_paths']:
        raise ValueError('invalid_transfer_destinations')
    if 'source_paths' in value:
        paths=value['source_paths']
        if type(paths) not in (list,tuple) or not 1<=len(paths)<=32:
            raise ValueError('invalid_transfer_sources')
        value['source_paths']=sorted(set(local_path(path) for path in paths))
        if value['source_path'] not in value['source_paths']:raise ValueError('invalid_transfer_sources')
    for key in ('digest', 'source_fingerprint'):
        if type(value[key]) is not str or not SHA.fullmatch(value[key]):
            raise ValueError('invalid_transfer_digest')
    caption = value['caption_digest']
    if caption is not None and (type(caption) is not str or not SHA.fullmatch(caption)):
        raise ValueError('invalid_transfer_digest')
    raw = encode(value)
    if len(raw) > 256 * 1024:
        raise ValueError('transfer_payload_too_large')
    return value, hashlib.sha256(raw).hexdigest()


def _source(conn, value):
    record = state(conn, value['source_copy_id'])
    row = record['state']['file']
    if record['fingerprint'] != value['source_fingerprint'] or row['is_offline'] or row['path'] != value['source_path']:
        raise ValueError('transfer_source_changed')
    known = row.get('hash_mod') or row.get('hash_original')
    if known is not None and (type(known) is not str or known.lower() != value['digest']):
        raise ValueError('transfer_source_digest_changed')
    return record


def _destination(repo, value, check):
    from core.generation_history import path_status
    if repo._derived_paths is not None:
        occupied = repo._derived_paths.occupied([path.casefold() for path in value['destination_paths']], check)
    else:
        occupied = path_status(repo.conn, value['destination_paths'], normalize=repo._normalize_path)['occupied']
    if occupied:
        raise ValueError('transfer_destination_requires_review')
    repo.conn.create_function('transfer_path_key_v1', 1, lambda path: path.casefold(), deterministic=True)
    aliases = [path.casefold() for path in value['destination_paths']]
    pending = repo.conn.execute(f'SELECT 1 FROM {NAME} WHERE phase=\'approved\' '
        f'AND operation_id<>? AND transfer_path_key_v1(result_path) IN ({",".join("?" for _ in aliases)}) LIMIT 1',
        (value['operation_id'], *aliases)).fetchone()
    if pending:
        raise ValueError('transfer_destination_requires_review')
    pending = repo.conn.execute(f'SELECT 1 FROM {NAME} t WHERE t.phase=\'approved\' AND t.operation_id<>? '
        f'AND EXISTS(SELECT 1 FROM {PATHS} p WHERE p.operation_id=t.operation_id '
        f'AND p.path IN ({",".join("?" for _ in aliases)})) LIMIT 1',
        (value['operation_id'],*aliases)).fetchone()
    if pending:raise ValueError('transfer_destination_requires_review')
    check()


def approve(repo, incoming):
    with _budget(repo.conn) as check:
        value, fingerprint = request(incoming)
        saved = receipt(repo.conn, value['operation_id'])
        if not saved['prepared']:
            raise ValueError('transfer_schema_not_prepared')
        previous = saved['receipt']
        if previous is not None:
            if previous['request_fingerprint'] != fingerprint or previous['phase'] == 'aborted':
                raise ValueError('transfer_operation_conflict')
            if previous['phase'] == 'approved':
                _source(repo.conn, value)
                _destination(repo, value, check)
            return True
        _source(repo.conn, value)
        _destination(repo, value, check)
        repo.conn.execute(f'INSERT INTO {NAME}(operation_id,source_copy_id,action,source_path,result_path,'
            'digest,caption_digest,source_fingerprint,request_fingerprint,phase) VALUES(?,?,?,?,?,?,?,?,?,\'approved\')',
            tuple(value[key] for key in ('operation_id','source_copy_id','action','source_path','result_path',
                'digest','caption_digest','source_fingerprint')) + (fingerprint,))
        paths={p.casefold() for p in [*value['destination_paths'],*value.get('source_paths',[value['source_path']])]}
        repo.conn.executemany(f'INSERT INTO {PATHS}(operation_id,path) VALUES(?,?)',
            [(value['operation_id'],path) for path in sorted(paths)])
    return True


def commit(repo, incoming, observation, identity):
    with _budget(repo.conn) as check:
        value, fingerprint = request(incoming)
        saved = receipt(repo.conn, value['operation_id'])['receipt']
        if saved is None or saved['request_fingerprint'] != fingerprint or saved['phase'] == 'aborted':
            raise ValueError('transfer_operation_conflict')
        if saved['phase'] == 'complete':
            return True  # Historical acknowledgement, never replay current curation.
        if (type(observation) is not dict or set(observation) != {'size'} or
                type(observation['size']) is not int or not 0 <= observation['size'] <= 2**63-1):
            raise ValueError('invalid_transfer_observation')
        # Validate worker evidence even when this library has not prepared identities.
        from core.path_identity_registry import _record
        if (type(identity) is not dict or set(identity) != {'canonical', 'physical'} or
                identity['canonical'] != value['result_path']):
            raise ValueError('invalid_transfer_identity')
        result = dict(file_id=value['source_copy_id'], stored_path=value['result_path'], is_offline=0,
            status='resolved', canonical=identity.get('canonical') if type(identity) is dict else None,
            physical=identity.get('physical') if type(identity) is dict else None, reason=None)
        _record(result)
        if identity_prepared(repo.conn):
            occupied = repo.conn.execute('SELECT file_id FROM path_identity_records WHERE canonical=? '
                'AND status=\'resolved\' AND is_offline=0 AND file_id<>? LIMIT 1',
                (result['canonical'], value['source_copy_id'])).fetchone()
            if occupied:
                raise ValueError('transfer_destination_requires_review')
        current = _source(repo.conn, value)['state']['file']
        _destination(repo, value, check)
        if value['action'] == 'copy':
            file = dict(current, path=value['result_path'], filename=value['result_path'].rsplit('/',1)[-1],
                        file_size=observation['size'], is_offline=0)
            del file['id']
            if file.get('hash_original') is None:
                file['hash_original'] = value['digest']
            columns = list(file)
            quoted = ['"'+name.replace('"','""')+'"' for name in columns]
            key = repo.conn.execute('INSERT INTO files('+','.join(quoted)+') VALUES('
                +','.join('?' for _ in columns)+')', [file[name] for name in columns]).lastrowid
            repo.conn.execute('INSERT INTO file_tags(file_id,tag_id,source) SELECT ?,tag_id,source '
                              'FROM file_tags WHERE file_id=?', (key, current['id']))
        else:
            key = current['id']
            repo.conn.execute('UPDATE files SET path=?,filename=?,file_size=? WHERE id=?',
                (value['result_path'], value['result_path'].rsplit('/',1)[-1], observation['size'], key))
        repo._fts_sync_file(key)
        if identity_prepared(repo.conn):
            result['file_id'] = key
            store_identity(repo.conn, [result])
        result_fingerprint = state(repo.conn, key)['fingerprint']
        repo.conn.execute(f'UPDATE {NAME} SET result_copy_id=?,result_fingerprint=?,phase=\'complete\' '
                          'WHERE operation_id=? AND phase=\'approved\'', (key,result_fingerprint,value['operation_id']))
    return True


def abort(repo, operation_id, *, expected_fingerprint):
    with _budget(repo.conn):
        current = receipt(repo.conn, operation_id)['receipt']
        if current is None or current['request_fingerprint'] != expected_fingerprint or current['phase'] == 'complete':
            raise ValueError('transfer_operation_conflict')
        repo.conn.execute(f'UPDATE {NAME} SET phase=\'aborted\' WHERE operation_id=?', (operation_id,))
    return True


def reserved(conn, copy_ids):
    """Incomplete transfers cannot be mistaken for missing source files."""
    ready=status(conn)['prepared']
    if not ready and conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(NAME,)).fetchone() is None:
        return False
    return conn.execute(f'SELECT 1 FROM {NAME} WHERE source_copy_id IN '
        f'({",".join("?" for _ in copy_ids)}) AND phase=\'approved\' LIMIT 1', tuple(copy_ids)).fetchone() is not None


def sources(conn, paths, normalize):
    """Conservative unprepared lookup: ambiguity/offline history require review."""
    from core.generation_history import path_keys
    keys = path_keys(paths, normalize=normalize)
    conn.create_function('transfer_path_key_v1', 1, lambda path:path.casefold() if isinstance(path,str) else None, deterministic=True)
    rows = conn.execute('SELECT id,is_offline FROM files WHERE transfer_path_key_v1(path) IN ('
        + ','.join('?' for _ in keys)+') ORDER BY id LIMIT 2',keys).fetchall()
    return dict(records=rows)


def guard_ingestion(conn, paths):
    if not paths:return
    ready=status(conn)['prepared']
    if not ready:
        if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(NAME,)).fetchone() is not None and conn.execute(
                f'SELECT 1 FROM {NAME} WHERE phase=\'approved\' LIMIT 1').fetchone():
            raise ValueError('transfer_ingestion_pending')
        return
    conn.create_function('transfer_path_key_v1', 1, lambda path:path.casefold() if isinstance(path,str) else None, deterministic=True)
    # Legacy SQL-only approvals did not persist aliases. Never infer them while
    # consuming an inventory: keep ambiguous pending work for explicit review.
    if conn.execute(f'SELECT 1 FROM {NAME} t WHERE phase=\'approved\' '
            f'AND NOT EXISTS(SELECT 1 FROM {PATHS} p WHERE p.operation_id=t.operation_id) LIMIT 1').fetchone():
        raise ValueError('transfer_ingestion_pending')
    canonical = ''
    if identity_prepared(conn):
        canonical = f" OR transfer_path_key_v1((SELECT canonical FROM path_identity_records WHERE file_id={NAME}.source_copy_id)) IN ({{}})"
    for start in range(0,len(paths),100):
        keys = [path.casefold() for path in paths[start:start+100]]
        marks = ','.join('?' for _ in keys)
        query = f'SELECT 1 FROM {NAME} WHERE phase=\'approved\' AND ('
        query += f'transfer_path_key_v1(source_path) IN ({marks}) OR transfer_path_key_v1(result_path) IN ({marks})'
        args = [*keys,*keys]
        if canonical:
            query += canonical.format(marks); args.extend(keys)
        if conn.execute(query+') LIMIT 1',args).fetchone():
            raise ValueError('transfer_ingestion_pending')
        if conn.execute(f'SELECT 1 FROM {NAME} t WHERE t.phase=\'approved\' '
                f'AND EXISTS(SELECT 1 FROM {PATHS} p WHERE p.operation_id=t.operation_id '
                f'AND p.path IN ({marks})) LIMIT 1',keys).fetchone():
            raise ValueError('transfer_ingestion_pending')


def retire_copy(repo, operation_id):
    """Explicit Copy undo; retain the independent copy's offline history."""
    with _budget(repo.conn):
        saved = receipt(repo.conn, operation_id)['receipt']
        if saved is None or saved['phase'] != 'complete' or saved['action'] != 'copy':
            raise ValueError('transfer_copy_undo_requires_review')
        source = state(repo.conn, saved['source_copy_id'])
        result = state(repo.conn, saved['result_copy_id'])
        reviewed = dict(result['state'], file=dict(result['state']['file'], is_offline=0))
        result_fingerprint = hashlib.sha256(encode(reviewed)).hexdigest()
        if (source['fingerprint'] != saved['source_fingerprint'] or
                result_fingerprint != saved['result_fingerprint'] or
                result['state']['file']['is_offline'] not in (0,1) or
                source['state']['file']['is_offline']):
            raise ValueError('transfer_copy_undo_changed')
        repo.conn.execute('UPDATE files SET is_offline=1 WHERE id=? AND is_offline=0', (saved['result_copy_id'],))
        from core.path_identity_offline import ids as sync_offline
        sync_offline(repo.conn,[saved['result_copy_id']])
    return True


def approve_restore(repo, incoming, expected_source_fingerprint, identity):
    """Explicit recovery preserves current source curation and pending guards.

    Worker pins the restored source before this command, removes only its own
    verified unregistered publications, then aborts the reservation. No FS I/O.
    """
    with _budget(repo.conn) as check:
        value, fingerprint = request(incoming)
        saved = receipt(repo.conn,value['operation_id'])['receipt']
        if saved is None or saved['phase'] != 'approved' or saved['request_fingerprint'] != fingerprint:
            raise ValueError('transfer_restore_requires_pending')
        current = state(repo.conn,value['source_copy_id'])
        file = current['state']['file']
        known = file.get('hash_mod') or file.get('hash_original')
        if (current['fingerprint'] != expected_source_fingerprint or file['is_offline'] or
                file['path'] != value['source_path'] or (known is not None and known.lower() != value['digest'])):
            raise ValueError('transfer_restore_source_changed')
        _destination(repo,value,check)
        if type(identity) is not dict or set(identity) != {'canonical','physical'}:
            raise ValueError('invalid_transfer_identity')
        from core.path_identity_registry import _record
        record=dict(file_id=file['id'],stored_path=file['path'],is_offline=0,status='resolved',
            canonical=identity['canonical'],physical=identity['physical'],reason=None)
        _record(record)
        if identity_prepared(repo.conn):store_identity(repo.conn,[record])
    return True
