"""Reconciliación observada de un diario explícito; nunca modifica DB ni archivos.

La fase escrita no prueba un commit o rename. Contrastar IDs/estado completo y bytes
en ambas ubicaciones; un reporte no constituye permiso de restauración/purga.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading

from core.curation_apply import _check_cancel, _identity
from core.duplicate_policy import DuplicatePolicy, SHA
from core.duplicate_snapshot import encode
from .exact_review import _probe
from .file_guards import plain_chain, documented

MAX_JOURNAL = 5*1024*1024
MAX_REPORT = 2*1024*1024


def _path(value):
    if (not isinstance(value,str) or not value or len(value)>4096 or '\x00' in value or
            not Path(value).is_absolute() or '..' in Path(value).parts or Path(value).as_posix()!=value):
        raise ValueError('invalid_duplicate_recovery_journal')
    return Path(value)


def _unique(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('invalid_duplicate_recovery_journal')
        value[key]=item
    return value


def _raw(path,event):
    _check_cancel(event)
    plain_chain(path)
    with path.open('rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_JOURNAL:
            raise ValueError('duplicate_recovery_journal_limit')
        before=_identity(info)
        raw=stream.read(MAX_JOURNAL+1)
        if len(raw)>MAX_JOURNAL:raise ValueError('duplicate_recovery_journal_limit')
        if _identity(os.fstat(stream.fileno()))!=before or _identity(path.stat())!=before:
            raise ValueError('duplicate_recovery_journal_changed')
    _check_cancel(event)
    return raw,before


def read_journal(name,event=None):
    if os.name!='nt':raise ValueError('duplicate_retirement_requires_windows')
    path=Path(name)
    if (not isinstance(name,str) or len(name)>4096 or not path.is_absolute() or
            path.name!='journal.json' or not re.fullmatch('[0-9a-f]{32}',path.parent.name)):
        raise ValueError('invalid_duplicate_recovery_journal')
    raw,identity=_raw(path,event)
    try:
        value=json.loads(raw,object_pairs_hook=_unique,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError,UnicodeError,RecursionError) as exc:
        raise ValueError('invalid_duplicate_recovery_journal') from exc
    keys={'format','version','phase','digest','folder','keep','approved','scope','copies','proofs','policy','moves','error'}
    if (not isinstance(value,dict) or set(value)-{'review_scope'}!=keys or
            value['format']!='panopticon.duplicate-retirement' or type(value['version']) is not int or value['version']!=1 or
            value['phase'] not in ('prepared','rejected','archived','moving','partial','complete') or
            value.get('review_scope','folder') not in ('folder','database') or
            not isinstance(value['digest'],str) or not SHA.fullmatch(value['digest'])):
        raise ValueError('invalid_duplicate_recovery_journal')
    _path(value['folder'])
    if not isinstance(value['policy'],dict):raise ValueError('invalid_duplicate_recovery_journal')
    DuplicatePolicy(**value['policy'])
    copies,approved,moves,proofs=value['copies'],value['approved'],value['moves'],value['proofs']
    if (not isinstance(copies,list) or not 2<=len(copies)<=256 or not isinstance(approved,list) or len(approved)!=len(copies) or
            not isinstance(moves,list) or not 1<=len(moves)<len(copies) or not isinstance(proofs,dict)):
        raise ValueError('invalid_duplicate_recovery_journal')
    paths,ids=set(),set()
    for copy,admission in zip(copies,approved):
        if not isinstance(copy,dict) or set(copy)!={'path','state','fingerprint'} or not isinstance(admission,dict) or set(admission)!={'path','copy_id','fingerprint'}:
            raise ValueError('invalid_duplicate_recovery_journal')
        source=_path(copy['path']).as_posix()
        state=copy['state']
        if (not isinstance(state,dict) or set(state)!={'file','tags'} or not isinstance(state['file'],dict) or
                not isinstance(state['tags'],list) or len(state['tags'])>256 or state['file'].get('path')!=source or
                type(state['file'].get('is_offline')) is not int or state['file']['is_offline']!=0):
            raise ValueError('invalid_duplicate_recovery_journal')
        key=state['file'].get('id')
        fingerprint=hashlib.sha256(encode(state)).hexdigest()
        if (type(key) is not int or not -2**63<=key<=2**63-1 or key in ids or source.casefold() in paths or
                copy['fingerprint']!=fingerprint or admission!=dict(path=source,copy_id=key,fingerprint=fingerprint)):
            raise ValueError('invalid_duplicate_recovery_journal')
        proof=proofs.get(source)
        if (not isinstance(proof,dict) or set(proof)!={'digest','identity','size'} or proof['digest']!=value['digest'] or
                not isinstance(proof['identity'],list) or len(proof['identity'])!=5 or
                any(type(part) is not int and part is not None for part in proof['identity']) or
                type(proof['size']) is not int or proof['size']<0 or proof['size']!=proof['identity'][2]):
            raise ValueError('invalid_duplicate_recovery_journal')
        ids.add(key);paths.add(source.casefold())
    exact_paths={copy['path'] for copy in copies}
    if set(proofs)!=exact_paths or not isinstance(value['keep'],list) or not value['keep'] or len(set(value['keep']))!=len(value['keep']) or not set(value['keep'])<=exact_paths:
        raise ValueError('invalid_duplicate_recovery_journal')
    if not isinstance(value['scope'],list) or len(value['scope'])>256:
        raise ValueError('invalid_duplicate_recovery_journal')
    for scope in value['scope']:_path(scope)
    selected=set()
    for index,move in enumerate(moves,1):
        if not isinstance(move,dict) or set(move)!={'source','destination','status'}:
            raise ValueError('invalid_duplicate_recovery_journal')
        source=_path(move['source'])
        destination=_path(move['destination'])
        expected=path.parent/f'{index:06d}{source.suffix}'
        if (source.as_posix() not in exact_paths or source.as_posix() in selected or source.as_posix() in value['keep'] or
                destination!=expected or move['status'] not in ('pending','moved')):
            raise ValueError('invalid_duplicate_recovery_journal')
        selected.add(source.as_posix())
    return path,value,hashlib.sha256(raw).hexdigest(),identity


def _physical(name,proof,event):
    _check_cancel(event)
    path=Path(name)
    try:
        plain_chain(path)
        info=path.stat()
        if not stat.S_ISREG(info.st_mode):return dict(status='unavailable',reason='duplicate_recovery_not_regular')
    except FileNotFoundError:
        # Ausencia sólo si el padre es accesible. Un disco desconectado no prueba pérdida.
        try:
            parent=path.parent.stat()
            if stat.S_ISDIR(parent.st_mode):return dict(status='absent',reason=None)
        except OSError:pass
        return dict(status='unavailable',reason='duplicate_recovery_parent_unavailable')
    except (OSError,ValueError):return dict(status='unavailable',reason='duplicate_recovery_path_unavailable')
    observed,error=_probe(name,event)
    if error is not None:return dict(status='unavailable',reason='duplicate_recovery_path_unavailable')
    plain_chain(path)
    return dict(status='matches' if observed['digest']==proof['digest'] else 'changed',
        same_identity=tuple(observed['identity'])==tuple(proof['identity']),
        identity=observed['identity'],digest=observed['digest'],reason=None)


class RecoveryReview:
    def __init__(self,service,journal):
        self.service=service
        self.journal=journal

    def run(self,event=None,progress=None):
        event=threading.Event() if event is None else event
        result=dict(journal=self.journal,phase=None,journal_fingerprint=None,copies=[],
            moves=[],complete=False,cancelled=False,error=None,review_required=True,verified_retired=0)
        try:
            path,journal,fingerprint,identity=read_journal(self.journal,event)
            result.update(phase=journal['phase'],journal_fingerprint=fingerprint)
            copies=journal['copies']
            ids=[copy['state']['file']['id'] for copy in copies]
            current=self.service.read('get_duplicate_recovery_state',ids,cancel_event=event)
            if (not isinstance(current,list) or len(current)!=len(ids) or
                    [copy.get('copy_id') for copy in current]!=ids):
                raise ValueError('duplicate_recovery_native_incomplete')
            selected={move['source'] for move in journal['moves']}
            locations=[copy['path'] for copy in copies]+[move['destination'] for move in journal['moves']]
            proofs={**journal['proofs'],**{move['destination']:journal['proofs'][move['source']] for move in journal['moves']}}
            physical={}
            with ThreadPoolExecutor(max_workers=4,thread_name_prefix='duplicate-recovery') as pool:
                # A lo sumo 64 futuros; no materializar un lote grande de trabajos.
                for start in range(0,len(locations),64):
                    _check_cancel(event)
                    block=locations[start:start+64]
                    jobs=[pool.submit(_physical,name,proofs[name],event) for name in block]
                    for name,job in zip(block,jobs):
                        physical[name]=job.result()
                        if progress:progress(len(physical),len(locations),name)
            by_source={}
            keeper_ok=True
            for before,now in zip(copies,current):
                name=before['path'];original=before['state']
                offline=deepcopy(original);offline['file']['is_offline']=1
                state=now['state']
                native=('missing' if state is None else 'active_unchanged' if state==original else
                    'offline_unchanged' if state==offline else 'changed')
                occupied=now['active_copy_id'] is not None and now['active_copy_id']!=now['copy_id']
                document=documented(Path(name))
                copy=dict(copy_id=now['copy_id'],source=name,native=native,active_copy_id=now['active_copy_id'],
                    occupied=occupied,documented=document,physical=physical[name],fingerprint=now['fingerprint'])
                by_source[name]=copy
                result['copies'].append(copy)
                if name not in selected and (native!='active_unchanged' or occupied or document or
                        physical[name]['status']!='matches' or not physical[name]['same_identity']):keeper_ok=False
            for move in journal['moves']:
                copy=by_source[move['source']]
                source,destination=physical[move['source']],physical[move['destination']]
                observed='requires_review'
                safe=keeper_ok and not copy['occupied'] and not copy['documented']
                if safe and copy['native']=='offline_unchanged':
                    if source['status']=='absent' and destination['status']=='matches' and destination['same_identity']:
                        observed='retired_verified'
                    elif source['status']=='matches' and source['same_identity'] and destination['status']=='absent':
                        observed='archived_source_present'
                elif safe and copy['native']=='active_unchanged' and source['status']=='matches' and source['same_identity'] and destination['status']=='absent':
                    observed='not_retired'
                result['moves'].append(dict(copy_id=copy['copy_id'],source=move['source'],destination=move['destination'],
                    recorded=move['status'],observed=observed,destination_state=destination))
            _check_cancel(event)
            raw,last_identity=_raw(path,event)
            if last_identity!=identity or hashlib.sha256(raw).hexdigest()!=fingerprint:
                raise ValueError('duplicate_recovery_journal_changed')
            result['verified_retired']=sum(move['observed']=='retired_verified' for move in result['moves'])
            result['review_required']=any(move['observed']!='retired_verified' for move in result['moves'])
            result['complete']=True
            if len(encode(result))>MAX_REPORT:raise ValueError('duplicate_recovery_report_limit')
        except Exception as exc:
            result.update(copies=[],moves=[],complete=False,verified_retired=0,review_required=True,
                cancelled=event.is_set(),error=None if event.is_set() else str(getattr(exc,'reason',exc))[:256])
        return result
