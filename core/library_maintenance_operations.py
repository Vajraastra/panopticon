"""Durable Native maintenance scopes, worker proofs and idempotent receipts.

Only LibraryService's SQL owner executes commands. Workers hold physical handles;
no filesystem observation or image mutation belongs in this module.
"""
from dataclasses import dataclass, field
from contextlib import contextmanager
import hashlib
import inspect
import json
import sqlite3
import time
from uuid import UUID

from core.library_versions import state
from core.path_identity import local_path
from core.path_identity_registry import prepared as registry_prepared
from core.derived_paths import DerivedPaths

OPS='panopticon_maintenance_operations_v1'
STEPS='panopticon_maintenance_steps_v1'
RECEIPTS='panopticon_maintenance_receipts_v1'
MEMBERS='panopticon_maintenance_members_v1'
NAMES='panopticon_maintenance_names_v1'
_AUTHORITY=object()
HOLDING=('ready','active','review')
MAINTENANCE_COMMANDS=frozenset({'maintenance_reserve','maintenance_plan_batch','maintenance_activate',
 'maintenance_bind_scope_owner','maintenance_unbind_scope_owner',
    'maintenance_mark_review','maintenance_resume','maintenance_cancel_ready','maintenance_prepare_admissions',
    'maintenance_intent','maintenance_ack_relocations','maintenance_admit_descendants',
    'maintenance_verify_batch','maintenance_complete'})


