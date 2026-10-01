"""Bounded read-only review of one producer journal; no replay or file mutations.

Native snapshots close before hashing. A second snapshot and file stat detect
observed concurrent changes; this is evidence for manual review, not a lock or
an atomic filesystem/SQLite transaction. Historical receipts are distinct from
the current bytes, identity, generation and curation revision.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from core.derived_copies import _request as derived_request
from core.generation_history import _request as metadata_request, _operation, SHA, FIELDS, COLUMNS
from core.duplicate_snapshot import encode
from core.library_connections import open_reader
from core.library_repository import LibraryRepository
from core.output_paths import safe_path

MAX_JOURNAL = 2 * 1024 * 1024
MAX_FILE = 256 * 1024 * 1024
PHASES = frozenset(('prepared','ready_to_publish','published','complete','review_required','not_published'))


def identity(path):
    value = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(value.st_mode):
        raise ValueError('producer_review_requires_regular_file')
    return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)


def _check(event, deadline):
    if event is not None and event.is_set():
        raise ValueError('producer_review_cancelled')
    if time.monotonic() >= deadline:
        raise ValueError('producer_review_budget')


def _pairs(values):
    result = {}
    for key,value in values:
        if key in result:
            raise ValueError('producer_journal_duplicate_key')
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError('producer_journal_nonfinite')


def _path(value):
    if not isinstance(value,str) or not value or len(value)>4096 or '\x00' in value or not Path(value).is_absolute():
        raise ValueError('producer_journal_invalid_path')
    return value


def _sha(value):
    if not isinstance(value,str) or not SHA.fullmatch(value):
        raise ValueError('producer_journal_invalid_digest')
    return value


def _journal(path):
    path = safe_path(Path(path).absolute(),portable=True)
    before = identity(path)
    if path.name!='journal.json' or before[2]>MAX_JOURNAL:
        raise ValueError('producer_journal_scope_or_size')
    with path.open('rb') as stream:
        raw = stream.read(MAX_JOURNAL+1)
    if len(raw)>MAX_JOURNAL or identity(path)!=before:
        raise ValueError('producer_journal_changed_or_oversized')
    data = json.loads(raw,object_pairs_hook=_pairs,parse_constant=_nonfinite)
    if (not isinstance(data,dict) or type(data.get('version')) is not int or data['version']!=1 or
            not isinstance(data.get('phase'),str) or data['phase'] not in PHASES or type(data.get('published')) is not bool):
        raise ValueError('producer_journal_invalid_header')
    _operation(data.get('operation_id'))
    if path.parent.name!=data['operation_id']:
        raise ValueError('producer_journal_operation_scope')
    request = data.get('request')
    if 'source' in data:
        source,output = _path(data['source']),_path(data.get('destination'))
        backup = _path(data.get('backup'))
        if safe_path(Path(backup).parent) != path.parent or not Path(backup).name.startswith('original.'):
            raise ValueError('producer_journal_backup_scope')
        source_sha = _sha(data.get('source_sha256'))
        result_sha = _sha(data['result_sha256']) if 'result_sha256' in data else None
        kind = 'metadata_export' if data.get('copy_id') is None else 'metadata_edit'
        fingerprint = None
        if request is not None:
            if kind!='metadata_edit' or not isinstance(request,dict):
                raise ValueError('producer_journal_invalid_request')
            fingerprint = metadata_request(data['copy_id'],**request)
            if (request['operation_id']!=data['operation_id'] or request['source_digest']!=source_sha or
                    request['result_digest']!=result_sha or request['expected_fingerprint']!=data.get('expected_fingerprint')):
                raise ValueError('producer_journal_request_conflict')
        elif kind=='metadata_edit' and data['phase'] not in ('prepared','not_published'):
            raise ValueError('producer_journal_missing_request')
        if kind=='metadata_edit':
            from core.library_versions import _id
            _id(data['copy_id'])
            _sha(data.get('expected_fingerprint'))
            if not _path_matches(source,output):
                raise ValueError('producer_journal_edit_path_conflict')
        if kind=='metadata_export' and source.casefold()==output.casefold():
            raise ValueError('producer_journal_export_alias')
    else:
        kind,backup = 'derived',None
        if not isinstance(request,dict):
            raise ValueError('producer_journal_missing_request')
        _,_,_,fingerprint = derived_request(data.get('copy_id'),**request,
            normalize=lambda value:os.path.normpath(value).replace('\\','/'))
        if request['operation_id']!=data['operation_id']:
            raise ValueError('producer_journal_request_conflict')
        source,output = _path(request['source_path']),_path(request['path'])
        source_sha,result_sha = request['source_digest'],request['result_digest']
    return dict(path=str(path),identity=before,sha256=hashlib.sha256(raw).hexdigest(),data=data,
                kind=kind,source=source,output=output,backup=backup,source_sha=source_sha,
                result_sha=result_sha,request_fingerprint=fingerprint)


def _copy(repo, copy_id):
    if copy_id is None:
        return None
    try:
        return repo.get_copy_version_state(copy_id)
    except ValueError as error:
        if str(error)=='copy_version_missing':
            return dict(missing=True,copy_id=copy_id)
        raise


def _native(db_path,journal,event,deadline):
    _check(event,deadline)
    data = journal['data']
    with closing(open_reader(db_path)) as conn:
        conn.set_progress_handler(lambda:int((event is not None and event.is_set()) or time.monotonic()>=deadline),1000)
        conn.execute('BEGIN')
        repo = LibraryRepository(conn)
        if journal['kind']=='derived':
            response = repo.get_derived_copy_receipt(data['operation_id'])
        elif journal['kind']=='metadata_edit':
            response = repo.get_metadata_edit_receipt(data['operation_id'])
        else:
            response = dict(prepared=None,receipt=None)
        receipt = response['receipt']
        source = _copy(repo,data.get('copy_id'))
        output_id = receipt['copy_id'] if receipt is not None else None
        output = source if journal['kind']=='metadata_edit' else _copy(repo,output_id)
        occupied = repo.get_metadata_path_status(journal['output'])['occupied']
        return dict(receipt=response,source=source,output=output,output_path_occupied=occupied)


def _file(path,event,deadline):
    _check(event,deadline)
    try:
        actual = safe_path(path)
        before = identity(actual)
        if before[2]>MAX_FILE:
            raise ValueError('producer_review_file_limit')
        value,size = hashlib.sha256(),0
        with actual.open('rb') as stream:
            while block:=stream.read(1024*1024):
                _check(event,deadline)
                size+=len(block)
                if size>MAX_FILE:
                    raise ValueError('producer_review_file_limit')
                value.update(block)
        if identity(actual)!=before:
            raise ValueError('producer_review_file_changed')
        return dict(path=str(actual),status='verified',sha256=value.hexdigest(),identity=before)
    except FileNotFoundError:
        return dict(path=path,status='missing')
    except (OSError,ValueError) as error:
        if str(error) in ('producer_review_cancelled','producer_review_budget'):
            raise
        return dict(path=path,status='unverified',error=str(error)[:1000])


def _summary(value):
    if value is None or value.get('missing'):
        return value
    row = value['state']['file']
    return dict(copy_id=row['id'],path=row['path'],fingerprint=value['fingerprint'],
        current_sha256=row['hash_mod'] or row['hash_original'],is_offline=row['is_offline'],
        rating=row['rating'],tag_count=len(value['state']['tags']))


def _path_matches(left,right):
    normalized=lambda value:os.path.normpath(value).replace('\\','/').casefold()
    if normalized(left)==normalized(right):
        return True
    # Windows 8.3 aliases may occur in existing Native/journal paths. Resolve
    # only for physical comparison; never rewrite or merge stored identities.
    try:
        a=safe_path(left,exists=os.path.lexists(left))
        b=safe_path(right,exists=os.path.lexists(right))
        return normalized(str(a))==normalized(str(b))
    except (ValueError,OSError):
        return False


def review(db_path,journal_path,*,event=None,budget_seconds=60):
    """Return bounded evidence for one explicit journal. No schema/write/cleanup."""
    if type(budget_seconds) not in (int,float) or not 0<budget_seconds<=300:
        raise ValueError('producer_review_invalid_budget')
    deadline = time.monotonic()+budget_seconds
    _check(event,deadline)
    journal = _journal(journal_path)
    db_path = safe_path(Path(db_path).absolute())
    db_identity = identity(db_path)[:2]
    first = _native(db_path,journal,event,min(deadline,time.monotonic()+10))
    observations, observed_paths = {}, {}
    for name in ('source','output','backup'):
        path = journal[name]
        if path is not None:
            if path not in observed_paths:
                observed_paths[path] = _file(path,event,deadline)
            observations[name] = dict(observed_paths[path])
    second = _native(db_path,journal,event,min(deadline,time.monotonic()+10))
    stable = first==second and identity(db_path)[:2]==db_identity
    stable = stable and identity(Path(journal['path']))==journal['identity']
    for observation in observations.values():
        if observation['status']=='verified':
            try:
                actual=safe_path(observation['path'])
                stable = stable and identity(actual)==observation['identity']
            except (OSError,ValueError):
                stable = False
        elif observation['status']=='missing' and os.path.lexists(observation['path']):
            stable = False
    data,request = journal['data'],journal['data'].get('request')
    receipt = second['receipt']['receipt']
    receipt_match = bool(receipt is not None and journal['request_fingerprint'] is not None and
        receipt['request_fingerprint']==journal['request_fingerprint'] and
        receipt['source_sha256']==journal['source_sha'] and receipt['result_sha256']==journal['result_sha'] and
        (receipt.get('source_copy_id') if journal['kind']=='derived' else receipt['copy_id'])==data.get('copy_id'))
    if receipt_match and journal['kind']=='derived':
        receipt_match = (_path_matches(receipt['source_path'],journal['source']) and
                         _path_matches(receipt['result_path'],journal['output']) and
                         receipt['producer']==request['producer'])
    source,output = _summary(second['source']),_summary(second['output'])
    output_file = observations['output']
    bytes_match = bool(journal['result_sha'] is not None and output_file.get('sha256')==journal['result_sha'])
    native_match = bool(output and not output.get('missing') and request is not None and
        _path_matches(output['path'],journal['output']) and output['current_sha256']==journal['result_sha'])
    generation_match = False
    if native_match:
        row = second['output']['state']['file']
        generation_match = all(row[column]==request['generation'][field] for field,column in zip(FIELDS,COLUMNS))
    if not stable:
        status = 'changed_during_review'
    elif receipt is not None and not receipt_match:
        status = 'receipt_conflict'
    elif receipt_match:
        status = ('confirmed_current' if bytes_match and native_match and generation_match and output['is_offline']==0
                  else 'confirmed_history_current_changed')
    elif journal['kind']=='metadata_export' and bytes_match:
        status = 'export_bytes_verified'
    elif bytes_match:
        status = 'published_bytes_without_receipt'
    elif output_file['status']=='missing':
        status = 'no_current_output'
    elif journal['result_sha'] is None:
        status = 'incomplete_preparation'
    else:
        status = 'current_bytes_unverified_or_changed'
    result = dict(version=1,kind=journal['kind'],operation_id=data['operation_id'],status=status,stable=stable,
        journal_path=journal['path'],journal_sha256=journal['sha256'],journal_phase=data['phase'],
        journal_published_claim=data['published'],receipt=second['receipt'],receipt_matches_request=receipt_match,
        source=source,output=output,files=observations,output_path_occupied=second['output_path_occupied'],
        result_bytes_match=bytes_match,current_native_matches=native_match,current_generation_matches=generation_match,
        source_bytes_match=observations['source'].get('sha256')==journal['source_sha'],
        source_revision_matches=bool(source and not source.get('missing') and source['fingerprint']==(
            request['expected_fingerprint'] if request else data.get('expected_fingerprint'))),
        backup_matches_source=observations.get('backup',{}).get('sha256')==journal['source_sha'] if journal['backup'] else None)
    _check(event,deadline)
    result['review_fingerprint']=hashlib.sha256(encode(result)).hexdigest()
    if len(encode(result))>32*1024:
        raise ValueError('producer_review_report_limit')
    return result
