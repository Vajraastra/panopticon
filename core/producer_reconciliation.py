"""Explicit one-journal reconciliation; no image/backup/journal restoration.

A matching historical receipt is acknowledged without replay. A missing receipt
permits one Native registration/metadata commit only after a fresh reviewed CAS,
verified current bytes/metadata and unchanged source identity/curation. Evidence
is persisted separately before admission. SQLite/file races are not atomic.
"""
from contextlib import contextmanager
from concurrent.futures import TimeoutError
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from PIL import Image
from core.duplicate_snapshot import encode
from core.metadata import MetadataExtractor
from core.output_paths import safe_path,path_variants
from core.producer_journal_review import review,_journal,_path_matches,identity
from core.generation_history import FIELDS,SHA


def _cancel(event):
    if event is not None and event.is_set():
        raise ValueError('producer_reconciliation_cancelled')


def _scope(service,path,event):
    actual=safe_path(path,portable=True)
    for entry in service.read('get_duplicate_retirement_roots',cancel_event=event):
        _cancel(event)
        root=Path(entry)
        if not root.is_absolute():
            raise ValueError('producer_reconciliation_root_requires_review')
        root=root.resolve(strict=False)
        if root.drive.casefold()!=actual.drive.casefold():
            continue
        try:
            within=os.path.commonpath((str(actual).casefold(),str(root).casefold()))==str(root).casefold()
        except ValueError:
            within=False
        if within:
            raise ValueError('producer_reconciliation_inside_library')
    return actual


@contextmanager
def _guard(folder,token):
    path=folder/'producer-reconcile.lock'
    try:
        with path.open('xb') as stream:
            owned=os.fstat(stream.fileno())
            stream.write(encode(dict(version=1,nonce=str(uuid4()),review_fingerprint=token)))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise ValueError('producer_reconciliation_lock_requires_review') from error
    try:
        yield
    finally:
        try:
            safe_path(path,portable=True)
            current=path.stat(follow_symlinks=False)
            if (current.st_dev,current.st_ino)==(owned.st_dev,owned.st_ino):
                path.unlink()
        except (OSError,ValueError):
            pass  # An inaccessible/replaced guard is left for explicit review.


