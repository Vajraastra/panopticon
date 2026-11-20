"""Preserve contradictory ingest evidence before coalescing, under the Native owner.

Existing public write signatures remain valid; explicit UUIDs add detailed receipts.
"""
import hashlib
import json
import logging
import math
import re
from uuid import uuid4

from core.library_versions import state
from core.library_transfers import operation

log=logging.getLogger(__name__)
CONFLICTS='panopticon_ingest_conflicts_v1'
RECEIPTS='panopticon_ingest_review_receipts_v1'
SHA=re.compile('[0-9a-fA-F]{64}')
MAX_REQUEST=2*1024*1024
MAX_EVIDENCE=16*1024
DDL=(f'''CREATE TABLE {CONFLICTS} (
    conflict_id TEXT PRIMARY KEY NOT NULL,
    path TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
    first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)''',f'''CREATE TABLE {RECEIPTS} (
    operation_id TEXT PRIMARY KEY NOT NULL,
    request_fingerprint TEXT NOT NULL,
    report_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)''')


def encode(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':')).encode('utf-8')


def canonical_hash(value):
    return value.lower() if type(value) is str and SHA.fullmatch(value) else value


def apply_observed(repo,records,callback):
    """Carry worker identity evidence to the shared primitive, privately.

The storage callback still validates clock/targets before reaching the primitive.
This avoids guessing case aliases twice; hash and size review remain mandatory.
"""
    previous=getattr(repo,'_review_observed_paths',frozenset())
    trusted={repo._normalize_path(record['data']['path']) for record in records
             if record.get('identity') is not None and record.get('clock') is not None}
    repo._review_observed_paths=previous|trusted
    try:return callback(records)
    finally:repo._review_observed_paths=previous


def prepared(conn):
    for name,expected in zip((CONFLICTS,RECEIPTS),DDL):
        row=conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
        if row is None:return False
        if ' '.join(row[0].split())!=' '.join(expected.split()):raise ValueError('ingest_review_schema_requires_review')
    return True


def prepare(repo):
    if not repo.conn.in_transaction:raise RuntimeError('ingest_review_requires_transaction')
    if not prepared(repo.conn):
        for ddl in DDL:repo.conn.execute(ddl)
    return True


def normalize(repo,records,*,observed,producer,watched_folder):
    if type(records) not in (list,tuple) or len(records)>(100 if observed else 10000):raise ValueError('invalid_ingest_review_batch')
    values=json.loads(encode(records))
    for record in values:
        data=record.get('data') if observed and type(record) is dict else record
        if type(data) is not dict:raise ValueError('invalid_ingest_review_data')
        if set(data)-{'path','filename','size','created','hash_original','origin_url','origin_site','artist'}:
            raise ValueError('unsupported_ingest_review_field')
        path=data.get('path');name=data.get('filename');size=data.get('size');created=data.get('created')
        if (type(path) is not str or not path or len(path)>4096 or '\0' in path or
            type(name) is not str or not name or len(name)>1024 or '\0' in name or
            type(size) is not int or size<0 or type(created) not in (int,float) or
            not math.isfinite(created) or created<0):raise ValueError('invalid_ingest_review_observation')
        data['path']=repo._normalize_path(path)
        digest=data.get('hash_original')
        if digest is not None:
            if type(digest) is not str or len(digest)>65536 or '\0' in digest:raise ValueError('invalid_ingest_review_digest')
            # Preserve the existing primitive's opaque legacy values. Real SHA
            # strings compare case-insensitively; this adds no unrelated migration.
            data['hash_original']=digest.lower() if SHA.fullmatch(digest) else digest
        for field,limit in [('origin_url',4096),('origin_site',128),('artist',256)]:
            value=data.get(field)
            if value is not None and (type(value) is not str or len(value)>limit or '\0' in value):
                raise ValueError('invalid_ingest_review_provenance')
    raw=encode(dict(records=values,observed=observed,producer=producer,watched_folder=watched_folder))
    if len(raw)>MAX_REQUEST:raise ValueError('ingest_review_payload_too_large')
    return values,hashlib.sha256(raw).hexdigest()


def receipt(conn,operation_id):
    operation(operation_id)
    if not prepared(conn):return dict(prepared=False,receipt=None)
    row=conn.execute(f'SELECT request_fingerprint,report_json,recorded_at FROM {RECEIPTS} WHERE operation_id=?',
                     (operation_id,)).fetchone()
    return dict(prepared=True,receipt=None if row is None else dict(operation_id=operation_id,
        request_fingerprint=row[0],report=json.loads(row[1]),recorded_at=row[2]))