def encode(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')


def sha(value):return hashlib.sha256(encode(value)).hexdigest()
def path(value):return local_path(str(value))
def inside(value,root):return value.casefold()==root.casefold() or value.casefold().startswith(root.casefold().rstrip('/')+'/')
def overlap(left,right):return inside(left,right) or inside(right,left)
def require(value,reason):
    if not value:raise ValueError(reason)


def file_proof(identity,canonical,size):
    physical=identity.get('physical',())
    require(identity.get('canonical')==canonical and len(physical)==7 and
            all(type(item) is int and item>=0 for item in physical) and
            (physical[3]<<32)+physical[4]==size,'invalid_canonical_physical_proof')


def plan_hash(entries):
    digest=hashlib.sha256()
    for entry in entries:digest.update(encode(entry)+b'\n')
    return digest.hexdigest()


@dataclass
class Targets:
    ids:set=field(default_factory=set)
    paths:set=field(default_factory=set)
    canonical:dict=field(default_factory=dict)
    unknown:str|None=None


# Every current LibraryService WRITES name is deliberately classified. Complex
# policies below stay UNKNOWN until their complete target resolver is reviewed.
# There is no generic recursive argument search or permissive default.
RESOLVERS={
 'prepare_ingest_reviews':'global',
 'prepare_copy_reconciliations':'global','reconcile_copy_versions_batch':'reconcile',
 'prepare_path_identity':'global','store_path_identity_batch':'identity_records',
 'prepare_transfers':'global','approve_transfer':'transfer','commit_transfer':'transfer',
 'approve_transfer_restore':'transfer','abort_transfer':'transfer_receipt','retire_transfer_copy':'transfer_receipt',
 'commit_maintenance_relocation':'relocations',
 'prepare_generation_history':'global','prepare_derived_copies':'global','prepare_duplicate_search':'global',
 'prepare_phash_registry':'global','rebuild_fts':'global','repair_fts':'global','vacuum_database':'global',
 'approve_metadata_backup_purge':'copy_id','approve_metadata_edit':'copy_id','commit_metadata_edit':'copy_id',
 'approve_derived_copy':'copy_id','register_derived_copy':'copy_id','restore_duplicate_copy':'copy_id',
 'archive_copy':'copy_id','make_copy_online':'copy_id',
 'register_files_minimal':'minimal_records','register_files_observed':'observed_records',
 'register_external_copy_observed':'observed_records','register_catalog_files_observed':'observed_records',
 'update_file_metadata':'path','add_watched_folder':'path','remove_watched_folder':'path',
 'archive_watched_folder_reviewed':'folder','add_tag_to_file':'path','remove_tag_from_file':'path',
 'update_file_rating':'path','edit_curation':'path','restore_curation':'path','register_curation_copy':'path',
 'adopt_curation_identity':'path','remove_files':'paths_list','set_hash_original_bulk':'id_pairs',
 'archive_missing_copies':'unknown:inventory_scope_and_approved_rows',
 'archive_duplicate_copies':'unknown:duplicate_policy_and_recovery_destinations',
 'approve_duplicate_purge':'unknown:retirement_and_keeper_receipts',
 'store_phash_batch':'unknown:perceptual_hash_review_records',
}


class MaintenanceOperations:
    def _assert_owner_version(self,operation_id):
        expected=self.bound_versions.get(operation_id)
        current=self.conn.execute('PRAGMA data_version').fetchone()[0]
        require(expected==(self.owner_nonce,current),'maintenance_external_sql_change_requires_review')

    def _refresh_scope(self,op):
        """Fresh bind rebuilds the indexed scope under the writer transaction.

        Retain historical member IDs even if another writer changed their names;
        also capture every new raw/canonical name now inside the reserved roots.
        """
        operation_id=op['operation_id'];aliases=json.loads(op['aliases'])
        self.conn.execute(f'DELETE FROM {NAMES} WHERE operation_id=?',(operation_id,))
        canonical="CASE WHEN p.status='resolved' THEN p.canonical END" if registry_prepared(self.conn) else 'NULL'
        registry=' LEFT JOIN path_identity_records p ON p.file_id=f.id' if canonical!='NULL' else ''
        sql=f'SELECT f.id,f.path,{canonical},f.is_offline,m.copy_id FROM files f'+registry
        sql+=f' LEFT JOIN {MEMBERS} m ON m.copy_id=f.id AND m.operation_id=?'
        for copy_id,stored,registered,offline,member in self.conn.execute(sql,(operation_id,)):
            stored=path(stored);registered=path(registered) if registered else None
            if member is not None or any(inside(name,alias) for name in (stored,registered) if name for alias in aliases):
                self.conn.execute(f'INSERT OR IGNORE INTO {MEMBERS} VALUES(?,?)',(operation_id,copy_id))
                self.conn.execute(f'INSERT INTO {NAMES} VALUES(?,?,?,?,?)',
                    (operation_id,copy_id,stored.casefold(),registered.casefold() if registered else None,offline))
        self._assert_scope_unambiguous(operation_id)

    def _assert_scope_unambiguous(self,operation_id):
        require(not self.conn.execute(f'SELECT name FROM (SELECT copy_id,path_key name FROM {NAMES} '
            'WHERE operation_id=? AND is_offline=0 UNION '
            f'SELECT copy_id,canonical_key FROM {NAMES} WHERE operation_id=? AND is_offline=0 AND canonical_key IS NOT NULL) '
            'GROUP BY name HAVING count(*)>1 LIMIT 1',(operation_id,operation_id)).fetchone(),
            'maintenance_scope_alias_ambiguous')

    def _refresh_name(self,operation_id,copy_id):
        row=self.conn.execute('SELECT path,is_offline FROM files WHERE id=?',(copy_id,)).fetchone()
        require(row is not None,'unknown_native_copy_id')
        _,canonical=self._registered_names(copy_id)
        self.conn.execute(f'INSERT OR REPLACE INTO {NAMES} VALUES(?,?,?,?,?)',
            (operation_id,copy_id,path(row[0]).casefold(),canonical.casefold() if canonical else None,row[1]))

    def _selected_steps(self,operation_id,step_ids):
        require(1<=len(step_ids)<=100 and len(set(step_ids))==len(step_ids),'maintenance_batch_limit')
        result={}
        for step_id in step_ids:
            row=self.conn.execute(f'SELECT step_id,plan_json,phase,copy_id,intent_json,verified_fingerprint '
                f'FROM {STEPS} WHERE operation_id=? AND step_id=?',(operation_id,step_id)).fetchone()
            require(row is not None,'unknown_maintenance_step')
            result[row[0]]=dict(step_id=row[0],plan=json.loads(row[1]),phase=row[2],copy_id=row[3],intent=row[4],verified=row[5])
        return result

    def _validate_plan_source(self,item):
        if item['kind']=='admit':self._new_target(item['target_path']);return
        current=state(self.conn,item['copy_id']);row=current['state']['file'];known=row['hash_mod'] or row['hash_original']
        require(current['fingerprint']==item['source_fingerprint'] and row['path']==item['source_path'] and not row['is_offline'],
                'maintenance_source_changed')
        require(type(known) is str and known.lower()==item['digest'],'maintenance_source_digest_changed')
        require(row['file_size']==item['size'],'maintenance_source_size_changed')
        if registry_prepared(self.conn):
            registered=self.conn.execute('SELECT stored_path,is_offline,status,canonical,physical '
                'FROM path_identity_records WHERE file_id=?',(item['copy_id'],)).fetchone()
            require(registered is not None and registered[:4]==(row['path'],0,'resolved',item['source_identity']['canonical']),
                    'maintenance_source_alias_requires_review')
            require(json.loads(registered[4])==list(item['source_identity']['physical']),
                    'maintenance_source_physical_requires_review')

    def _ops(self):
        return [dict(zip(('operation_id','profile_id','root','root_identity','aliases','plan_sha','expected_steps','phase'),row))
                for row in self.conn.execute(f'SELECT operation_id,profile_id,root,root_identity,aliases,plan_sha,expected_steps,phase '
                                              f'FROM {OPS} WHERE phase<>\'complete\' ORDER BY operation_id')]

    def reservation_snapshot(self):return sha(self._ops())

    def _scope(self,value,op):return any(overlap(path(value),alias) for alias in json.loads(op['aliases']))

    def _registered_names(self,copy_id):
        row=self.conn.execute('SELECT path FROM files WHERE id=?',(copy_id,)).fetchone()
        require(row is not None,'unknown_native_copy_id')
        names={path(row[0])};canonical=None
        if registry_prepared(self.conn):
            record=self.conn.execute('SELECT status,canonical FROM path_identity_records WHERE file_id=?',(copy_id,)).fetchone()
            if record and record[0]=='resolved' and record[1]:
                canonical=path(record[1]);names.add(canonical)
        return names,canonical

    def _names_stream(self,active_only=False):
        condition=' WHERE f.is_offline=0' if active_only else ''
        if registry_prepared(self.conn):
            sql="SELECT f.id,f.path,CASE WHEN p.status='resolved' THEN p.canonical END FROM files f "
            sql+='LEFT JOIN path_identity_records p ON p.file_id=f.id'+condition
        else:sql='SELECT f.id,f.path,NULL FROM files f'+condition
        for copy_id,stored,canonical in self.conn.execute(sql):
            yield copy_id,path(stored),path(canonical) if canonical else None

    def resolve(self,command,args,kwargs):
        category=RESOLVERS.get(command)
        if category is None:return Targets(unknown='unmapped_native_command')
        if category=='global' or category.startswith('unknown:'):return Targets(unknown=category)
        signature=inspect.signature(getattr(self.repo,command))
        bound=dict(signature.bind(*args,**kwargs).arguments)
        for name,parameter in signature.parameters.items():
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                bound.update(bound.pop(name,{}))
        targets=Targets()
        def add_path(value):targets.paths.add(path(value))
        if category in ('path','folder','paths_list'):
            value=bound[category]
            for item in value if category=='paths_list' else [value]:add_path(item)
        elif category=='copy_id':targets.ids.add(bound['copy_id'])
        elif category=='id_pairs':targets.ids.update(item[0] for item in bound['pairs'])
        elif category in ('minimal_records','observed_records','identity_records'):
            values=bound.get('files_data',bound.get('records'))
            for item in values:
                if category=='identity_records':
                    targets.ids.add(item['file_id']);add_path(item['stored_path'])
                    if item.get('canonical'):targets.canonical[path(item['stored_path'])]=path(item['canonical'])
                else:
                    data=item if category=='minimal_records' else item['data'];add_path(data['path'])
                    identity=item.get('identity') if category=='observed_records' else None
                    if identity and identity.get('status')=='resolved':
                        targets.canonical[path(data['path'])]=path(identity['canonical'])
            if bound.get('watched_folder'):add_path(bound['watched_folder'])
        elif category=='relocations':
            for item in bound['values']:
                targets.ids.add(item['id']);add_path(item['old_path']);add_path(item['new_path'])
        elif category=='reconcile':targets.ids.update(item['copy_id'] for item in bound['requests'])
        elif category in ('transfer','transfer_receipt'):
            if category=='transfer':value=bound['request']
            else:
                record=self.conn.execute('SELECT source_copy_id,result_copy_id,source_path,result_path '
                    'FROM panopticon_transfers_v1 WHERE operation_id=?',(bound['operation_id'],)).fetchone()
                if record is None:return Targets(unknown='transfer_receipt_missing')
                value=dict(source_copy_id=record[0],result_copy_id=record[1],source_path=record[2],result_path=record[3])
            for name in ('source_copy_id','result_copy_id'):
                if value.get(name) is not None:targets.ids.add(value[name])
            for name in ('source_path','result_path'):add_path(value[name])
            for name in ('source_paths','destination_paths'):
                for item in value.get(name,[]):add_path(item)
        # These are declared public path fields of known core commands. An
        # unknown command never reaches this supplemental list.
        for name in ('path','source_path','result_path','backup_path','expected_path','folder','watched_folder'):
            if bound.get(name):add_path(bound[name])
        for name in ('source_paths','destination_paths','backup_paths'):
            for item in bound.get(name,[]):add_path(item)
        file_data=bound.get('file_data')
        if isinstance(file_data,dict) and file_data.get('path'):add_path(file_data['path'])
        if bound.get('expected_copy_id') is not None:targets.ids.add(bound['expected_copy_id'])
        identity=bound.get('identity')
        if isinstance(identity,dict) and identity.get('canonical'):add_path(identity['canonical'])
        return targets

    def guard(self,command,args,kwargs,proofs=()):
        ops=self._ops()
        require(not any(name in kwargs for name in ('maintenance_operation_id','_maintenance_operation_id','authorized_operation')),
                'operation_context_not_allowed_on_generic_command')
        if not ops:return
        require(all(op['operation_id'] in self.bound_roots for op in ops),'maintenance_owner_fence_unbound')
        for op in ops:self._assert_owner_version(op['operation_id'])
        targets=self.resolve(command,args,kwargs)
        require(targets.unknown is None,'maintenance_write_targets_unknown:'+str(targets.unknown))
        hints=dict(targets.canonical);canonical={};ids=set(targets.ids)
        for value in targets.paths:
            ids.update(row[0] for row in self.conn.execute('SELECT id FROM files WHERE path=?',(value,)))
        for copy_id in ids:
            names,registered=self._registered_names(copy_id);targets.paths.update(names)
            if registered:
                for value in names:canonical[value]=registered
            require(not self.conn.execute(f'SELECT 1 FROM {MEMBERS} m JOIN {OPS} o USING(operation_id) '
                "WHERE m.copy_id=? AND o.phase<>'complete' LIMIT 1",(copy_id,)).fetchone(),'maintenance_copy_reserved')
        observed={path(proof['path']):proof for proof in proofs}
        for value in targets.paths:
            require(not any(self._scope(value,op) for op in ops),'maintenance_path_reserved')
            if value in hints:
                require(not any(self._scope(hints[value],op) for op in ops),'maintenance_canonical_reserved')
        for value in targets.paths:
            proof=observed.get(value)
            if proof:
                require(proof.get('reservation_snapshot')==self.reservation_snapshot() and
                        proof.get('owner_nonce')==self.owner_nonce and proof.get('pins_held') is True and
                        len(proof.get('physical',()))==7,
                        'canonical_worker_proof_stale_or_missing')
                canonical[value]=path(proof['canonical'])
                canonical[path(proof['canonical'])]=path(proof['canonical'])
            require(value in canonical,'canonical_worker_proof_required')
            require(not any(self._scope(canonical[value],op) for op in ops),'maintenance_canonical_reserved')
        keys={value.casefold() for value in canonical.values()}
        require(not keys.intersection(active_aliases(self.repo)),'native_path_alias_ambiguous')

    def _op(self,operation_id,root_identity=None):
        row=self.conn.execute(f'SELECT operation_id,profile_id,root,root_identity,aliases,plan_sha,expected_steps,phase '
                              f'FROM {OPS} WHERE operation_id=?',(operation_id,)).fetchone()
        require(row is not None,'unknown_maintenance_operation')
        op=dict(zip(('operation_id','profile_id','root','root_identity','aliases','plan_sha','expected_steps','phase'),row))
        if root_identity is not None:
            require(list(root_identity[:3])==json.loads(op['root_identity']),'maintenance_root_identity_changed')
            require(op['phase']=='complete' or operation_id in self.bound_roots,'maintenance_owner_fence_unbound')
            if op['phase']!='complete':self._assert_owner_version(operation_id)
        return op

    def _maintenance_bind_scope_owner(self,operation_id,proof):
        op=self._op(operation_id)
        require(op['phase'] in HOLDING,'maintenance_scope_not_reserved')
        require(proof.get('owner_nonce')==self.owner_nonce,'maintenance_owner_nonce_changed')
        require(proof.get('kind')=='directory' and proof.get('handles_held') is True and
                path(proof.get('canonical'))==op['root'] and
                list(proof.get('physical',())[:3])==json.loads(op['root_identity']),
                'maintenance_root_fence_requires_fresh_canonical_pin')
        self._refresh_scope(op)
        if op['phase']=='ready':
            for step in self._steps(operation_id):self._validate_plan_source(step['plan'])
        # A new owner lease cannot reuse verifications from the prior SQL view.
        self.conn.execute(f'UPDATE {STEPS} SET verified_fingerprint=NULL '
            'WHERE operation_id=? AND verified_fingerprint IS NOT NULL',(operation_id,))
        self.bound_roots.add(operation_id)
        self.bound_versions[operation_id]=(self.owner_nonce,self.conn.execute('PRAGMA data_version').fetchone()[0])
        return dict(changed_rows=0,owner_bound=True)

    def _maintenance_unbind_scope_owner(self,operation_id):
        """Worker calls this BEFORE closing root handles on Stop/finally.

        A process crash instead discards the whole owner nonce. Neither path
        releases the durable reservation or reclassifies an unacknowledged step.
        """
        self._op(operation_id)
        self.bound_roots.discard(operation_id)
        self.bound_versions.pop(operation_id,None)
        return dict(changed_rows=0,owner_bound=False)

    def _maintenance_reserve(self,operation_id,profile_id,root,root_identity,aliases,plan_sha,expected_steps):
        require(str(UUID(operation_id))==operation_id,'invalid_operation_id')
        root=path(root);physical=list(root_identity[:3])
        require(len(physical)==3 and all(type(item) is int and item>=0 for item in physical),'invalid_root_identity')
        require(1<=len(aliases)<=32 and root in [path(item['path']) for item in aliases],'invalid_root_aliases')
        require(all(list(item['root_identity'][:3])==physical for item in aliases),'root_alias_identity_disagrees')
        names=sorted(set(path(item['path']) for item in aliases))
        require(type(expected_steps) is int and expected_steps>=0 and len(plan_sha)==64,'invalid_plan_header')
        previous=self.conn.execute(f'SELECT profile_id,root,root_identity,aliases,plan_sha,expected_steps FROM {OPS} '
                                   'WHERE operation_id=?',(operation_id,)).fetchone()
        incoming=(profile_id,root,json.dumps(physical),json.dumps(names),plan_sha,expected_steps)
        if previous:
            require(previous==incoming,'maintenance_operation_conflict');return dict(changed_rows=0)
        require(not any(json.loads(op['root_identity'])==physical or
                        any(overlap(alias,old) for old in json.loads(op['aliases']) for alias in names) for op in self._ops()),
                'maintenance_scope_already_reserved')
        self.conn.execute(f'INSERT INTO {OPS}(operation_id,profile_id,root,root_identity,aliases,plan_sha,expected_steps,phase) '
                           "VALUES(?,?,?,?,?,?,?,'ready')",(operation_id,*incoming))
        for copy_id,stored,canonical in self._names_stream():
            if any(any(inside(name,alias) for alias in names) for name in (stored,canonical) if name):
                self.conn.execute(f'INSERT INTO {MEMBERS} VALUES(?,?)',(operation_id,copy_id))
                self._refresh_name(operation_id,copy_id)
        self._assert_scope_unambiguous(operation_id)
        return dict(changed_rows=1,phase='ready')

    def _maintenance_plan_batch(self,operation_id,entries,root_identity):
        op=self._op(operation_id,root_identity);require(op['phase']=='ready','maintenance_plan_not_ready')
        require(1<=len(entries)<=100,'maintenance_batch_limit')
        changed=0
        for ordinal,entry in entries:
            require(entry['kind'] in ('relocate','unchanged','admit'),'unknown_maintenance_step_kind')
            require(any(inside(path(entry['target_path']),alias) for alias in json.loads(op['aliases'])),'target_outside_scope')
            if entry['kind']!='admit':
                file_proof(entry['source_identity'],entry['source_identity']['canonical'],entry['size'])
                require(any(inside(path(entry['source_identity']['canonical']),alias) for alias in json.loads(op['aliases'])),
                        'source_outside_scope')
                require(entry['source_identity']['physical'][4]+(entry['source_identity']['physical'][3]<<32)==entry['size'],
                        'source_physical_size_mismatch')
                require((entry['kind']=='unchanged') == (entry['source_path']==entry['target_path']),
                        'unchanged_step_contract')
                if entry['kind']=='relocate':
                    staging=path(entry['staging_path']);prefix=op['root'].rstrip('/')+'/.cherry-maintenance/'+operation_id+'/staging'
                    require(inside(staging,prefix) and staging not in (entry['source_path'],entry['target_path']),
                            'invalid_staging_path')
            self._validate_plan_source(entry)
            raw=encode(entry).decode('utf-8')
            old=self.conn.execute(f'SELECT plan_json FROM {STEPS} WHERE operation_id=? AND ordinal=?',(operation_id,ordinal)).fetchone()
            if old:
                require(old==(raw,),'maintenance_plan_page_conflict');continue
            self.conn.execute(f'INSERT INTO {STEPS}(operation_id,ordinal,step_id,plan_json,phase,copy_id) VALUES(?,?,?,?,?,?)',
                (operation_id,ordinal,entry['step_id'],raw,'unchanged' if entry['kind']=='unchanged' else 'planned',entry.get('copy_id')));changed+=1
            if entry.get('copy_id') is not None:
                self.conn.execute(f'INSERT OR IGNORE INTO {MEMBERS} VALUES(?,?)',(operation_id,entry['copy_id']))
        return dict(changed_rows=changed)

    def _steps(self,operation_id):
        for row in self.conn.execute(f'SELECT step_id,plan_json,phase,copy_id,intent_json,verified_fingerprint '
                                     f'FROM {STEPS} WHERE operation_id=? ORDER BY ordinal',(operation_id,)):
            yield dict(step_id=row[0],plan=json.loads(row[1]),phase=row[2],copy_id=row[3],intent=row[4],verified=row[5])

    def _maintenance_activate(self,operation_id,root_identity):
        op=self._op(operation_id,root_identity);require(op['phase']=='ready','maintenance_not_ready')
        digest=hashlib.sha256();count=0
        for step in self._steps(operation_id):
            item=step['plan'];digest.update(encode(item)+b'\n');count+=1
        require(count==op['expected_steps'] and digest.hexdigest()==op['plan_sha'],'maintenance_plan_sha_or_count_changed')
        self.conn.execute(f"UPDATE {OPS} SET phase='active' WHERE operation_id=?",(operation_id,))
        return dict(changed_rows=1,phase='active')

    def _maintenance_mark_review(self,operation_id,reason):
        op=self._op(operation_id);require(op['phase'] in HOLDING and bool(reason),'invalid_review_transition')
        self.conn.execute(f"UPDATE {OPS} SET review_from=CASE WHEN phase='review' THEN review_from ELSE phase END,"
                          "phase='review',reason=? WHERE operation_id=?",(reason,operation_id))
        self.bound_roots.discard(operation_id)
        self.bound_versions.pop(operation_id,None)
        return dict(changed_rows=1,phase='review')

    def _maintenance_resume(self,operation_id,root_identity,plan_sha):
        op=self._op(operation_id,root_identity)
        require(op['phase']=='review' and plan_sha==op['plan_sha'],'maintenance_resume_requires_fresh_review')
        previous=self.conn.execute(f'SELECT review_from FROM {OPS} WHERE operation_id=?',(operation_id,)).fetchone()[0]
        require(previous in ('ready','active'),'maintenance_review_phase_unknown')
        self.conn.execute(f'UPDATE {OPS} SET phase=?,review_from=NULL WHERE operation_id=?',(previous,operation_id))
        return dict(changed_rows=1,phase=previous)

    def _maintenance_cancel_ready(self,operation_id,root_identity):
        op=self._op(operation_id,root_identity)
        require(op['phase']=='ready' and not self.conn.execute(f'SELECT 1 FROM {RECEIPTS} WHERE operation_id=? LIMIT 1',
                (operation_id,)).fetchone(),'maintenance_cancel_only_before_activation')
        self.conn.execute(f"UPDATE {OPS} SET phase='complete',completion_kind='cancelled_before_fs' WHERE operation_id=?",(operation_id,))
        return dict(changed_rows=1,phase='complete')

    def _new_target(self,target):
        key=path(target).casefold()
        require(not self.conn.execute(f'SELECT 1 FROM {NAMES} n JOIN {OPS} o USING(operation_id) '
            "WHERE o.phase<>'complete' AND (n.path_key=? OR n.canonical_key=?) LIMIT 1",(key,key)).fetchone(),
            'maintenance_descendant_history_requires_review')

    def _maintenance_prepare_admissions(self,operation_id,step_ids,root_identity):
        op=self._op(operation_id,root_identity);require(op['phase']=='active' and 1<=len(step_ids)<=100,'maintenance_admission_limit')
        steps=self._selected_steps(operation_id,step_ids)
        for step_id in step_ids:
            step=steps[step_id];item=step['plan']
            require(item['kind']=='admit' and step['phase']=='planned','maintenance_descendant_not_planned')
            self._new_target(item['target_path'])
            self.conn.execute(f"UPDATE {STEPS} SET phase='admit_intent',intent_json=? WHERE operation_id=? AND step_id=?",
                (encode(dict(target_path=item['target_path'],digest=item['digest'],size=item['size'])).decode('utf-8'),operation_id,step_id))
        return dict(changed_rows=len(step_ids))

    def _maintenance_intent(self,operation_id,leg,reviews,root_identity):
        op=self._op(operation_id,root_identity);require(op['phase']=='active','maintenance_not_active')
        require(leg in ('stage','target') and 1<=len(reviews)<=100,'invalid_maintenance_leg')
        steps=self._selected_steps(operation_id,[item['step_id'] for item in reviews]);result=[]
        for review in reviews:
            step=steps[review['step_id']];item=step['plan']
            require(item['kind']=='relocate' and step['phase']==('planned' if leg=='stage' else 'staged'),
                    'maintenance_leg_order')
            current=state(self.conn,item['copy_id']);old=item['source_path'] if leg=='stage' else item['staging_path']
            target=item['staging_path'] if leg=='stage' else item['target_path']
            file_proof(review['identity'],item['source_identity']['canonical'] if leg=='stage' else old,item['size'])
            require(current['state']['file']['path']==old and current['fingerprint']==review['fingerprint'] and
                    review['digest']==item['digest'] and review['identity']['physical'][:3]==item['source_identity']['physical'][:3],
                    'maintenance_source_proof_changed')
            row=current['state']['file']
            require(not row['is_offline'] and row['file_size']==item['size'] and
                    (row['hash_mod'] or row['hash_original'])==item['digest'],'maintenance_source_changed')
            intent=dict(step_id=step['step_id'],leg=leg,id=item['copy_id'],old_path=old,new_path=target,
                        expected_fingerprint=current['fingerprint'],digest=item['digest'],size=item['size'])
            self.conn.execute(f"UPDATE {STEPS} SET phase=?,intent_json=?,verified_fingerprint=NULL WHERE operation_id=? AND step_id=?",
                (leg+'_intent',encode(intent).decode('utf-8'),operation_id,step['step_id']))
            result.append(intent)
        occupied=indexed_occupants(self.conn,operation_id,sorted({item['new_path'].casefold() for item in result}))
        require(len(occupied)<=100 and not (set(occupied)-{item['id'] for item in result}),
                'maintenance_destination_occupied')
        return dict(changed_rows=len(result),intents=result)

    def _maintenance_ack_relocations(self,operation_id,leg,acknowledgements,root_identity):
        op=self._op(operation_id,root_identity);require(1<=len(acknowledgements)<=100,'maintenance_batch_limit')
        steps=self._selected_steps(operation_id,[item['step_id'] for item in acknowledgements]);values=[];pending=[];results=[]
        for ack in acknowledgements:
            signature=sha(ack);old=self.conn.execute(f'SELECT request_sha,result_json FROM {RECEIPTS} '
                'WHERE operation_id=? AND step_id=? AND leg=?',(operation_id,ack['step_id'],leg)).fetchone()
            if old:
                require(old[0]==signature,'maintenance_ack_payload_conflict');results.append(json.loads(old[1]));continue
            require(op['phase']=='active','maintenance_not_active')
            step=steps[ack['step_id']];item=step['plan'];require(step['phase']==leg+'_intent','maintenance_intent_required')
            intent=json.loads(step['intent'])
            file_proof(ack['identity'],intent['new_path'],item['size'])
            require(ack['digest']==item['digest'] and ack['size']==item['size'] and
                    ack['identity']['canonical']==intent['new_path'] and
                    ack['identity']['physical'][:3]==item['source_identity']['physical'][:3],
                    'maintenance_ack_identity_or_digest_changed')
            values.append(dict(intent,identity=ack['identity']));values[-1].pop('step_id');values[-1].pop('leg')
            pending.append((step,ack,signature))
        if values:
            self.repo._maintenance_relocation_operation=operation_id
            self.repo._maintenance_authority=_AUTHORITY
            try:
                self.repo.commit_maintenance_relocation(values)
                for value in values:self._refresh_name(operation_id,value['id'])
            finally:
                self.repo._maintenance_relocation_operation=None
                self.repo._maintenance_authority=None
        for step,ack,signature in pending:
            current=state(self.conn,step['copy_id']);result=dict(step_id=step['step_id'],leg=leg,
                copy_id=step['copy_id'],fingerprint=current['fingerprint'],path=current['state']['file']['path'])
            self.conn.execute(f'INSERT INTO {RECEIPTS} VALUES(?,?,?,?,?)',
                (operation_id,step['step_id'],leg,signature,encode(result).decode('utf-8')))
            self.conn.execute(f'UPDATE {STEPS} SET phase=?,intent_json=NULL WHERE operation_id=? AND step_id=?',
                ('staged' if leg=='stage' else 'complete',operation_id,step['step_id']))
            results.append(result)
        return dict(changed_rows=len(pending),receipts=results)

    def _maintenance_admit_descendants(self,operation_id,entries,root_identity):
        op=self._op(operation_id,root_identity);require(1<=len(entries)<=100,'maintenance_admission_limit')
        steps=self._selected_steps(operation_id,[item['step_id'] for item in entries]);results=[];changed=0
        for incoming in entries:
            step=steps[incoming['step_id']];item=step['plan'];signature=sha(incoming)
            old=self.conn.execute(f'SELECT request_sha,result_json FROM {RECEIPTS} '
                "WHERE operation_id=? AND step_id=? AND leg='admit'",(operation_id,step['step_id'])).fetchone()
            if old:
                require(old[0]==signature,'maintenance_admission_payload_conflict');results.append(json.loads(old[1]));continue
            require(op['phase']=='active','maintenance_not_active')
            require(item['kind']=='admit' and step['phase']=='admit_intent','maintenance_descendant_intent_required')
            record=incoming['record'];data=record['data'];proof=incoming['proof']
            file_proof(proof['identity'],item['target_path'],item['size'])
            require(path(data['path'])==item['target_path'] and data.get('hash_original')==item['digest'] and
                    data['size']==item['size'] and proof['digest']==item['digest'] and
                    proof['identity']['canonical']==item['target_path'],'maintenance_descendant_proof_changed')
            self._new_target(item['target_path'])
            self.repo.register_files_observed([record])
            copy_id=self.repo.get_file_id(item['target_path']);current=state(self.conn,copy_id)
            require(current['state']['file']['hash_original']==item['digest'] and
                    not current['state']['tags'] and not current['state']['file']['rating'],'maintenance_descendant_inherited_curation')
            result=dict(step_id=step['step_id'],copy_id=copy_id,fingerprint=current['fingerprint'],path=item['target_path'])
            self.conn.execute(f'UPDATE {STEPS} SET phase=\'admitted\',copy_id=?,intent_json=NULL WHERE operation_id=? AND step_id=?',
                (copy_id,operation_id,step['step_id']))
            self.conn.execute(f'INSERT INTO {MEMBERS} VALUES(?,?)',(operation_id,copy_id))
            self._refresh_name(operation_id,copy_id)
            self.conn.execute(f'INSERT INTO {RECEIPTS} VALUES(?,?,?,?,?)',
                (operation_id,step['step_id'],'admit',signature,encode(result).decode('utf-8')));results.append(result);changed+=1
        return dict(changed_rows=changed,receipts=results)

    def _maintenance_verify_batch(self,operation_id,reviews,root_identity):
        op=self._op(operation_id,root_identity);require(op['phase']=='active' and 1<=len(reviews)<=100,'maintenance_verification_limit')
        steps=self._selected_steps(operation_id,[item['step_id'] for item in reviews])
        for review in reviews:
            step=steps[review['step_id']];item=step['plan']
            require(step['phase'] in ('complete','unchanged','admitted'),'maintenance_step_unsettled')
            current=state(self.conn,step['copy_id']);row=current['state']['file']
            file_proof(review['identity'],item['target_path'],item['size'])
            require(current['fingerprint']==review['fingerprint'] and row['path']==item['target_path'] and
                    (row['hash_mod'] or row['hash_original'])==item['digest'] and review['digest']==item['digest'] and
                    review['identity']['canonical']==item['target_path'],'maintenance_final_verification_changed')
            self.conn.execute(f'UPDATE {STEPS} SET verified_fingerprint=? WHERE operation_id=? AND step_id=?',
                (current['fingerprint'],operation_id,step['step_id']))
        return dict(changed_rows=len(reviews))

    def _maintenance_complete(self,operation_id,root_identity):
        op=self._op(operation_id,root_identity);require(op['phase']=='active','maintenance_not_active')
        # Verification is admitted in <=100-row CAS commands. The reservation
        # blocks every generic write to these IDs until this final seal.
        count,invalid=self.conn.execute(f'SELECT count(*),sum(CASE WHEN verified_fingerprint IS NULL '
            "OR intent_json IS NOT NULL OR phase NOT IN ('complete','unchanged','admitted') THEN 1 ELSE 0 END) "
            f'FROM {STEPS} WHERE operation_id=?',(operation_id,)).fetchone()
        require(not invalid,'maintenance_not_verified_or_unsettled')
        require(count==op['expected_steps'],'maintenance_incomplete_plan')
        self.conn.execute(f"UPDATE {OPS} SET phase='complete',completion_kind='verified' WHERE operation_id=?",(operation_id,))
        return dict(changed_rows=1,phase='complete')



from core.library_results import ResultBudget
from core.derived_copies import _budget

PREPARE='prepare_maintenance_operations'
READ_NAMES=frozenset({'get_maintenance_status','get_maintenance_operation','get_maintenance_step',
    'get_maintenance_steps','get_maintenance_receipt','get_maintenance_reservation_snapshot',
    'get_maintenance_copy_states','get_maintenance_scope_members','get_maintenance_pending'})

# Kept exact: constructor prototypes and this explicit preparer must agree.
TABLE_DDL={
 OPS:f'''CREATE TABLE {OPS}(operation_id TEXT PRIMARY KEY,profile_id INTEGER NOT NULL,
            root TEXT NOT NULL,root_identity TEXT NOT NULL,aliases TEXT NOT NULL,plan_sha TEXT NOT NULL,
            expected_steps INTEGER NOT NULL,phase TEXT NOT NULL CHECK(phase IN ('ready','active','review','complete')),
            reason TEXT,completion_kind TEXT,review_from TEXT)''',
 STEPS:f'''CREATE TABLE {STEPS}(operation_id TEXT NOT NULL REFERENCES {OPS}(operation_id),
            ordinal INTEGER NOT NULL,step_id TEXT NOT NULL,plan_json TEXT NOT NULL,phase TEXT NOT NULL,
            copy_id INTEGER,intent_json TEXT,verified_fingerprint TEXT,
            PRIMARY KEY(operation_id,step_id),UNIQUE(operation_id,ordinal))''',
 RECEIPTS:f'''CREATE TABLE {RECEIPTS}(operation_id TEXT NOT NULL,step_id TEXT NOT NULL,leg TEXT NOT NULL,
            request_sha TEXT NOT NULL,result_json TEXT NOT NULL,PRIMARY KEY(operation_id,step_id,leg))''',
 NAMES:f'''CREATE TABLE {NAMES}(operation_id TEXT NOT NULL REFERENCES {OPS}(operation_id),
            copy_id INTEGER NOT NULL,path_key TEXT NOT NULL,canonical_key TEXT,is_offline INTEGER NOT NULL,
            PRIMARY KEY(operation_id,copy_id))''',
 MEMBERS:f'''CREATE TABLE {MEMBERS}(operation_id TEXT NOT NULL REFERENCES {OPS}(operation_id),
            copy_id INTEGER NOT NULL,PRIMARY KEY(operation_id,copy_id))''',
}
INDEX_DDL={
 'panopticon_maintenance_pending_v1':f'CREATE INDEX panopticon_maintenance_pending_v1 ON {OPS}(root_identity,phase)',
 'panopticon_maintenance_path_v1':f'CREATE INDEX panopticon_maintenance_path_v1 ON {NAMES}(operation_id,path_key,is_offline)',
 'panopticon_maintenance_canonical_v1':f'CREATE INDEX panopticon_maintenance_canonical_v1 ON {NAMES}(operation_id,canonical_key,is_offline)',
}


def status(conn):
    canonical=lambda text:' '.join(text.split()).replace('CREATE TABLE IF NOT EXISTS ','CREATE TABLE ').rstrip(';')
    found=[]
    for name,ddl in (TABLE_DDL|INDEX_DDL).items():
        row=conn.execute('SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
        if row is not None:
            table=OPS if name=='panopticon_maintenance_pending_v1' else NAMES
            require(row[:2]==(('index',table) if name in INDEX_DDL else ('table',name)) and canonical(row[2])==canonical(ddl),'maintenance_schema_requires_review')
        found.append(row is not None)
    require(not any(found) or all(found),'maintenance_partial_schema_requires_review')
    return dict(prepared=all(found))


def actor(repo,*,working=False):
    result=object.__new__(MaintenanceOperations)
    result.conn=repo.conn;result.repo=repo
    result.owner_nonce=getattr(repo,'_maintenance_owner_nonce',None)
    current=getattr(repo,'_maintenance_bound_roots',set())
    result.bound_roots=set(current) if working else current
    versions=getattr(repo,'_maintenance_bound_versions',{})
    result.bound_versions=dict(versions) if working else versions
    return result


def prepare(repo):
    require(repo.conn.in_transaction and getattr(repo,'_maintenance_writer',False),'maintenance_owner_transaction_required')
    with _budget(repo.conn):
        if not status(repo.conn)['prepared']:
            for statement in (TABLE_DDL|INDEX_DDL).values():repo.conn.execute(statement)
        require(status(repo.conn)['prepared'],'maintenance_prepare_not_confirmed')
    return True


def guard(repo,operation,args,kwargs):
    repo._maintenance_guard_operation=operation
    remaining=dict(kwargs)
    if operation==PREPARE:
        remaining.pop('_canonical_worker_proofs',None)
        status(repo.conn)
        return remaining
    if not status(repo.conn)['prepared']:
        require(operation not in MAINTENANCE_COMMANDS,'maintenance_schema_not_prepared')
        return remaining
    if (operation in MAINTENANCE_COMMANDS or actor(repo)._ops()) and operation!='maintenance_unbind_scope_owner':
        require(not DerivedPaths(repo.conn)._unknown_triggers(),'maintenance_trigger_footprint_unknown')
    return guard_native_write(repo,operation,args,remaining)


def finish_state(repo,committed):
    pending=getattr(repo,'_maintenance_pending_fences',None)
    if pending is not None and committed:
        with repo._maintenance_context_lock:
            repo._maintenance_bound_roots.clear();repo._maintenance_bound_roots.update(pending)
            repo._maintenance_bound_versions.clear()
            repo._maintenance_bound_versions.update(repo._maintenance_pending_versions)
    repo._maintenance_pending_fences=None
    repo._maintenance_pending_versions=None
    stamp=getattr(repo,'_maintenance_alias_stamp',None)
    stable=MAINTENANCE_COMMANDS|{'add_tag_to_file','remove_tag_from_file','update_file_rating',
        'edit_curation','restore_curation','set_hash_original_bulk'}
    if committed and stamp is not None and getattr(repo,'_maintenance_guard_operation',None) in stable:
        repo._maintenance_alias_stamp=(repo.conn.total_changes,stamp[1])
    else:
        repo._maintenance_alias_stamp=None


def write_command(name):
    def write(repo,*args,**kwargs):
        require(repo.conn.in_transaction and getattr(repo,'_maintenance_writer',False),'maintenance_owner_transaction_required')
        require(status(repo.conn)['prepared'],'maintenance_schema_not_prepared')
        context=actor(repo,working=True)
        with command_budget(repo.conn,name):
            getattr(context,'_'+name)(*args,**kwargs)
        if name in {'maintenance_complete','maintenance_cancel_ready'}:
            context.bound_roots.discard(args[0])
            context.bound_versions.pop(args[0],None)
        # Publish RAM changes only after Service has committed. Rollback leaves
        # the real owner fence unchanged, including failed bind/unbind calls.
        repo._maintenance_pending_fences=context.bound_roots
        repo._maintenance_pending_versions=context.bound_versions
        return True
    write.__name__=name
    return write


@contextmanager
def command_budget(conn,name):
    if name not in {'maintenance_reserve','maintenance_activate','maintenance_bind_scope_owner'}:
        with _budget(conn):yield
        return
    # Reservation captures one full name snapshot; activation streams the seal.
    # Neither runs per file nor performs worker filesystem I/O.
    deadline=time.monotonic()+120
    conn.set_progress_handler(lambda:int(time.monotonic()>=deadline),1000)
    try:
        yield
        require(time.monotonic()<deadline,'maintenance_scope_budget')
    except sqlite3.OperationalError as exc:
        if time.monotonic()>=deadline:raise ValueError('maintenance_scope_budget') from exc
        raise
    finally:
        conn.set_progress_handler(None,0)


def operation(repo,operation_id):
    if not status(repo.conn)['prepared']:return dict(operation=None)
    row=repo.conn.execute(f'SELECT operation_id,profile_id,root,root_identity,aliases,plan_sha,expected_steps,phase,reason,completion_kind '
                         f'FROM {OPS} WHERE operation_id=?',(operation_id,)).fetchone()
    if row is None:return dict(operation=None)
    fields=('operation_id','profile_id','root','root_identity','aliases','plan_sha','expected_steps','phase','reason','completion_kind')
    result=dict(zip(fields,row))
    for name in ('root_identity','aliases'):result[name]=__import__('json').loads(result[name])
    return dict(operation=result)


def step(repo,operation_id,step_id):
    if not status(repo.conn)['prepared']:return dict(step=None)
    import json
    row=repo.conn.execute(f'SELECT ordinal,step_id,plan_json,phase,copy_id,intent_json,verified_fingerprint '
                         f'FROM {STEPS} WHERE operation_id=? AND step_id=?',(operation_id,step_id)).fetchone()
    if row is None:return dict(step=None)
    return dict(step=dict(ordinal=row[0],step_id=row[1],plan=json.loads(row[2]),phase=row[3],copy_id=row[4],
                          intent=json.loads(row[5]) if row[5] else None,verified=row[6]))


def steps(repo,operation_id,*,after_ordinal=None,limit=100):
    require(type(limit) is int and 1<=limit<=100,'maintenance_read_page_limit')
    if not status(repo.conn)['prepared']:return dict(steps=[],next_after=None,has_more=False)
    condition='' if after_ordinal is None else ' AND ordinal>?'
    parameters=(operation_id,) if after_ordinal is None else (operation_id,after_ordinal)
    ids=repo.conn.execute(f'SELECT ordinal,step_id FROM {STEPS} WHERE operation_id=?'+condition+' ORDER BY ordinal LIMIT ?',
                          (*parameters,limit+1)).fetchall()
    result=[];budget=ResultBudget()
    for ordinal,step_id in ids[:limit]:
        record=step(repo,operation_id,step_id)['step'];budget.add(record);result.append(record)
    return dict(steps=result,next_after=ids[limit-1][0] if len(ids)>limit else None,has_more=len(ids)>limit)


def receipt(repo,operation_id,step_id,leg):
    if not status(repo.conn)['prepared']:return dict(receipt=None,request_sha=None)
    import json
    row=repo.conn.execute(f'SELECT request_sha,result_json FROM {RECEIPTS} WHERE operation_id=? AND step_id=? AND leg=?',
                         (operation_id,step_id,leg)).fetchone()
    return dict(receipt=None if row is None else json.loads(row[1]),request_sha=None if row is None else row[0])


def snapshot(repo):
    return dict(snapshot=None if not status(repo.conn)['prepared'] else actor(repo).reservation_snapshot())


def copy_states(repo,copy_ids):
    from core.library_versions import state
    require(type(copy_ids) in (list,tuple) and 1<=len(copy_ids)<=100 and
            all(type(value) is int for value in copy_ids),'maintenance_state_batch_limit')
    require(len(set(copy_ids))==len(copy_ids),'maintenance_state_ids_repeated')
    result=[];budget=ResultBudget()
    for copy_id in copy_ids:
        record=state(repo.conn,copy_id);budget.add(record);result.append(record)
    return dict(records=result)


def scope_members(repo,operation_id,*,after_id=None,limit=100):
    from core.path_identity_registry import prepared as registry_prepared
    require(type(limit) is int and 1<=limit<=100 and (after_id is None or type(after_id) is int),
            'maintenance_member_page_limit')
    if not status(repo.conn)['prepared']:return dict(members=[],next_after=None,has_more=False)
    condition='' if after_id is None else ' AND m.copy_id>?'
    parameters=(operation_id,) if after_id is None else (operation_id,after_id)
    canonical="CASE WHEN p.status='resolved' THEN p.canonical END" if registry_prepared(repo.conn) else 'NULL'
    join=' LEFT JOIN path_identity_records p ON p.file_id=f.id' if canonical!='NULL' else ''
    rows=repo.conn.execute(f'SELECT f.id,f.path,f.is_offline,{canonical} FROM {MEMBERS} m '
        'JOIN files f ON f.id=m.copy_id'+join+' WHERE m.operation_id=?'+condition+' ORDER BY m.copy_id LIMIT ?',
        (*parameters,limit+1)).fetchall()
    return dict(members=[dict(id=row[0],path=row[1],is_offline=row[2],canonical=row[3]) for row in rows[:limit]],
                next_after=rows[limit-1][0] if len(rows)>limit else None,has_more=len(rows)>limit)


def pending(repo,root_identity):
    require(type(root_identity) in (list,tuple) and len(root_identity)==3 and
            all(type(value) is int and value>=0 for value in root_identity),'invalid_root_identity')
    if not status(repo.conn)['prepared']:return dict(operations=[])
    rows=repo.conn.execute(f'SELECT operation_id,root,phase FROM {OPS} WHERE root_identity=? '
        "AND phase IN ('ready','active','review') ORDER BY operation_id LIMIT 101",(json.dumps(list(root_identity)),)).fetchall()
    require(len(rows)<=100,'maintenance_pending_limit')
    result=[];budget=ResultBudget()
    for operation_id,root,phase in rows:
        value=dict(operation_id=operation_id,root=root,phase=phase);budget.add(value);result.append(value)
    return dict(operations=result)



def guard_native_write(repo,operation,args,kwargs):
    remaining=dict(kwargs);proofs=remaining.pop('_canonical_worker_proofs',())
    if operation not in MAINTENANCE_COMMANDS:
        actor(repo).guard(operation,args,remaining,proofs)
    return remaining


def destination_occupants(repo,keys):
    """Indexed targets only inside an authenticated in-flight Native ACK."""
    operation_id=getattr(repo,'_maintenance_relocation_operation',None)
    require(repo.conn.in_transaction and getattr(repo,'_maintenance_writer',False) and
            getattr(repo,'_maintenance_authority',None) is _AUTHORITY,'maintenance_relocation_context_invalid')
    context=actor(repo);op=context._op(operation_id)
    require(op['phase']=='active' and operation_id in context.bound_roots,'maintenance_owner_fence_unbound')
    context._assert_owner_version(operation_id)
    require(1<=len(keys)<=100 and all(key==path(key).casefold() for key in keys),'maintenance_destination_keys_invalid')
    require(all(context._scope(key,op) for key in keys),'maintenance_destination_outside_scope')
    return indexed_occupants(repo.conn,operation_id,keys)


def indexed_occupants(conn,operation_id,keys):
    marks=','.join('?' for _ in keys)
    rows=conn.execute(f'SELECT copy_id FROM {NAMES} WHERE operation_id=? AND is_offline=0 AND path_key IN ({marks}) '
        f'UNION SELECT copy_id FROM {NAMES} WHERE operation_id=? AND is_offline=0 AND canonical_key IN ({marks}) LIMIT 101',
        (operation_id,*keys,operation_id,*keys)).fetchall()
    return [row[0] for row in rows]


def active_aliases(repo):
    """Bounded ambiguity cache; other writes and external commits invalidate it."""
    conn=repo.conn
    stamp=(conn.total_changes,conn.execute('PRAGMA data_version').fetchone()[0])
    if getattr(repo,'_maintenance_alias_stamp',None)!=stamp:
        conn.create_function('maintenance_unicode_key_v1',1,lambda value:path(value).casefold(),deterministic=True)
        sql='SELECT id,maintenance_unicode_key_v1(path) name FROM files WHERE is_offline=0'
        if registry_prepared(conn):
            sql+=" UNION SELECT f.id,maintenance_unicode_key_v1(p.canonical) FROM files f JOIN path_identity_records p "
            sql+="ON p.file_id=f.id WHERE f.is_offline=0 AND p.status='resolved' AND p.canonical IS NOT NULL"
        values=set();used=0
        for (key,) in conn.execute('SELECT name FROM ('+sql+') GROUP BY name HAVING count(*)>1'):
            used+=len(key.encode('utf-8'))+128
            require(used<=4*1024*1024,'maintenance_alias_review_limit')
            values.add(key)
        repo._maintenance_aliases=frozenset(values);repo._maintenance_alias_stamp=stamp
    return repo._maintenance_aliases
