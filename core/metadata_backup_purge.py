"""Explicit preview and confirmed Windows purge of one original Metadata Hub backup.

The producer journal, history, current copy and curation survive. Open handles
protect file identities through deletion; SQLite/filesystem are not one transaction.
"""
from concurrent.futures import TimeoutError
import hashlib
import os
from pathlib import Path
from uuid import uuid4

from core.curation_apply import _identity
from core.duplicate_snapshot import encode
from core.generation_history import SHA, FIELDS, COLUMNS
from core.metadata_backup_policy import history_fingerprint
from core.output_paths import safe_path, path_variants
from core.pinned_file import PinnedFile
from core.producer_journal_review import review, _journal, _path_matches
from core.producer_reconciliation import _scope, _guard, _save, _generation


def _cancel(event):
    if event is not None and event.is_set():
        raise ValueError('metadata_backup_purge_cancelled')


def _native(service, journal, event):
    _cancel(event)
    record = service.read('get_metadata_edit_history_record', journal['data']['operation_id'],
                          cancel_event=event)['record']
    if (record is None or record['copy_id']!=journal['data']['copy_id'] or
            record['request_fingerprint']!=journal['request_fingerprint'] or
            record['source_sha256']!=journal['source_sha'] or record['result_sha256']!=journal['result_sha'] or
            record['previous']['observed']!=journal['data']['request']['source_generation'] or
            ('native_previous' in journal['data'] and record['previous']['native']!=journal['data']['native_previous'])):
        raise ValueError('metadata_backup_purge_history_requires_review')
    previous = record['previous']['native']['hash_mod'] or record['previous']['native']['hash_original']
    if previous is not None and previous.lower()!=journal['source_sha']:
        raise ValueError('metadata_backup_purge_history_requires_review')
    current = service.read('get_copy_version_state', record['copy_id'], cancel_event=event)
    row = current['state']['file']
    digest = row['hash_mod'] or row['hash_original']
    if (row['is_offline']!=0 or not _path_matches(row['path'],journal['output']) or
            not isinstance(digest,str) or not SHA.fullmatch(digest)):
        raise ValueError('metadata_backup_purge_current_copy_requires_review')
    return record,current,digest


def _generation_matches(path,row):
    observed,bundle = _generation(path)
    expected = dict(zip(FIELDS,(row[column] for column in COLUMNS)))
    for field in ('model','seed'):
        if observed[field]=='' and expected[field]=='Unknown':
            observed[field]='Unknown'
    if all(expected[field]=='' for field in FIELDS[2:]):
        if bundle.has_prompts() or bundle.has_generation_params():
            raise ValueError('metadata_backup_purge_current_generation_changed')
        observed.update({field:'' for field in FIELDS[2:]})
    if observed!=expected:
        raise ValueError('metadata_backup_purge_current_generation_changed')


def _paths(service,journal,event):
    folder = _scope(service,Path(journal['path']).parent,event)
    backup = safe_path(journal['backup'],portable=True)
    current = safe_path(journal['output'],portable=True)
    if backup.parent!=folder or backup==current:
        raise ValueError('metadata_backup_purge_backup_scope_changed')
    for suffix in ('.txt','.xmp','.json'):
        if os.path.lexists(backup.with_suffix(suffix)) or os.path.lexists(Path(str(backup)+suffix)):
            raise ValueError('metadata_backup_purge_documented_backup')
    aliases = path_variants(backup,journal['backup'])
    if service.read('get_metadata_path_status',aliases,cancel_event=event)['occupied']:
        raise ValueError('metadata_backup_purge_backup_has_native_identity')
    scope = service.read('get_duplicate_retirement_roots',cancel_event=event)
    return folder,backup,current,aliases,scope


def preview(service,journal_path,*,expected_review_fingerprint,event=None):
    """Read-only, one explicit operation; ready evidence never reserves a purge."""
    if not isinstance(expected_review_fingerprint,str) or not SHA.fullmatch(expected_review_fingerprint):
        raise ValueError('metadata_backup_purge_invalid_review')
    _cancel(event)
    journal = _journal(journal_path)
    if journal['kind']!='metadata_edit' or journal['data'].get('request') is None:
        raise ValueError('metadata_backup_purge_original_edit_required')
    report = review(service.db_path,journal['path'],event=event)
    if (not report['stable'] or report['review_fingerprint']!=expected_review_fingerprint or
            report['journal_sha256']!=journal['sha256']):
        raise ValueError('metadata_backup_purge_review_changed')
    if not report['receipt_matches_request'] or not report['backup_matches_source']:
        raise ValueError('metadata_backup_purge_confirmed_history_and_backup_required')
    record,current,digest = _native(service,journal,event)
    row = current['state']['file']
    if (current['fingerprint']!=report['source']['fingerprint'] or
            report['files']['output'].get('sha256')!=digest):
        raise ValueError('metadata_backup_purge_current_copy_changed')
    _generation_matches(journal['output'],row)
    folder,backup,kept,aliases,scope = _paths(service,journal,event)
    kept_identity,backup_identity = _identity(kept.stat()),_identity(backup.stat())
    with PinnedFile(kept) as keeper, PinnedFile(backup,delete=True) as victim:
        keeper.verify(digest,kept_identity,event)
        victim.verify(journal['source_sha'],backup_identity,event)
        if kept_identity[:2]==backup_identity[:2]:
            raise ValueError('metadata_backup_purge_same_physical_file')
        if _native(service,journal,event)!=(record,current,digest):
            raise ValueError('metadata_backup_purge_native_changed')
        if _paths(service,journal,event)!=(folder,backup,kept,aliases,scope):
            raise ValueError('metadata_backup_purge_scope_changed')
        if _journal(journal['path'])['sha256']!=journal['sha256']:
            raise ValueError('metadata_backup_purge_journal_changed')
    _cancel(event)
    evidence = dict(version=1,operation_id=journal['data']['operation_id'],journal=journal['path'],
        journal_sha256=journal['sha256'],review_fingerprint=expected_review_fingerprint,
        backup=str(backup),backup_sha256=journal['source_sha'],backup_identity=backup_identity,
        current=str(kept),current_sha256=digest,current_identity=kept_identity,
        history_fingerprint=history_fingerprint(record),size=backup_identity[2],
        approval=dict(copy_id=row['id'],expected_fingerprint=current['fingerprint'],
            expected_path=row['path'],operation_id=journal['data']['operation_id'],
            expected_history_fingerprint=history_fingerprint(record),current_digest=digest,
            expected_scope=scope,backup_paths=aliases))
    if len(encode(evidence))>128*1024:
        raise ValueError('metadata_backup_purge_evidence_limit')
    return dict(evidence=evidence,preview_fingerprint=hashlib.sha256(encode(evidence)).hexdigest())