def status(conn,*,after_id='',limit=50):
    if type(after_id) is not str or type(limit) is not int or not 1<=limit<=100:raise ValueError('invalid_ingest_review_page')
    if not prepared(conn):return dict(prepared=False,open_count=0,records=[],next_after=None)
    count=conn.execute(f"SELECT count(*) FROM {CONFLICTS} WHERE status='open'").fetchone()[0]
    rows=[]
    for row in conn.execute(f"SELECT conflict_id,path,reason,evidence_json,first_seen FROM {CONFLICTS} "
                            "WHERE status='open' AND conflict_id>? ORDER BY conflict_id LIMIT ?",(after_id,limit)):
        rows.append(dict(conflict_id=row[0],path=row[1],reason=row[2],evidence=json.loads(row[3]),first_seen=row[4]))
    return dict(prepared=True,open_count=count,records=rows,next_after=rows[-1]['conflict_id'] if len(rows)==limit else None)


def same_observation(row,data):
    # Legacy Native.created_date may be the catalog's download end time, while
    # stat.birthtime is download start. Neither proves content replacement.
    return row['file_size']==data['size']


def partition(repo,records,*,observed,producer,watched_folder):
    data=[r['data'] if observed else r for r in records]
    paths=list(dict.fromkeys(row['path'] for row in data))
    target_ids=[]
    if observed:
        for record in records:
            target=record.get('target')
            if target is not None:
                if type(target) is not list or len(target)!=2 or type(target[0]) is not int or type(target[1]) is not str:
                    raise ValueError('invalid_ingest_review_target')
                target_ids.append(target[0])
    bindings={};historical=set();historical_bindings={};native_rows={}
    fields=('id','path','is_offline','file_size','created_date','hash_original','hash_mod')
    seen=set()
    for field,wanted in [('path',paths),('id',list(dict.fromkeys(target_ids)))]:
        for start in range(0,len(wanted),256):
            block=wanted[start:start+256];marks=','.join('?' for _ in block)
            for values in repo.conn.execute('SELECT '+','.join(fields)+' FROM files WHERE '+field+' IN ('+marks+')',block):
                key,path,offline=values[:3]
                if key in seen:continue
                seen.add(key)
                native_rows[key]=dict(zip(fields,values))
                if offline:
                    historical.add(path);historical_bindings.setdefault(path,[]).append(key)
                else:bindings.setdefault(path,[]).append(key)
    unproven={row['path'].casefold() for record,row in zip(records,data)
              if row['path'] not in bindings and row['path'] not in getattr(repo,'_review_observed_paths',())
              and (not observed or record.get('identity') is None)}
    aliases={};historical_aliases={}
    if unproven:
        # One scan for this batch's unknown raw paths, never one UDF per file.
        # A fold match requires review; it never supplies an adopted identity.
        for key,path,offline in repo.conn.execute('SELECT id,path,is_offline FROM files'):
            if path.casefold() in unproven:
                target=historical_aliases if offline else aliases
                target.setdefault(path.casefold(),[]).append(key)
    snapshots={};selected=[];groups={}
    for index,(record,row) in enumerate(zip(records,data)):
        target=record.get('target') if observed else None
        ids=[target[0]] if target is not None else bindings.get(row['path'],[])
        alias=not ids and row['path'].casefold() in aliases
        if alias:ids=aliases[row['path'].casefold()]
        if not ids and row['path'] in historical_bindings:
            ids=historical_bindings[row['path']];alias='historical'
        if not ids and row['path'].casefold() in historical_aliases:
            ids=historical_aliases[row['path'].casefold()];alias='historical'
        group=('id',ids[0]) if len(ids)==1 else ('path',row['path'])
        selected.append((ids,group,alias));groups.setdefault(group,set())
        if row.get('hash_original') is not None:groups[group].add(row['hash_original'])
    missing=list({key for ids,_,_ in selected for key in ids}-native_rows.keys())
    for start in range(0,len(missing),256):
        block=missing[start:start+256];marks=','.join('?' for _ in block)
        for values in repo.conn.execute('SELECT '+','.join(fields)+' FROM files WHERE id IN ('+marks+')',block):
            native_rows[values[0]]=dict(zip(fields,values))
    accepted=[];decisions=[];conflicts=[]
    for index,(record,incoming,(ids,group,alias)) in enumerate(zip(records,data,selected)):
        natives=[native_rows[key] for key in ids]
        reason=None;decision='new_copy'
        native=natives[0] if len(natives)==1 else None
        known=set() if native is None else {canonical_hash(native[k]) for k in ('hash_original','hash_mod') if native[k] is not None}
        incoming_digest=incoming.get('hash_original')
        if alias=='historical':reason='historical_path_requires_review'
        elif len(natives)>1:reason='active_identity_ambiguous'
        elif alias:reason='case_alias_requires_observed_identity'
        elif native is not None and native['is_offline']:reason='target_is_offline'
        elif native is None and incoming['path'] in historical:reason='historical_path_requires_review'
        elif len(groups[group])>1 and not groups[group]<=known:reason='incoming_content_ambiguous'
        elif native is not None:
            if incoming_digest is not None and known and incoming_digest not in known:
                reason='catalog_digest_drift'
            elif not same_observation(native,incoming):
                reason='observation_changed_without_verified_version'
            else:
                decision=('compatible_original' if incoming_digest is not None and incoming_digest==canonical_hash(native['hash_original']) else
                          'compatible_modified' if incoming_digest is not None and incoming_digest==canonical_hash(native['hash_mod']) else
                          'first_documented_hash' if incoming_digest is not None and not known else 'unchanged_observation')
        if reason is None:
            if native is not None and not observed:
                # Every observed route eventually uses this same primitive.
                # Preserve the existing date instead of silently migrating its
                # meaning from catalog timestamp to filesystem birthtime.
                incoming['created']=native['created_date']
            accepted.append(record)
            decisions.append(dict(index=index,path=incoming['path'],copy_id=None if native is None else native['id'],decision=decision))
            continue
        evidence=dict(producer=producer,watched_folder=watched_folder,incoming=incoming,
                      incoming_hashes=sorted(groups[group]),
                      observed_proof={k:v for k,v in record.items() if k!='data'} if observed else None,
                      proof_status='unadmitted_claim' if observed else 'inventory_claim',native=[])
        for row in natives:
            key=row['id']
            if key not in snapshots:snapshots[key]=state(repo.conn,key)
            value=snapshots[key]
            evidence['native'].append(dict(id=row['id'],path=row['path'],hash_original=row['hash_original'],
                hash_mod=row['hash_mod'],file_size=row['file_size'],created_date=row['created_date'],fingerprint=value['fingerprint']))
        raw=encode(evidence)
        if len(raw)>MAX_EVIDENCE:raise ValueError('ingest_review_evidence_too_large')
        conflict_id=hashlib.sha256(encode(dict(reason=reason,evidence=evidence))).hexdigest()
        conflicts.append(dict(index=index,path=incoming['path'],reason=reason,conflict_id=conflict_id,evidence=evidence))
    return accepted,decisions,conflicts