def _save(path,data,*,create=False):
    raw=encode(data)
    if len(raw)>128*1024:
        raise ValueError('producer_reconciliation_record_limit')
    safe_path(path,exists=os.path.lexists(path),portable=True)
    fd,name=tempfile.mkstemp(prefix='.reconcile-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            if os.name=='nt':
                os.rename(name,path)
            else:
                os.link(name,path)
        else:
            os.replace(name,path)
    finally:
        if os.path.lexists(name):
            os.unlink(name)


def _generation(path):
    path=safe_path(path)
    before=identity(path)
    bundle=MetadataExtractor.extract(path,strict=True)
    with Image.open(path) as image:
        if max(image.size)>32768 or image.width*image.height>20_000_000 or getattr(image,'n_frames',1)!=1:
            raise ValueError('producer_reconciliation_image_limit')
        dimensions=image.size
        image.load()
    if identity(path)!=before:
        raise ValueError('producer_reconciliation_file_changed')
    return dict(width=dimensions[0],height=dimensions[1],tool=bundle.tool,model=bundle.model,
        positive=bundle.positive_prompt,negative=bundle.negative_prompt,seed=str(bundle.seed)),bundle


def _metadata(journal):
    request=journal['data']['request']
    observed,bundle=_generation(journal['output'])
    expected=request['generation']
    if journal['kind']=='metadata_edit':
        # UniversalParser's legacy absence labels differ from MetadataBundle.
        # Preserve the original request, accepting only absence -> Unknown.
        for field in ('model','seed'):
            if observed[field]=='' and expected[field]=='Unknown':
                observed[field]='Unknown'
    if journal['kind']=='metadata_edit' and all(expected[field]=='' for field in FIELDS[2:]):
        if bundle.has_prompts() or bundle.has_generation_params():
            raise ValueError('producer_reconciliation_generation_requires_review')
        observed.update({field:'' for field in FIELDS[2:]})
    if observed!=expected:
        raise ValueError('producer_reconciliation_generation_requires_review')
    if journal['kind']=='metadata_edit':
        _,previous=_generation(journal['backup'])
        values=dict(tool=previous.tool,model=previous.model,positive=previous.positive_prompt,
            negative=previous.negative_prompt,seed=str(previous.seed),steps=str(previous.steps),
            cfg=str(previous.cfg),sampler=previous.sampler,vae=previous.vae,
            loras=json.dumps(previous.loras,ensure_ascii=False))
        absence_fields={'model','seed','steps','cfg','sampler','vae'}
        if any(values[key]!=value and not (key in absence_fields and values[key]=='' and value=='Unknown')
               for key,value in request['source_generation'].items()):
            raise ValueError('producer_reconciliation_previous_generation_requires_review')
    info=Path(journal['output']).stat()
    observation=request['observation']
    if observation['size']!=info.st_size or observation['created']!=info.st_ctime:
        raise ValueError('producer_reconciliation_observation_requires_review')
    if journal['kind']=='derived':
        current={os.path.normpath(value).replace('\\','/').casefold() for value in
                 path_variants(safe_path(journal['output']),request['path'])}
        recorded={os.path.normpath(value).replace('\\','/').casefold() for value in request['destination_paths']}
        if current!=recorded:
            raise ValueError('producer_reconciliation_aliases_require_review')


def _receipt(service,journal):
    operation='get_derived_copy_receipt' if journal['kind']=='derived' else 'get_metadata_edit_receipt'
    value=service.read(operation,journal['data']['operation_id'])['receipt']
    if value is None or value['request_fingerprint']!=journal['request_fingerprint'] or (
            value.get('source_copy_id') if journal['kind']=='derived' else value['copy_id'])!=journal['data']['copy_id'] or (
            value['source_sha256']!=journal['source_sha'] or value['result_sha256']!=journal['result_sha']):
        raise ValueError('producer_reconciliation_receipt_unconfirmed')
    if journal['kind']=='derived' and (not _path_matches(value['source_path'],journal['source']) or
            not _path_matches(value['result_path'],journal['output']) or
            value['producer']!=journal['data']['request']['producer']):
        raise ValueError('producer_reconciliation_receipt_unconfirmed')
    return value


def _write(service,operation,copy_id,request,event):
    _cancel(event)
    future=service.submit(operation,copy_id,**request)
    while True:
        try:
            result=future.result(timeout=.1)
            if result is not True:
                raise ValueError('producer_reconciliation_write_unconfirmed')
            return
        except TimeoutError:
            if future.done():
                raise
            if event is not None and event.is_set() and future.cancel():
                raise ValueError('producer_reconciliation_cancelled')


def reconcile(service,journal_path,*,expected_review_fingerprint,event=None):
    """One explicit reviewed action; admitted Native work drains, no auto-retry."""
    if not isinstance(expected_review_fingerprint,str) or not SHA.fullmatch(expected_review_fingerprint):
        raise ValueError('producer_reconciliation_invalid_review')
    _cancel(event)
    journal=_journal(journal_path)
    folder=_scope(service,Path(journal['path']).parent,event)
    with _guard(folder,expected_review_fingerprint):
        observed=review(service.db_path,journal['path'],event=event)
        if (not observed['stable'] or observed['review_fingerprint']!=expected_review_fingerprint or
                observed['journal_sha256']!=journal['sha256']):
            raise ValueError('producer_reconciliation_changed_since_review')
        if observed['receipt_matches_request']:
            action='acknowledge_historical_receipt'
        elif journal['kind']=='metadata_export' and observed['status']=='export_bytes_verified':
            action='record_export_review'
        else:
            if (observed['receipt']['receipt'] is not None or not observed['receipt']['prepared'] or
                    observed['status']!='published_bytes_without_receipt' or not observed['source_revision_matches'] or
                    observed['source'] is None or observed['source'].get('missing') or
                    observed['source']['is_offline']!=0 or not _path_matches(observed['source']['path'],journal['source'])):
                raise ValueError('producer_reconciliation_requires_manual_review')
            if journal['kind']=='derived':
                if not observed['source_bytes_match'] or observed['output_path_occupied']:
                    raise ValueError('producer_reconciliation_requires_manual_review')
                action='register_derived_copy'
            else:
                if not observed['backup_matches_source']:
                    raise ValueError('producer_reconciliation_requires_manual_review')
                action='commit_metadata_edit'
            _metadata(journal)
            fresh=review(service.db_path,journal['path'],event=event)
            if not fresh['stable'] or fresh['review_fingerprint']!=expected_review_fingerprint:
                raise ValueError('producer_reconciliation_changed_since_review')
        _cancel(event)
        _scope(service,folder,event)
        record_path=folder/('reconciliation-'+str(uuid4())+'.json')
        record=dict(version=1,operation_id=journal['data']['operation_id'],action=action,
            phase='prepared',native_confirmed=False,review=observed,receipt=None)
        _save(record_path,record,create=True)  # Durable intent before any admitted database write.
        confirmed=False
        try:
            if action in ('register_derived_copy','commit_metadata_edit'):
                latest=review(service.db_path,journal['path'],event=event)
                if not latest['stable'] or latest['review_fingerprint']!=expected_review_fingerprint:
                    raise ValueError('producer_reconciliation_changed_since_review')
                try:
                    _write(service,action,journal['data']['copy_id'],journal['data']['request'],event)
                except Exception as write_error:
                    try:
                        receipt=_receipt(service,journal)
                    except Exception:
                        raise write_error
                else:
                    receipt=_receipt(service,journal)
                confirmed=True
            elif action=='acknowledge_historical_receipt':
                receipt=_receipt(service,journal)
                confirmed=True
            else:
                receipt=None
            # No cancellation after admission: preserve its historical confirmation.
            final=review(service.db_path,journal['path'])
            record.update(phase='complete',native_confirmed=confirmed,receipt=receipt,final_review=final)
            _scope(service,folder,None)
            _save(record_path,record)
            return dict(status='complete',action=action,native_confirmed=confirmed,
                        record=str(record_path),current_status=final['status'])
        except Exception as error:
            record.update(phase='review_required',native_confirmed=confirmed,error=str(error)[:1000])
            try:
                _scope(service,folder,None)
                _save(record_path,record)
            except Exception:
                pass  # Last durable intent and producer journal remain for review.
            return dict(status='review_required',action=action,native_confirmed=confirmed,
                        record=str(record_path),error=str(error)[:1000])
