"""Native Cherry maintenance worker: fresh phase plans and retained file handles.

SQL belongs to the Native backend; catalogs/extraction belong to Cherry. Scope
and source handles span physical publication and durable Native acknowledgements.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import threading
from typing import Protocol
from uuid import uuid4

from core.pinned_move import DirectoryPins, MoveJournal, MovableFileContext, PendingMove, local_move_path, api, identity128
from core.library_maintenance_operations import plan_hash
from core.path_identity import local_path, observe_path
from core.pinned_file import PinnedFile
from core.output_paths import path_variants

SHA = re.compile('[0-9a-f]{64}')


class Backend(Protocol):
    owner_nonce: str
    def read(self, command, *args, **kwargs): ...
    def write(self, command, *args, **kwargs): ...


def batches(values):
    for start in range(0, len(values), 100): yield values[start:start+100]


def normalized(value): return local_path(str(value))


def root_identity(record):
    raw = bytes.fromhex(record['file_id'])
    return [record['volume'], int.from_bytes(raw[:8], 'little'), int.from_bytes(raw[8:], 'little')]


def physical(pin):
    # This observations API opens attributes only. The worker's source handle
    # remains open, denying writers and rename until the Native acknowledgement.
    value = observe_path(str(pin.path))
    if value.status != 'resolved' or value.kind != 'file': raise ValueError('physical_observation_unavailable')
    return dict(canonical=value.canonical, physical=list(value.physical))


def read_roster(pin):
    """Buffered JSONL over an unbuffered pinned handle, without taking ownership."""
    pin.stream.seek(0)
    buffered = io.BufferedReader(pin.stream, buffer_size=1024*1024)
    try: return [json.loads(line) for line in buffered if line.strip()]
    finally: buffered.detach()


class RecoveryRequired(RuntimeError):
    def __init__(self, session, cause):
        self.session = session
        super().__init__('maintenance_requires_fresh_recovery: '+str(cause))


class NativePhaseSession:
    """Composable callback actor; profile_guard is mandatory and supplied by host.

    A lock unknown to download/fusion producers is insufficient. Production must
    pass their shared admission/exclusion guard, not merely create a lock file.
    """
    def __init__(self, root, profile_id, backend: Backend, *, profile_guard, cancel=None):
        if profile_guard is None: raise ValueError('shared_profile_guard_required')
        self.root = local_move_path(root, existing=True)
        self.profile_id, self.backend, self.profile_guard = profile_id, backend, profile_guard
        self.cancel = cancel or threading.Event()
        self.stack = ExitStack(); self.pins = None; self.phase = None
        self.active = False; self.failed = False; self.completed = []
        self.source_context=None
        self.error_operation_id=None

    def __enter__(self):
        try:
            self.scope_ticket=self.stack.enter_context(self.profile_guard)
            self.pins = self.stack.enter_context(DirectoryPins())
            self.root_record = self.pins.pin(self.root)
            self.root_id = root_identity(self.root_record)
            self.active = True
            return self
        except BaseException:
            self.stack.close(); raise

    def _write(self, command, *args):
        if self.backend.write(command, *args) is not True: raise ValueError('native_command_unconfirmed:'+command)

    def _claim(self):
        if hasattr(self.scope_ticket,'assert_current'): self.scope_ticket.assert_current()
        record = self.pins.pin(self.root)
        if root_identity(record) != self.root_id: raise ValueError('root_identity_changed')
        return dict(canonical=normalized(record['path']),physical=self.root_id,kind='directory',
                    handles_held=self.pins.active,owner_nonce=self.backend.owner_nonce)

    def _cancel(self):
        if self.cancel.is_set(): raise RuntimeError('maintenance_cancelled')

    def check_cancel(self):self._cancel()

    def begin_phase(self):
        if self.phase:raise ValueError('previous_native_phase_unsettled')
        self.error_operation_id=None;self.source_context=None;self._cancel()

    async def reserved_numbers(self,artist,layout_v2,physical_paths):
        from .counter_highwater import namespace_counter
        sources={normalized(value).casefold() for value in physical_paths};reserved={}
        for name in self._source_context()['names']:
            self._cancel()
            if name in sources:continue
            parsed=namespace_counter(artist,name)
            if parsed is not None:
                namespace,number=parsed
                reserved.setdefault(namespace if layout_v2 else 'legacy',set()).add(number)
        return reserved

    async def prepare_extraction_counters(self,artist):
        from .counter_highwater import namespace_counter
        from third_party.cherry_dl import catalog
        from third_party.cherry_dl.file_routing import LAYOUT_VERSION
        context=self._source_context();minima={}
        layout_v2=(await catalog.get_meta_int(self.root,'layout_version') or 0)>=LAYOUT_VERSION
        for name in context['names']:
            self._cancel();parsed=namespace_counter(artist,name)
            if parsed is not None:
                namespace,number=parsed;key=namespace if layout_v2 else 'legacy'
                minima[key]=max(minima.get(key,0),number)
        if self._revision()!=context['revision']:raise ValueError('phase_source_snapshot_changed')
        self._cancel()
        if minima:await catalog.ensure_counter_at_least(self.root,minima)

    def _path(self, relative):
        target = self.root / relative
        local_move_path(target, existing=False)
        if not target.is_relative_to(self.root): raise ValueError('path_outside_profile')
        return normalized(target)

    def _revision(self):
        value=tuple(self.backend.service.revision_snapshot())
        if len(value)!=3 or value[2] is not True: raise ValueError('source_snapshot_owner_unavailable')
        return value

    def _source_context(self):
        baseline=self._revision()
        if self.source_context is not None:
            if self.source_context['revision']!=baseline: raise ValueError('phase_source_snapshot_changed')
            return self.source_context
        roots=sorted({normalized(Path(value).parent) for value in
                      path_variants(self.root/'__native_phase_alias__',
                                    Path(getattr(self.scope_ticket,'folder',self.root))/'__native_phase_alias__')})
        names={};cursor=0;snapshot=None
        while True:
            self._cancel()
            if self._revision()!=baseline:raise ValueError('phase_source_snapshot_changed')
            page=self.backend.read('get_source_names_page',roots,after_id=cursor,snapshot=snapshot)
            if self._revision()!=baseline:raise ValueError('phase_source_snapshot_changed')
            snapshot=page['snapshot']
            for record in page['records']:
                for name in record['paths']:
                    names.setdefault(name.casefold(),set()).add(record['id'])
                    for root in roots:
                        prefix=root.rstrip('/')+'/'
                        if name.casefold().startswith(prefix.casefold()):
                            canonical=normalized(self.root)+'/'+name[len(prefix):]
                            names.setdefault(canonical.casefold(),set()).add(record['id'])
            if not page['done'] and page['after_id']<=cursor:raise ValueError('source_snapshot_cursor_not_advancing')
            cursor=page['after_id']
            if page['done']:break
        self.source_context=dict(names=names,states={},revision=baseline,roots=roots)
        return self.source_context

    def _snapshots(self, paths):
        context=self._source_context(); wanted=set();matches={}
        for name in paths:
            variants={name}
            if Path(name).parent.is_dir():variants.update(normalized(p) for p in path_variants(Path(name),Path(name)))
            if Path(name).is_file():
                short=api().GetShortPathNameW;short.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint32];short.restype=ctypes.c_uint32
                buffer=ctypes.create_unicode_buffer(32768);length=short(name,buffer,len(buffer))
                if not 0<length<len(buffer):raise ValueError('source_short_name_requires_review')
                variants.add(normalized(buffer.value))
            ids=set()
            for variant in variants:ids.update(context['names'].get(variant.casefold(),set()))
            matches[name.casefold()]=ids;wanted.update(ids)
        missing=sorted(wanted-set(context['states']))
        for batch in batches(missing):
            for value in self.backend.read('get_maintenance_copy_states',batch)['records']:
                context['states'][value['state']['file']['id']]=value
        if self._revision()!=context['revision']:raise ValueError('phase_source_snapshot_changed')
        return {name.casefold():{key:context['states'][key] for key in matches[name.casefold()]}
                for name in paths}

    async def plan_observation(self,path,candidates):
        """No byte read: validate exact CAT row against fresh Native current state."""
        self._cancel()
        source=normalized(local_move_path(path,existing=True))
        native=self._binding(self._snapshots([source]),source)
        if native is None:
            if len(candidates)!=1: raise ValueError('uncataloged_source_requires_review')
            return None
        if len(candidates)!=1: raise ValueError('catalog_path_ambiguous')
        catalog_digest,digest=self._catalog_digest(dict(hash=candidates[0]['hash']),native)
        size=Path(source).stat().st_size
        if native['state']['file']['file_size']!=size: raise ValueError('native_source_size_disagrees')
        return dict(digest=digest,catalog_hash=catalog_digest,size=size,catalog_size=candidates[0].get('file_size'))

    @staticmethod
    def _binding(found, source):
        values = list(found.get(source.casefold(), {}).values())
        active = [value for value in values if not value['state']['file']['is_offline']]
        if len(active) > 1: raise ValueError('source_active_identity_ambiguous')
        if active and active[0]['state']['file']['path'] != source: raise ValueError('source_alias_requires_review')
        if not active and values: raise ValueError('history_only_source_requires_review')
        return active[0] if active else None

    @staticmethod
    def _catalog_digest(item, native):
        catalog_digest = item.get('catalog_hash', item['hash'])
        if type(catalog_digest) is not str or SHA.fullmatch(catalog_digest.lower()) is None:
            raise ValueError('catalog_digest_invalid')
        catalog_digest = catalog_digest.lower()
        if native is None: return catalog_digest, catalog_digest
        row = native['state']['file']
        known = {value.lower() for value in (row['hash_original'], row['hash_mod']) if type(value) is str}
        actual = row['hash_mod'] or row['hash_original']
        if type(actual) is not str or SHA.fullmatch(actual.lower()) is None or catalog_digest not in known:
            raise ValueError('native_catalog_digest_disagrees')
        # Preserve the catalog's original key even when the physical bytes were
        # stamped by Metadata Hub. File moves verify Native.current, never ORIG.
        return catalog_digest, actual.lower()

    def preview(self, plan):
        """SQL/stat comparison only; never hashes, allocates scopes or mutates sources."""
        if plan.get('errors') or plan.get('duplicates'): raise ValueError('native_duplicates_or_errors_require_review')
        if plan.get('status', 'planned') != 'planned': raise ValueError('historical_engine_plan_requires_recovery')
        paths = [self._path(item[name]) for item in plan['files'] for name in ('old','new')]
        found = self._snapshots(paths)
        result=[]; seen=set(); source_names={self._path(item['old']).casefold() for item in plan['files']}
        for item in plan['files']:
            self._cancel()
            source,target = (self._path(item[name]) for name in ('old','new'))
            if target.casefold() in seen: raise ValueError('duplicate_phase_target')
            seen.add(target.casefold())
            native = self._binding(found, source)
            catalog_digest,digest = self._catalog_digest(item, native)
            if native is not None and native['state']['file']['file_size'] != item['size']:
                raise ValueError('native_source_size_disagrees')
            if target.casefold() not in source_names and found.get(target.casefold()):
                raise ValueError('destination_native_history_requires_review')
            if Path(source).stat().st_size != item['size']: raise ValueError('physical_source_size_changed')
            result.append(dict(item=item,source=source,target=target,native=native,
                               digest=digest,catalog_digest=catalog_digest))
        return result

    async def before_apply(self, plan, phase='compact'):
        if not self.active or self.phase or self.failed: raise ValueError('session_not_ready_for_phase')
        self._cancel()
        reviewed = self.preview(plan)
        opid = str(uuid4()); phase_dir = self.root/'.cherry-maintenance'/opid
        staging = phase_dir/'staging'; staging.mkdir(parents=True, exist_ok=False)
        journal = MoveJournal(phase_dir/'journal.jsonl')
        current = dict(operation_id=opid,label=phase,plan=plan,entries=[],operators={},journal=journal,reserved=False)
        self.phase=current
        try:
            # The complete phase is reviewed/pinned before any physical rename.
            for number,value in enumerate(reviewed):
                self._cancel()
                item,source,target = value['item'],value['source'],value['target']
                step_id=f'{number:06d}'; native=value['native']
                moving=source!=target
                self.pins.pin(Path(source).parent)
                pin = (MovableFileContext(source,value['digest'],directories=self.pins,cancel_event=self.cancel) if moving
                       else PinnedFile(source))
                pin.__enter__(); current['operators'][step_id]=pin
                identity=physical(pin)
                volume,file_id=identity128(api(),pin.handle)
                if identity['canonical'] != source: raise ValueError('source_canonical_requires_review')
                # Unchanged files compare Native/CAT/current stat; their bytes
                # are not reread merely to preview or compact a different file.
                if not moving and native is None: pin.verify(value['digest'],pin.check(),event=self.cancel)
                if pin.check()[2] != item['size']: raise ValueError('source_size_changed')
                entry=dict(step_id=step_id,kind=('relocate' if moving else 'unchanged') if native else 'admit',
                    target_path=target,digest=value['digest'],size=item['size'],catalog_digest=value['catalog_digest'],
                    source_file_id128=dict(volume=volume,file_id=file_id),digest_verified=bool(moving or native is None),
                    phase_kind=phase,catalog_counter=item.get('counter',0),catalog_counters=plan.get('counters',{}),
                    catalog_audit=plan.get('audit_version',0),archive_origin=item.get('archive_origin'),
                    archive_sha=plan.get('archive_sha'),archive_original_size=item.get('archive_original_size'),
                    source_native_original=None if native is None else native['state']['file']['hash_original'],
                    catalog_size=item.get('catalog_size',item['size']))
                entry.update(source_path=source,source_identity=identity)
                if native:
                    entry.update(copy_id=native['state']['file']['id'],source_fingerprint=native['fingerprint'])
                if moving:
                    entry['staging_path']=normalized(staging/(step_id+Path(source).suffix))
                item.update(_native_step_id=step_id,hash=value['digest'],catalog_hash=value['catalog_digest'])
                if moving: item['native_staging']=entry['staging_path']
                current['entries'].append(entry)
            entries=current['entries']; journal.record('phase_plan',operation_id=opid,label=phase,
                root_identity=self.root_id,plan=entries,plan_sha=plan_hash(entries))
            plan['native_operation_id']=opid
            self._write('maintenance_reserve',opid,self.profile_id,normalized(self.root),self.root_id,
                [dict(path=name,root_identity=self.root_id) for name in self._source_context()['roots']],plan_hash(entries),len(entries))
            current['reserved']=True
            self.error_operation_id=opid
            self._write('maintenance_bind_scope_owner',opid,self._claim())
            for batch in batches(list(enumerate(entries))): self._write('maintenance_plan_batch',opid,batch,self.root_id)
            self._write('maintenance_activate',opid,self.root_id)
            admissions=[entry['step_id'] for entry in entries if entry['kind']=='admit']
            for batch in batches(admissions): self._write('maintenance_prepare_admissions',opid,batch,self.root_id)
            for entry in entries:
                if entry['kind']=='admit' and normalized(current['operators'][entry['step_id']].path)==entry['target_path']:
                    self._admit(entry,current['operators'][entry['step_id']])
        except BaseException as exc:
            self._fail(exc); raise

    def _entry(self,item):
        if not self.phase: raise ValueError('no_active_phase')
        return next(entry for entry in self.phase['entries'] if entry['step_id']==item['_native_step_id'])

    def _receipt(self, entry, leg):
        value=self.backend.read('get_maintenance_receipt',self.phase['operation_id'],entry['step_id'],leg)['receipt']
        if value is None: raise ValueError('native_receipt_missing')
        return value

    def _admit(self,entry,pin):
        observation=observe_path(str(pin.path)); identity=physical(pin)
        if observation.status!='resolved' or identity['canonical']!=entry['target_path']:
            raise ValueError('admission_observation_changed')
        data=dict(path=entry['target_path'],filename=Path(entry['target_path']).name,size=entry['size'],
            created=os.fstat(pin.stream.fileno()).st_birthtime,hash_original=entry['digest'])
        proof=self.backend.read('get_path_identity_ingest_state',[observation.canonical],[observation.path],
                                physicals=[observation.physical])
        if proof['coverage']['prepared']:
            if proof['matches'] or proof['historical'] or proof['coverage']['pending'] or proof['coverage']['unresolved']:
                raise ValueError('admission_existing_identity_requires_review')
            record=dict(data=data,identity=asdict(observation),clock=proof['clock'],target=None,historical=False)
        else: record=dict(data=data,identity=None,clock=None,target=None,historical=False)
        request=dict(step_id=entry['step_id'],record=record,proof=dict(digest=entry['digest'],identity=identity))
        self._write('maintenance_admit_descendants',self.phase['operation_id'],[request],self.root_id)
        receipt=self._receipt(entry,'admit'); entry['copy_id']=receipt['copy_id']
        return dict(confirmed=True,step_id=entry['step_id'],receipt=receipt)

    async def move_file(self,source,target,item,*,intent_exists=False):
        self._cancel(); entry=self._entry(item); pin=self.phase['operators'][entry['step_id']]
        if normalized(source)!=normalized(pin.path): raise ValueError('engine_move_source_disagrees')
        target=normalized(target); leg='stage' if target==entry.get('staging_path') else 'target'
        if target!=(entry.get('staging_path') if leg=='stage' else entry['target_path']):
            raise ValueError('engine_move_target_disagrees')
        Path(target).parent.mkdir(parents=True,exist_ok=True)
        if entry['kind']=='relocate':
            current=self.backend.read('get_copy_version_state',entry['copy_id'])
            review=dict(step_id=entry['step_id'],fingerprint=current['fingerprint'],digest=entry['digest'],identity=physical(pin))
            if intent_exists:
                saved=self.backend.read('get_maintenance_step',self.phase['operation_id'],entry['step_id'])['step']
                intent=saved['intent'] or {}
                if (saved['phase']!=leg+'_intent' or intent.get('old_path')!=normalized(source) or
                        intent.get('new_path')!=target or intent.get('expected_fingerprint')!=current['fingerprint']):
                    raise ValueError('recovery_intent_changed')
            else:self._write('maintenance_intent',self.phase['operation_id'],leg,[review],self.root_id)
        def acknowledge(evidence,operator):
            if entry['kind']=='admit':
                if leg=='stage': return dict(confirmed=True,step_id=entry['step_id'],staging_only=True)
                return self._admit(entry,operator)
            ack=dict(step_id=entry['step_id'],digest=entry['digest'],size=entry['size'],identity=physical(operator))
            self._write('maintenance_ack_relocations',self.phase['operation_id'],leg,[ack],self.root_id)
            receipt=self._receipt(entry,leg)
            if receipt['path']!=target or receipt['copy_id']!=entry['copy_id']: raise ValueError('native_receipt_disagrees')
            return dict(confirmed=True,step_id=entry['step_id'],receipt=receipt)
        pin._native_acknowledge=acknowledge
        try:
            pin.move_to(target,journal=self.phase['journal'],acknowledge=acknowledge,
                        step_id=entry['step_id'],cancel=self.cancel)
        except BaseException as exc:
            self._fail(exc); raise
        return True

    async def after_apply(self,plan):
        if not self.phase or self.phase['plan'] is not plan: raise ValueError('phase_plan_disagrees')
        try:
            reviews=[]
            for entry in self.phase['entries']:
                pin=self.phase['operators'][entry['step_id']]
                if isinstance(pin,MovableFileContext): pin.proof()
                else: pin.check()
                current=self.backend.read('get_copy_version_state',entry['copy_id'])
                reviews.append(dict(step_id=entry['step_id'],fingerprint=current['fingerprint'],digest=entry['digest'],identity=physical(pin)))
            for batch in batches(reviews): self._write('maintenance_verify_batch',self.phase['operation_id'],batch,self.root_id)
            self._write('maintenance_complete',self.phase['operation_id'],self.root_id)
            self.phase['journal'].record('phase_complete',operation_id=self.phase['operation_id'])
            self.completed.append(self.phase['operation_id']); self._release_phase()
        except BaseException as exc:
            self._fail(exc); raise

    @contextmanager
    def archive_source(self,source,row):
        source=normalized(local_move_path(source,existing=True))
        native=self._binding(self._snapshots([source]),source)
        catalog_digest,digest=self._catalog_digest(dict(hash=row['hash']),native)
        if digest!=catalog_digest: raise ValueError('modified_archive_requires_review_keep_package')
        with PinnedFile(source) as pin:
            pin.verify(catalog_digest,pin.check(),event=self.cancel)
            yield pin
            pin.verify(catalog_digest,pin.check(),event=self.cancel)

    async def before_members(self,archive,entries,stage,known):
        # Member-original SHA stays in the manifest. Existing stamped members
        # use their verified current Native MOD digest without republishing ORIG.
        files=[]; self.member_items={}
        for index,entry in enumerate(entries):
            source=self.root/entry['target'] if entry['known'] else stage/f'{index}.bin'
            item=dict(old=source.relative_to(self.root).as_posix(),new=entry['target'],hash=entry['hash'],
                      size=source.stat().st_size,catalog_size=entry['size'],counter=entry.get('counter',0),archive_original_size=entry['size'])
            if not entry['known']: item['archive_origin']=archive
            self.member_items[id(entry)]=item; files.append(item)
        source=self.root/archive['filename']
        files.append(dict(old=archive['filename'],new=archive['filename'],hash=archive['hash'],
                          size=source.stat().st_size,catalog_size=archive.get('file_size'),counter=archive.get('counter') or 0))
        self.members_plan=dict(folder=str(self.root),files=files,duplicates=[],errors=[],status='planned',archive_sha=archive['hash'])
        await self.before_apply(self.members_plan,'archive_members')

    async def member_known(self,entry,existing):
        if existing['hash']!=entry['hash'] or existing['filename']!=entry['target']:
            raise ValueError('archive_member_original_chain_disagrees')
        item=self.member_items[id(entry)]; native_entry=self._entry(item)
        pin=self.phase['operators'][native_entry['step_id']]
        pin.verify(native_entry['digest'],pin.check(),event=self.cancel)
        return dict(copy_id=native_entry['copy_id'],original_member_sha=entry['hash'],
                    current_digest=native_entry['digest'],current_size=native_entry['size'],identity=physical(pin))

    async def publish_member(self,source,target,entry,archive):
        item=self.member_items[id(entry)]
        await self.move_file(source,Path(item['native_staging']),item)
        await self.move_file(Path(item['native_staging']),target,item)
        return True

    async def after_members(self,archive,entries):
        opid=self.phase['operation_id']
        for entry in entries:
            native=self._entry(self.member_items[id(entry)])
            entry['native_current']=dict(copy_id=native['copy_id'],operation_id=opid,step_id=native['step_id'],
                original_member_sha=entry['hash'],current_digest=native['digest'],current_size=native['size'])
        await self.after_apply(self.members_plan)
        return dict(operation_id=opid)

    async def completed_member(self,archive,member,existing):
        """A complete Native manifest may satisfy later retirement; never restore it."""
        proof=member.get('native_current') or {}
        opid,step_id=proof.get('operation_id'),proof.get('step_id')
        if not opid or not step_id:raise ValueError('archive_completed_member_native_evidence_missing')
        operation=self.backend.read('get_maintenance_operation',opid)['operation']
        step=self.backend.read('get_maintenance_step',opid,step_id)['step']
        if not operation or operation['phase']!='complete' or not step or not step['verified']:
            raise ValueError('archive_completed_member_native_receipt_missing')
        planned=step['plan']
        if (planned.get('archive_sha')!=archive['hash'] or planned.get('catalog_digest')!=member['hash'] or
                planned.get('archive_original_size')!=member['size'] or step['copy_id']!=proof.get('copy_id')):
            raise ValueError('archive_completed_member_manifest_disagrees')
        current=self.backend.read('get_copy_version_state',step['copy_id']);row=current['state']['file']
        original=planned['digest'] if planned['kind']=='admit' else planned.get('source_native_original')
        if row['hash_original']!=original:raise ValueError('archive_member_original_identity_changed')
        old=self._path(existing['filename'])
        if not row['is_offline'] and row['path']==old:return None
        disposition=dict(copy_id=row['id'],fingerprint=current['fingerprint'],path=row['path'],
            reason='native_offline' if row['is_offline'] else 'native_moved',
            original_member_sha=member['hash'],original_member_size=member['size'],operation_id=opid,step_id=step_id)
        if not row['is_offline']:
            digest=row['hash_mod'] or row['hash_original']
            with PinnedFile(row['path']) as pin:
                pin.verify(digest,pin.check(),event=self.cancel);disposition['current_digest']=digest
        return disposition

    def _fail(self,cause):
        self.failed=True
        if self.phase:
            reason=type(cause).__name__+': '+str(cause)
            self.phase['journal'].record('phase_requires_review',reason=reason)
            if self.phase['reserved']:
                op=self.backend.read('get_maintenance_operation',self.phase['operation_id'])['operation']
                if op and op['phase']=='complete':self.phase['reserved']=False
                else:self._write('maintenance_mark_review',self.phase['operation_id'],reason)

    def _release_phase(self):
        current=self.phase
        if current['reserved']: self._write('maintenance_unbind_scope_owner',current['operation_id'])
        for operator in current['operators'].values(): operator.close()
        current['journal'].close(); self.phase=None
        self.source_context=None

    def release_for_recovery(self):
        """Explicit boundary only; preserves durable scope, plans and every byte."""
        if not self.phase: return
        for operator in self.phase['operators'].values():
            if isinstance(operator,MovableFileContext) and operator.pending: operator.release_for_recovery()
        self._release_phase()
        self.active=False; self.stack.close()

    def finish_pending_moves(self):
        """Explicit live-handle recovery, with a fresh root and physical proof.

        Only settles the already performed physical step. It never executes a
        following rename, restores a deleted file, or replays an engine plan.
        """
        if not self.phase or not self.failed: raise ValueError('no_failed_live_phase')
        pending=[operator for operator in self.phase['operators'].values()
                 if isinstance(operator,MovableFileContext) and operator.pending]
        if not pending: raise ValueError('no_live_pending_handles')
        for operator in pending: operator.proof()
        opid=self.phase['operation_id']
        operation=self.backend.read('get_maintenance_operation',opid)['operation']
        self._write('maintenance_bind_scope_owner',opid,self._claim())
        self._write('maintenance_resume',opid,self.root_id,operation['plan_sha'])
        for operator in pending: operator.finish_pending(operator._native_acknowledge)
        self.failed=False
        return dict(settled=len(pending),replayed_plan=False)

    def inspect_recovery(self,operation_id):
        """Fresh bounded report, never executes an old engine plan or renames."""
        operation=self.backend.read('get_maintenance_operation',operation_id)['operation']
        if not operation: raise ValueError('unknown_maintenance_operation')
        saved_root=operation['root_identity']
        if isinstance(saved_root,str): saved_root=json.loads(saved_root)
        if saved_root!=self.root_id: raise ValueError('recovery_root_identity_changed')
        placements=[]; after=None
        while True:
            page=self.backend.read('get_maintenance_steps',operation_id,after_ordinal=after,limit=100)
            for step in page['steps']:
                entry=step['plan']; found=[]; occupied=[]; visited=set()
                for name in ('source_path','staging_path','target_path'):
                    target=entry.get(name)
                    if not target or target in visited or not Path(target).is_file(): continue
                    visited.add(target)
                    with PinnedFile(local_move_path(target,existing=True)) as pin:
                        identity=physical(pin)
                        volume,file_id=identity128(api(),pin.handle)
                        if entry.get('source_file_id128')!={'volume':volume,'file_id':file_id}:
                            occupied.append(dict(path=target,reason='other_file_id128',volume=volume,file_id=file_id));continue
                        if entry.get('source_identity') and identity['physical'][:3]!=entry['source_identity']['physical'][:3]:
                            occupied.append(dict(path=target,reason='other_physical_identity',identity=identity));continue
                        if entry.get('source_identity') and identity['physical']!=entry['source_identity']['physical']:
                            raise ValueError('recovery_source_size_or_mtime_changed')
                        pin.verify(entry['digest'],pin.check(),event=self.cancel)
                        found.append(dict(path=target,identity=identity))
                placements.append(dict(step_id=entry['step_id'],phase=step['phase'],locations=found,occupied=occupied))
            after=page.get('next_after')
            if after is None: break
        return dict(operation_id=operation_id,placements=placements,requires_review=True,replayed=False)

    def _saved_steps(self,operation_id):
        result=[];after=None
        while True:
            page=self.backend.read('get_maintenance_steps',operation_id,after_ordinal=after,limit=100)
            result.extend(page['steps']);after=page['next_after']
            if after is None:return result

    def review_recovery(self,operation_id):
        """Explicit review, current physical proof and Native fingerprints; no writes."""
        if self.phase:raise ValueError('recovery_requires_idle_session')
        operation=self.backend.read('get_maintenance_operation',operation_id)['operation']
        steps=self._saved_steps(operation_id)
        if operation is None or len(steps)!=operation['expected_steps']:
            raise ValueError('recovery_incomplete_sealed_plan_requires_manual_review')
        if plan_hash([step['plan'] for step in steps])!=operation['plan_sha']:
            raise ValueError('recovery_native_plan_seal_changed')
        report=self.inspect_recovery(operation_id);report['operation']=operation
        report['steps']=steps;report['current']=[]
        for step,placement in zip(steps,report['placements']):
            if len(placement['locations'])!=1:raise ValueError('recovery_requires_one_verified_location_per_entry')
            copy_id=step['copy_id']
            current=None if copy_id is None else self.backend.read('get_copy_version_state',copy_id)
            if current is not None:
                row=current['state']['file'];entry=step['plan']
                allowed={entry.get('source_path'),entry.get('staging_path'),entry['target_path']}
                if (row['is_offline'] or row['path'] not in allowed or
                        (row['hash_mod'] or row['hash_original'])!=entry['digest'] or row['file_size']!=entry['size']):
                    raise ValueError('recovery_native_current_changed')
                expected_native={'planned':step['plan'].get('source_path'),'stage_intent':step['plan'].get('source_path'),
                    'staged':step['plan'].get('staging_path'),'target_intent':step['plan'].get('staging_path'),
                    'complete':step['plan']['target_path'],'unchanged':step['plan']['target_path'],
                    'admitted':step['plan']['target_path']}.get(step['phase'])
                if expected_native is not None and row['path']!=expected_native:
                    raise ValueError('recovery_native_location_changed')
            if step['phase'] in ('complete','unchanged','admitted') and placement['locations'][0]['path']!=step['plan']['target_path']:
                raise ValueError('recovery_completed_physical_location_changed')
            report['current'].append(current)
        report['counts']=dict(entries=len(steps),remaining=sum(step['phase'] not in ('complete','unchanged','admitted') for step in steps),
                              admissions=sum(step['plan']['kind']=='admit' for step in steps),packages_deleted=0)
        report['review_token']=hashlib.sha256(json.dumps(report,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        return report

    async def resume_recovery(self,operation_id,expected_review_token):
        """Apply only a freshly reviewed Native intent; never replay an engine plan."""
        reviewed=self.review_recovery(operation_id)
        if reviewed['review_token']!=expected_review_token:raise ValueError('recovery_review_changed')
        operation=reviewed['operation'];steps=reviewed['steps']
        directory=self.root/'.cherry-maintenance'/operation_id;directory.mkdir(parents=True,exist_ok=True)
        journal=MoveJournal(directory/('recovery-'+str(uuid4())+'.jsonl'))
        fresh=dict(folder=str(self.root),files=[],duplicates=[],errors=[],status='reviewed_recovery',native_operation_id=operation_id)
        current=dict(operation_id=operation_id,label=steps[0]['plan'].get('phase_kind','compact') if steps else 'compact',
                     plan=fresh,entries=[],operators={},journal=journal,reserved=operation['phase']!='complete')
        self.phase=current
        try:
            for step,placement,native in zip(steps,reviewed['placements'],reviewed['current']):
                entry=dict(step['plan']);step_id=entry['step_id'];location=placement['locations'][0]
                self.pins.pin(Path(location['path']).parent)
                pin=(MovableFileContext(location['path'],entry['digest'],directories=self.pins,cancel_event=self.cancel)
                     if entry['kind']!='unchanged' and entry.get('source_path')!=entry['target_path'] else PinnedFile(location['path']))
                pin.__enter__();current['operators'][step_id]=pin
                pin.verify(entry['digest'],pin.check(),event=self.cancel)
                volume,file_id=identity128(api(),pin.handle)
                if physical(pin)!=location['identity'] or entry['source_file_id128']!=dict(volume=volume,file_id=file_id):
                    raise ValueError('recovery_file_changed_after_review')
                if native is not None and self.backend.read('get_copy_version_state',step['copy_id'])!=native:
                    raise ValueError('recovery_native_changed_after_review')
                if step['copy_id'] is not None:entry['copy_id']=step['copy_id']
                current['entries'].append(entry)
                fresh['files'].append(dict(old=Path(entry['source_path']).relative_to(self.root).as_posix(),
                    new=Path(entry['target_path']).relative_to(self.root).as_posix(),hash=entry['digest'],catalog_hash=entry['catalog_digest'],
                    size=entry['size'],catalog_size=entry.get('catalog_size',entry['size']),counter=entry.get('catalog_counter',0),_native_step_id=step_id))
            journal.record('fresh_recovery_review',review_token=expected_review_token,counts=reviewed['counts'])
            if operation['phase']!='complete':
                self._write('maintenance_bind_scope_owner',operation_id,self._claim())
                if operation['phase']=='review':self._write('maintenance_resume',operation_id,self.root_id,operation['plan_sha'])
                elif operation['phase']=='ready':self._write('maintenance_activate',operation_id,self.root_id)
                admissions=[]
                for entry in current['entries']:
                    step=self.backend.read('get_maintenance_step',operation_id,entry['step_id'])['step']
                    if entry['kind']=='admit' and step['phase']=='planned':admissions.append(entry['step_id'])
                for batch in batches(admissions):self._write('maintenance_prepare_admissions',operation_id,batch,self.root_id)
                # Settle existing physical intents, then finish ALL staging.
                for item,entry in zip(fresh['files'],current['entries']):
                    self._cancel();step=self.backend.read('get_maintenance_step',operation_id,entry['step_id'])['step']
                    pin=current['operators'][entry['step_id']]
                    if entry['kind']=='relocate' and step['phase']=='stage_intent':
                        if normalized(pin.path)==step['intent']['new_path']:self._recover_ack(entry,pin,'stage')
                        else:await self.move_file(pin.path,Path(entry['staging_path']),item,intent_exists=True)
                    elif entry['kind']=='relocate' and step['phase']=='planned':
                        await self.move_file(pin.path,Path(entry['staging_path']),item)
                    elif entry['kind']=='admit' and step['phase']=='admit_intent' and normalized(pin.path) not in (entry.get('staging_path'),entry['target_path']):
                        await self.move_file(pin.path,Path(entry['staging_path']),item)
                for item,entry in zip(fresh['files'],current['entries']):
                    self._cancel();step=self.backend.read('get_maintenance_step',operation_id,entry['step_id'])['step']
                    pin=current['operators'][entry['step_id']]
                    if entry['kind']=='relocate' and step['phase']=='target_intent':
                        if normalized(pin.path)==step['intent']['new_path']:self._recover_ack(entry,pin,'target')
                        else:await self.move_file(pin.path,Path(entry['target_path']),item,intent_exists=True)
                    elif entry['kind']=='relocate' and step['phase']=='staged':
                        await self.move_file(pin.path,Path(entry['target_path']),item)
                    elif entry['kind']=='admit' and step['phase']=='admit_intent':
                        if normalized(pin.path)==entry['target_path']:self._admit(entry,pin)
                        else:await self.move_file(pin.path,Path(entry['target_path']),item)
            await self._recover_catalog(fresh)
            if operation['phase']!='complete':await self.after_apply(fresh)
            else:self._release_phase()
            return dict(operation_id=operation_id,complete=True,counts=reviewed['counts'],replayed_engine_plan=False)
        except BaseException as exc:
            self._fail(exc);raise

    def _recover_ack(self,entry,pin,leg):
        ack=dict(step_id=entry['step_id'],digest=entry['digest'],size=entry['size'],identity=physical(pin))
        self._write('maintenance_ack_relocations',self.phase['operation_id'],leg,[ack],self.root_id)
        self._receipt(entry,leg)
        self.phase['journal'].record('recovered_physical_intent',step_id=entry['step_id'],leg=leg,proof=ack)

    async def _recover_catalog(self,fresh):
        from third_party.cherry_dl import catalog,archives,project_maintenance
        entries=self.phase['entries'];kind=self.phase['label']
        for entry in entries:
            pin=self.phase['operators'][entry['step_id']]
            native=self.backend.read('get_copy_version_state',entry['copy_id'])['state']['file']
            original=entry['digest'] if entry['kind']=='admit' else entry.get('source_native_original')
            if (normalized(pin.path)!=entry['target_path'] or native['path']!=entry['target_path'] or native['is_offline'] or
                    (native['hash_mod'] or native['hash_original'])!=entry['digest'] or native['file_size']!=entry['size'] or
                    native['hash_original']!=original):
                raise ValueError('recovery_catalog_target_not_published')
        files=[dict(item,hash=item['catalog_hash'],size=item.get('catalog_size',item['size'])) for item in fresh['files']]
        if kind=='organize':
            for item in files:await catalog.relocate_file(self.root,item['hash'],item['new'])
        elif kind=='archive_members':
            for item,entry in zip(files,entries):
                origin=entry.get('archive_origin')
                if origin:
                    await catalog.add_file(self.root,item['hash'],item['new'],file_size=item['size'],counter=item['counter'],
                        url_source=origin.get('url_source'),content_service=origin.get('content_service',''),
                        post_id=origin.get('post_id',''),content_role='archive_member')
            archive_sha=entries[0].get('archive_sha') if entries else None
            if not archive_sha:raise ValueError('archive_recovery_manifest_missing')
            receipt=self.root/'.cherry-archives'/(archive_sha+'.native.json')
            with PinnedFile(receipt) as pin:
                buffer=io.BufferedReader(pin.stream,buffer_size=1024*1024)
                try:saved=json.loads(buffer.read())
                finally:buffer.detach()
            if saved.get('archive',{}).get('hash')!=archive_sha:raise ValueError('archive_recovery_manifest_disagrees')
            by_hash={entry['catalog_digest']:entry for entry in entries if entry['catalog_digest']!=archive_sha}
            for member in saved['entries']:
                entry=by_hash.get(member['hash'])
                if entry is None or entry.get('archive_original_size')!=member['size']:
                    raise ValueError('archive_recovery_member_manifest_changed')
                member['target']=Path(entry['target_path']).relative_to(self.root).as_posix()
                member['native_current']=dict(copy_id=entry['copy_id'],original_member_sha=member['hash'],
                    operation_id=self.phase['operation_id'],step_id=entry['step_id'],
                    current_digest=entry['digest'],current_size=entry['size'],identity=physical(self.phase['operators'][entry['step_id']]))
            saved['state']='completed_keep_package';archives._write(receipt,saved)
        else:
            await catalog.apply_project_maintenance(self.root,files,entries[0].get('catalog_counters',{}) if entries else {},
                entries[0].get('catalog_audit',0) if entries else 0)
            state=self.root/project_maintenance.STATE_DIR;state.mkdir(exist_ok=True)
            project_maintenance._update_receipts(self.root,state,files)
            project_maintenance._write(state/project_maintenance.PLAN_NAME,
                dict(fresh,status='completed',counters=entries[0].get('catalog_counters',{}) if entries else {},audit_version=0))

    def __exit__(self,kind,error,trace):
        if self.phase:
            if any(isinstance(operator,MovableFileContext) and operator.pending for operator in self.phase['operators'].values()):
                raise RecoveryRequired(self,error or 'unacknowledged_physical_move') from error
            if self.phase['reserved'] and not self.failed: self._fail(error or 'unfinished_phase')
            self._release_phase()
        self.active=False; self.stack.close()


class NativeMaintenanceFailure(RuntimeError):
    def __init__(self,errors,operation_id=None):
        self.errors=list(errors);self.operation_id=operation_id
        super().__init__('maintenance_review_required: '+'; '.join(self.errors))


async def process_native_project(folder,artist,*,data_service,profile_id=0,emit=lambda _:None,cancel_event=None,**options):
    from core.maintenance_backend import Backend
    from .profile_scope import profile_scope
    from third_party.cherry_dl.archives import process_project
    if data_service is None:raise ValueError('native_maintenance_service_required')
    backend=Backend(data_service)
    if not backend.read('get_maintenance_status')['prepared']:
        backend.write('prepare_maintenance_operations')
    try:
        with NativePhaseSession(folder,profile_id,backend,profile_guard=profile_scope(folder),cancel=cancel_event) as session:
            pending=backend.read('get_maintenance_pending',session.root_id)['operations']
            if pending:raise NativeMaintenanceFailure(['pending_native_maintenance'],pending[0]['operation_id'])
            result=await process_project(Path(folder),artist,emit,native_hooks=session,**options)
            if result.errors:
                raise NativeMaintenanceFailure(result.errors,session.error_operation_id)
            return result
    except RecoveryRequired as exc:
        operation_id=exc.session.phase['operation_id']
        exc.session.release_for_recovery()
        raise NativeMaintenanceFailure([str(exc)],operation_id) from exc
    except NativeMaintenanceFailure:raise
    except Exception as exc:
        raise NativeMaintenanceFailure([str(exc)],session.error_operation_id) from exc


def review_native_recovery(folder,operation_id,*,data_service,cancel_event=None):
    from core.maintenance_backend import Backend
    from .profile_scope import profile_scope
    with NativePhaseSession(folder,0,Backend(data_service),profile_guard=profile_scope(folder),cancel=cancel_event) as session:
        return session.review_recovery(operation_id)


async def resume_native_recovery(folder,operation_id,expected_review_token,*,data_service,cancel_event=None):
    from core.maintenance_backend import Backend
    from .profile_scope import profile_scope
    try:
        with NativePhaseSession(folder,0,Backend(data_service),profile_guard=profile_scope(folder),cancel=cancel_event) as session:
            return await session.resume_recovery(operation_id,expected_review_token)
    except RecoveryRequired as exc:
        exc.session.release_for_recovery()
        raise NativeMaintenanceFailure([str(exc)],operation_id) from exc
