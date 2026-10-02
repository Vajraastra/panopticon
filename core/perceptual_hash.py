"""Cache derivado explícito por ID/SHA/algoritmo; no curación ni prueba de retiro."""
import re
from pathlib import PureWindowsPath

from core.duplicate_candidates import DIGEST, VALID
from core.duplicate_policy import SHA
from core.duplicate_snapshot import encode
from core.library_scopes import folder_paths,folder_filter,observed_paths

NAME='panopticon_phash_evidence_v1'
DDL=f'''CREATE TABLE {NAME} (
    copy_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    phash TEXT NOT NULL
)'''
PHASH=re.compile('[0-9a-f]{16}')
ALGORITHM=re.compile(r'phash8-v1/[A-Za-z0-9._+:/-]{1,160}')
IMAGES='('+' OR '.join(f"lower(path) LIKE '%{suffix}'" for suffix in
    ('.jpg','.jpeg','.png','.webp','.bmp','.avif'))+')'


def _algorithm(value):
    if not isinstance(value,str) or not ALGORITHM.fullmatch(value):
        raise ValueError('invalid_phash_algorithm')


def status(conn):
    row=conn.execute('SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?',(NAME,)).fetchone()
    if row is None:return dict(prepared=False)
    canonical=lambda sql:' '.join(sql.split()).strip().rstrip(';')
    if row[:2]!=('table',NAME) or not isinstance(row[2],str) or canonical(row[2])!=canonical(DDL):
        raise ValueError('phash_registry_name_conflict')
    return dict(prepared=True)


def prepare(conn):
    if not conn.in_transaction:raise RuntimeError('phash_write_requires_transaction')
    if not status(conn)['prepared']:conn.execute(DDL)
    if not status(conn)['prepared']:raise ValueError('phash_registry_prepare_unconfirmed')
    return True


def page(conn,folders,algorithm,*,after_id=0,through_id=None,limit=256,normalize,evidence=None,scope_filter=None):
    _algorithm(algorithm)
    if (type(after_id) is not int or not 0<=after_id<=2**63-1 or type(limit) is not int or not 1<=limit<=256 or
            through_id is not None and (type(through_id) is not int or not after_id<=through_id<=2**63-1) or
            after_id>0 and through_id is None):
        raise ValueError('invalid_phash_page')
    paths=folder_paths(folders,normalize,check_nested=evidence is None)
    if evidence is None:
        scope,params=folder_filter(paths)
    else:
        observed_paths(paths,evidence)
        conditions=[scope_filter(path) for path in paths]
        scope='('+' OR '.join(clause for clause,_ in conditions)+')'
        params=[value for _,bindings in conditions for value in bindings]
    prepared=status(conn)['prepared']
    total=known=None
    if after_id==0:
        bound='' if through_id is None else ' AND files.id<=?'
        total,known,maximum=conn.execute(f'SELECT count(*),coalesce(sum(CASE WHEN {VALID} THEN 1 ELSE 0 END),0),coalesce(max(id),0) '
            f'FROM files WHERE is_offline=0 AND {IMAGES} AND {scope}'+bound,
            params+([] if through_id is None else [through_id])).fetchone()
        if through_id is None:through_id=maximum
    fields='NULL'
    joins=''
    if prepared:
        fields=f'CASE WHEN e.source_sha256={DIGEST} AND e.algorithm=? AND e.phash=files.phash THEN e.phash END'
        joins=f'LEFT JOIN {NAME} e ON e.copy_id=files.id'
    rows=conn.execute(f'SELECT files.id,substr(path,1,4097),{DIGEST},{fields} FROM files {joins} '
        f'WHERE {VALID} AND {IMAGES} AND {scope} AND files.id>? AND files.id<=? ORDER BY files.id LIMIT ?',
        ([algorithm] if prepared else [])+params+[after_id,through_id,limit+1]).fetchall()
    if any(not isinstance(path,str) or not path or len(path)>4096 or
           cached is not None and (not isinstance(cached,str) or not PHASH.fullmatch(cached))
           for _,path,_,cached in rows):
        raise ValueError('phash_page_requires_review')
    records=[dict(copy_id=key,path=path,source_sha256=digest,phash=cached) for key,path,digest,cached in rows[:limit]]
    result=dict(prepared=prepared,total_images=total,known_images=known,unknown_images=None if total is None else total-known,
        through_id=through_id,
        records=records,has_more=len(rows)>limit,next_after_id=records[-1]['copy_id'] if records else after_id)
    if len(encode(result))>2*1024*1024:raise ValueError('phash_page_payload_limit')
    return result