def _approve(service,approval,event):
    _cancel(event)
    future = service.submit('approve_metadata_backup_purge',**approval)
    while True:
        try:
            if future.result(timeout=.1) is not True:
                raise ValueError('metadata_backup_purge_approval_unconfirmed')
            return
        except TimeoutError:
            if future.done():
                raise
            if event is not None and event.is_set() and future.cancel():
                raise ValueError('metadata_backup_purge_cancelled')


def purge(service,journal_path,*,expected_review_fingerprint,expected_preview_fingerprint,
          confirmed=False,event=None):
    """Confirmation binds one fresh preview; deletes only its exclusive backup handle."""
    if confirmed is not True or not isinstance(expected_preview_fingerprint,str) or not SHA.fullmatch(expected_preview_fingerprint):
        raise ValueError('metadata_backup_purge_confirmation_required')
    _cancel(event)
    fresh = preview(service,journal_path,expected_review_fingerprint=expected_review_fingerprint,event=event)
    if fresh['preview_fingerprint']!=expected_preview_fingerprint:
        raise ValueError('metadata_backup_purge_preview_changed')
    evidence = fresh['evidence']
    journal = _journal(evidence['journal'])
    folder = _scope(service,Path(journal['path']).parent,event)
    result = dict(status='review_required',record=None,approved=False,delete_marked=False,deleted=False,error=None)
    operation = None
    with _guard(folder,expected_preview_fingerprint):
        try:
            with PinnedFile(evidence['current']) as keeper, PinnedFile(evidence['backup'],delete=True) as victim:
                keeper.verify(evidence['current_sha256'],evidence['current_identity'],event)
                victim.verify(evidence['backup_sha256'],evidence['backup_identity'],event)
                _paths(service,journal,event)
                target = folder/('backup-purge-'+str(uuid4())+'.json')
                result['record'] = str(target)
                operation = dict(version=1,phase='prepared',evidence=evidence,approved=False,
                                 delete_marked=False,deleted=False,error=None)
                _save(target,operation,create=True)
                operation['intent_record'] = str(target)
                _approve(service,evidence['approval'],event)
                result['approved'] = operation['approved'] = True
                operation['phase'] = 'approved'
                _scope(service,folder,None)
                approved_record = target.with_name(target.stem+'-approved.json')
                _save(approved_record,operation,create=True)
                result['record'] = str(approved_record)
                operation['approval_record'] = str(approved_record)
                # Accepted SQL approval drains. Cancellation can still prevent deletion.
                _cancel(event)
                keeper.verify(evidence['current_sha256'],evidence['current_identity'],event)
                victim.verify(evidence['backup_sha256'],evidence['backup_identity'],event)
                record,current,digest = _native(service,journal,event)
                if (history_fingerprint(record)!=evidence['history_fingerprint'] or
                        current['fingerprint']!=evidence['approval']['expected_fingerprint']):
                    raise ValueError('metadata_backup_purge_native_changed')
                if _paths(service,journal,event)[4]!=evidence['approval']['expected_scope']:
                    raise ValueError('metadata_backup_purge_scope_changed')
                if _journal(evidence['journal'])['sha256']!=evidence['journal_sha256']:
                    raise ValueError('metadata_backup_purge_journal_changed')
                _cancel(event)
                try:
                    victim.mark_delete()
                finally:
                    result['delete_marked'] = operation['delete_marked'] = victim.marked
                victim.close()
                if os.path.lexists(evidence['backup']) or not Path(evidence['backup']).parent.is_dir():
                    raise ValueError('metadata_backup_purge_deletion_unconfirmed')
                result['deleted'] = operation['deleted'] = True
                keeper.verify(evidence['current_sha256'],evidence['current_identity'])
                operation['phase'] = 'complete'
                _scope(service,folder,None)
                complete_record = target.with_name(target.stem+'-complete.json')
                _save(complete_record,operation,create=True)
                result['record'] = str(complete_record)
                result['status'] = 'complete'
        except Exception as error:
            # A failed final record never turns an actual deletion into a rollback claim.
            if result['delete_marked'] and not os.path.lexists(evidence['backup']) and Path(evidence['backup']).parent.is_dir():
                result['deleted'] = True
            result['error'] = str(error)[:1000]
            if operation is not None:
                operation.update(phase='review_required',approved=result['approved'],
                                 delete_marked=result['delete_marked'],deleted=result['deleted'],error=result['error'])
                try:
                    _scope(service,folder,None)
                    failure_record = target.with_name(target.stem+'-review-required.json')
                    _save(failure_record,operation,create=True)
                    result['record'] = str(failure_record)
                except Exception:
                    pass
    return result