def dispatch(repo,records,*,observed,producer,apply,watched_folder=None,operation_id=None):
    if type(records) in (list,tuple) and not records and operation_id is None and not observed:return True
    # The Native owner always starts BEGIN IMMEDIATE. Legacy in-memory callers
    # keep their existing healthy no-op behavior; evidence writes require a txn.
    values,fingerprint=normalize(repo,records,observed=observed,producer=producer,watched_folder=watched_folder)
    explicit=operation_id is not None
    if explicit:
        previous=receipt(repo.conn,operation_id)['receipt']
        if previous is not None:
            if previous['request_fingerprint']!=fingerprint:raise ValueError('ingest_review_operation_conflict')
            return True
    if observed:
        # Validate every claim before classification, including rejected content.
        # A stale/forged binding must retry or fail, never become a conflict ACK.
        # Historical receipt acknowledgement deliberately precedes this check.
        from core.library_path_ingest import validate_observed
        validate_observed(repo, values)
    accepted,decisions,conflicts=partition(repo,values,observed=observed,producer=producer,watched_folder=watched_folder)
    if explicit or conflicts:
        if not repo.conn.in_transaction:raise RuntimeError('ingest_review_requires_transaction')
        if not prepared(repo.conn):raise ValueError('ingest_review_not_prepared')
        operation_id=operation_id or str(uuid4())
    # Both the evidence and valid observations share the owner's transaction.
    if apply(accepted) is not True:raise ValueError('ingest_review_apply_failed')
    if not explicit and not conflicts:return True
    for item in decisions:
        if item['copy_id'] is None:item['copy_id']=repo.get_file_id(item['path'])
    for conflict in conflicts:
        inserted=repo.conn.execute(f'INSERT OR IGNORE INTO {CONFLICTS}(conflict_id,path,reason,evidence_json) VALUES(?,?,?,?)',
            (conflict['conflict_id'],conflict['path'],conflict['reason'],encode(conflict['evidence']).decode())).rowcount
        if inserted:log.warning('Native ingest review required: %s | %s | %s',conflict['conflict_id'],conflict['reason'],conflict['path'])
    report=dict(input_count=len(values),accepted_count=len(accepted),conflict_count=len(conflicts),accepted=decisions,
                conflicts=[{k:v for k,v in item.items() if k!='evidence'} for item in conflicts])
    repo.conn.execute(f'INSERT INTO {RECEIPTS}(operation_id,request_fingerprint,report_json) VALUES(?,?,?)',
        (operation_id,fingerprint,encode(report).decode()))
    return True


def review_notice(saved_receipt):
    """Worker can acknowledge its queue after a committed report, with visibility."""
    value=saved_receipt['receipt']
    if value is None:raise ValueError('ingest_review_confirmation_missing')
    report=value['report']
    return dict(operation_id=value['operation_id'],accepted=report['accepted_count'],
                conflicts=report['conflict_count'],conflict_ids=[row['conflict_id'] for row in report['conflicts']],
                retry=False,requires_review=bool(report['conflict_count']))