def store(conn,records,algorithm,*,normalize):
    if not conn.in_transaction:raise RuntimeError('phash_write_requires_transaction')
    _algorithm(algorithm)
    if not isinstance(records,(list,tuple)) or not 1<=len(records)<=64:
        raise ValueError('invalid_phash_batch')
    values=[]
    for row in records:
        if (not isinstance(row,dict) or set(row)!={'copy_id','path','source_sha256','phash'} or
            type(row['copy_id']) is not int or not 1<=row['copy_id']<=2**63-1 or
            not isinstance(row['path'],str) or not row['path'] or len(row['path'])>4096 or '\x00' in row['path'] or
            not isinstance(row['source_sha256'],str) or not SHA.fullmatch(row['source_sha256']) or
            not isinstance(row['phash'],str) or not PHASH.fullmatch(row['phash'])):
            raise ValueError('invalid_phash_batch')
        values.append((row['copy_id'],normalize(row['path']),row['source_sha256'],row['phash']))
    if len({row[0] for row in values})!=len(values) or len(encode(records))>512*1024:
        raise ValueError('invalid_phash_batch')
    if not status(conn)['prepared']:raise ValueError('phash_registry_not_prepared')
    placeholders=','.join('?' for _ in values)
    rows=conn.execute(f'SELECT files.id,path,{DIGEST},files.phash,e.source_sha256,e.algorithm,e.phash '
        f'FROM files LEFT JOIN {NAME} e ON e.copy_id=files.id WHERE {VALID} AND {IMAGES} AND files.id IN ({placeholders})',
        [row[0] for row in values]).fetchall()
    observed={row[0]:row[1:] for row in rows}
    if len(rows)!=len(values) or any(observed[row[0]][:2]!=row[1:3] for row in values):
        raise ValueError('phash_copy_identity_changed')
    for key,path,digest,phash in values:
        if observed[key][2:]==(phash,digest,algorithm,phash):continue
        if conn.execute('UPDATE files SET phash=? WHERE id=? AND is_offline=0',(phash,key)).rowcount!=1:
            raise ValueError('phash_copy_identity_changed')
        conn.execute(f'INSERT INTO {NAME}(copy_id,source_sha256,algorithm,phash) VALUES(?,?,?,?) '
            'ON CONFLICT(copy_id) DO UPDATE SET source_sha256=excluded.source_sha256,algorithm=excluded.algorithm,phash=excluded.phash',
            (key,digest,algorithm,phash))
    return True


def states(conn,paths,algorithm,*,normalize):
    _algorithm(algorithm)
    if (not isinstance(paths,(list,tuple)) or not 1<=len(paths)<=256 or
            any(not isinstance(path,str) or not path or len(path)>4096 or '\x00' in path
                or not PureWindowsPath(path).is_absolute() for path in paths) or len(encode(paths))>512*1024):
        raise ValueError('invalid_phash_state_request')
    normalized=[normalize(path) for path in paths]
    params=[]
    for index,path in enumerate(normalized):params.extend((index,path))
    fields='NULL'
    joins=''
    if status(conn)['prepared']:
        fields=f'CASE WHEN e.source_sha256={DIGEST} AND e.algorithm=? AND e.phash=files.phash THEN e.phash END'
        joins=f'LEFT JOIN {NAME} e ON e.copy_id=files.id'
        params.append(algorithm)
    rows=conn.execute(f"WITH requested(n,wanted_path) AS (VALUES {','.join('(?,?)' for _ in paths)}) "
        f'SELECT requested.wanted_path,files.id,{DIGEST},{fields} FROM requested '
        f'LEFT JOIN files ON files.path=requested.wanted_path AND {VALID} AND {IMAGES} {joins} ORDER BY requested.n',params).fetchall()
    if len(rows)!=len(paths) or [row[0] for row in rows]!=normalized:
        raise ValueError('phash_state_identity_changed')
    if any(cached is not None and (not isinstance(cached,str) or not PHASH.fullmatch(cached)) for _,_,_,cached in rows):
        raise ValueError('phash_state_requires_review')
    result=[dict(path=path,copy_id=key,source_sha256=digest,phash=cached) for path,key,digest,cached in rows]
    if len(encode(result))>2*1024*1024:raise ValueError('phash_page_payload_limit')
    return result
