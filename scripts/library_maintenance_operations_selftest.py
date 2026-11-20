"""Windows Native ledger, SQLite rollback, physical identity and scope fences."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
HERE=ROOT/'.work';HERE.mkdir(exist_ok=True)
from core.maintenance_backend import MaintenanceBackend as Backend
from core.library_maintenance_operations import status,TABLE_DDL,OPS
from core.library_repository import LibraryRepository
from core.library_service import LibraryService
from types import SimpleNamespace
def make_namespaces():return SimpleNamespace(Service=LibraryService,Repository=LibraryRepository)
from core.library_maintenance_operations import plan_hash
from core.pinned_move import DirectoryPins
from core.library_connections import open_reader
from core.library_service import LibraryError
from core.path_identity import observe_path


class ServiceIntegration(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='private-service-',dir=HERE);self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name);self.root=self.base/'artist';self.root.mkdir()
        self.other=self.base/'other';self.other.mkdir();self.database=self.base/'native.db'
        self.source=self.root/'Image_00005.png';self.source.write_bytes(b'physical native modified bytes')
        self.outside=self.other/'Outside.png';self.outside.write_bytes(b'outside')
        self.namespaces=make_namespaces();self.service=self.namespaces.Service(self.database);self.backend=Backend(self.service)
        def close():
            if self.service is not None:self.service.close();self.service=None
        self.addCleanup(close)
        self.pins=DirectoryPins();self.addCleanup(self.pins.close)
        root=self.pins.pin(self.root);raw=bytes.fromhex(root['file_id'])
        self.root_identity=[root['volume'],int.from_bytes(raw[:8],'little'),int.from_bytes(raw[8:],'little')]
        self.root_record=root;self.opid=str(uuid4());self.target=self.root/'Image_00001.png'
        self.staging=self.root/'.cherry-maintenance'/self.opid/'staging'/'image.png'
        self.digest=hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.backend.write('register_files_minimal',[dict(path=item.as_posix(),filename=item.name,size=item.stat().st_size,
            created=item.stat().st_birthtime,hash_original=hashlib.sha256(item.read_bytes()).hexdigest())
            for item in (self.source,self.outside)])
        self.key=self.backend.read('get_file_id',self.source.as_posix())
        self.backend.write('edit_curation',self.source.as_posix(),rating=5,add_tags=['preserved'])
        self.before=self.backend.read('get_copy_version_state',self.key)

    def prepare(self):
        self.assertFalse(self.backend.read('get_maintenance_status')['prepared'])
        self.backend.write('prepare_maintenance_operations')
        self.assertTrue(self.backend.read('get_maintenance_status')['prepared'])

    def claim(self):
        return dict(owner_nonce=self.backend.owner_nonce,kind='directory',handles_held=self.pins.active,
            canonical=Path(self.root_record['path']).as_posix(),physical=self.root_identity)

    def proof(self,item):
        observed=observe_path(str(item))
        return dict(path=item.as_posix(),canonical=observed.canonical,physical=list(observed.physical),pins_held=True,
            owner_nonce=self.backend.owner_nonce,reservation_snapshot=self.backend.read('get_maintenance_reservation_snapshot')['snapshot'])

    def plan(self,kind='relocate'):
        observed=observe_path(str(self.source))
        return dict(kind=kind,step_id='one',copy_id=self.key,source_path=self.source.as_posix(),
            source_fingerprint=self.before['fingerprint'],source_identity=dict(canonical=observed.canonical,physical=list(observed.physical)),
            staging_path=self.staging.as_posix(),target_path=self.target.as_posix() if kind=='relocate' else self.source.as_posix(),
            digest=self.digest,catalog_digest='a'*64,size=self.source.stat().st_size)

    def reserve(self,entries):
        self.backend.write('maintenance_reserve',self.opid,1,self.root.as_posix(),self.root_identity,
            [dict(path=self.root.as_posix(),root_identity=self.root_identity)],plan_hash(entries),len(entries))

    def load(self,entries):
        self.reserve(entries)
        self.backend.write('maintenance_bind_scope_owner',self.opid,self.claim())
        self.backend.write('maintenance_plan_batch',self.opid,list(enumerate(entries)),self.root_identity)
        self.backend.write('maintenance_activate',self.opid,self.root_identity)

    def test_no_maintenance_schema_on_start_or_read_and_explicit_prepare_backed_up(self):
        with closing(open_reader(self.database)) as conn:self.assertFalse(status(conn)['prepared'])
        self.assertEqual(self.backend.read('get_maintenance_operation',self.opid),dict(operation=None))
        self.prepare()
        backups=list(self.base.glob('native.db.pre-maintenance-operations-*.bak'));self.assertEqual(len(backups),1)
        with closing(open_reader(backups[0])) as conn:self.assertFalse(status(conn)['prepared'])
        self.backend.write('prepare_maintenance_operations')
        self.assertEqual(len(list(self.base.glob('native.db.pre-maintenance-operations-*.bak'))),1)

    def test_prepare_ddl_rollback_has_no_executescript_implicit_commit(self):
        original=self.namespaces.Repository.prepare_maintenance_operations
        def fail(repo):original(repo);raise RuntimeError('fail after maintenance DDL')
        self.namespaces.Repository.prepare_maintenance_operations=fail
        try:
            with self.assertRaisesRegex(LibraryError,'fail after maintenance DDL'):
                self.backend.write('prepare_maintenance_operations')
        finally:self.namespaces.Repository.prepare_maintenance_operations=original
        with closing(open_reader(self.database)) as conn:self.assertFalse(status(conn)['prepared'])
        self.assertEqual(self.backend.read('get_copy_version_state',self.key),self.before)

    def test_nonce_shared_with_writer_and_reader_repo_never_mints_new_nonce(self):
        self.prepare();self.reserve([])
        with closing(open_reader(self.database)) as conn:
            reader=self.namespaces.Repository(conn)
            self.assertIsNone(reader._maintenance_owner_nonce)
        self.backend.write('maintenance_bind_scope_owner',self.opid,self.claim())
        self.assertEqual(self.service.maintenance_context()['owner_nonce'],self.backend.owner_nonce)
        self.assertEqual(self.service.maintenance_context()['bound_operations'],[self.opid])
        self.assertEqual(self.backend.read('get_maintenance_operation',self.opid)['operation']['phase'],'ready')

    def test_ram_binding_is_not_published_on_rolled_back_command(self):
        self.prepare();self.reserve([])
        original=self.namespaces.Repository.maintenance_bind_scope_owner
        def fail(repo,*args,**kwargs):original(repo,*args,**kwargs);raise RuntimeError('fail after staged RAM bind')
        self.namespaces.Repository.maintenance_bind_scope_owner=fail
        try:
            with self.assertRaisesRegex(LibraryError,'staged RAM bind'):
                self.backend.write('maintenance_bind_scope_owner',self.opid,self.claim())
        finally:self.namespaces.Repository.maintenance_bind_scope_owner=original
        self.assertEqual(self.service.maintenance_context()['bound_operations'],[])
        with self.assertRaisesRegex(LibraryError,'fence_unbound'):
            self.backend.write('edit_curation',self.outside.as_posix(),rating=1,_canonical_worker_proofs=[self.proof(self.outside)])

    def test_private_queue_moves_ack_receipts_and_boolean_results(self):
        self.prepare();self.load([self.plan()])
        for leg,source,target in [('stage',self.source,self.staging),('target',self.staging,self.target)]:
            observed=observe_path(str(source));current=self.backend.read('get_copy_version_state',self.key)
            reviews=[dict(step_id='one',fingerprint=current['fingerprint'],digest=self.digest,
                identity=dict(canonical=observed.canonical,physical=list(observed.physical)))]
            self.assertIs(self.backend.write('maintenance_intent',self.opid,leg,reviews,self.root_identity),True)
            step=self.backend.read('get_maintenance_step',self.opid,'one')['step']
            self.assertEqual(step['intent']['new_path'],target.as_posix())
            target.parent.mkdir(parents=True,exist_ok=True);os.rename(source,target)
            published=observe_path(str(target));ack=dict(step_id='one',digest=self.digest,size=target.stat().st_size,
                identity=dict(canonical=published.canonical,physical=list(published.physical)))
            self.assertIs(self.backend.write('maintenance_ack_relocations',self.opid,leg,[ack],self.root_identity),True)
            receipt=self.backend.read('get_maintenance_receipt',self.opid,'one',leg)
            self.assertEqual(receipt['receipt']['copy_id'],self.key)
            self.backend.write('maintenance_ack_relocations',self.opid,leg,[ack],self.root_identity)
            self.assertEqual(self.backend.read('get_maintenance_receipt',self.opid,'one',leg),receipt)
        observed=observe_path(str(self.target));current=self.backend.read('get_copy_version_state',self.key)
        review=dict(step_id='one',fingerprint=current['fingerprint'],digest=self.digest,
                    identity=dict(canonical=observed.canonical,physical=list(observed.physical)))
        self.backend.write('maintenance_verify_batch',self.opid,[review],self.root_identity)
        self.backend.write('maintenance_complete',self.opid,self.root_identity)
        self.assertEqual(self.service.maintenance_context()['bound_operations'],[])
        self.assertEqual(self.backend.read('get_maintenance_operation',self.opid)['operation']['phase'],'complete')
        self.assertEqual(current['state']['tags'],self.before['state']['tags'])

    def test_private_queue_reopen_has_new_nonce_and_still_holds_scope(self):
        self.prepare();self.load([self.plan('unchanged')]);old=self.claim()
        self.service.close();self.service=self.namespaces.Service(self.database,require_ready=True);self.backend=Backend(self.service)
        self.assertNotEqual(self.backend.owner_nonce,old['owner_nonce'])
        with self.assertRaisesRegex(LibraryError,'nonce_changed'):
            self.backend.write('maintenance_bind_scope_owner',self.opid,old)
        with self.assertRaisesRegex(LibraryError,'fence_unbound'):
            self.backend.write('edit_curation',self.outside.as_posix(),rating=1,_canonical_worker_proofs=[self.proof(self.outside)])
        self.backend.write('maintenance_bind_scope_owner',self.opid,self.claim())
        self.backend.write('edit_curation',self.outside.as_posix(),rating=1,_canonical_worker_proofs=[self.proof(self.outside)])

    def test_bounded_copy_state_reader_and_scope_member_pagination(self):
        self.prepare();self.reserve([])
        expected=self.backend.read('get_copy_version_state',self.key)
        self.assertEqual(self.backend.read('get_maintenance_copy_states',[self.key]),dict(records=[expected]))
        members=self.backend.read('get_maintenance_scope_members',self.opid,limit=1)
        self.assertEqual([record['id'] for record in members['members']],[self.key])
        self.assertFalse(members['has_more'])
        with self.assertRaisesRegex(ValueError,'state_batch_limit'):
            self.backend.read('get_maintenance_copy_states',list(range(101)))


    def test_scoped_ack_uses_index_without_global_path_udf(self):
        from unittest.mock import patch
        with patch('core.library_maintenance._keys',side_effect=AssertionError('global UDF forbidden in scoped ACK')):
            self.test_private_queue_moves_ack_receipts_and_boolean_results()

    def test_prepared_identity_same_sha_replacement_needs_review(self):
        self.backend.write('prepare_path_identity')
        saved=observe_path(str(self.source))
        self.backend.write('store_path_identity_batch',[dict(file_id=self.key,stored_path=self.source.as_posix(),
            is_offline=0,status='resolved',canonical=saved.canonical,physical=list(saved.physical),reason=None)])
        replacement=self.source.with_suffix('.replacement');replacement.write_bytes(self.source.read_bytes())
        os.replace(replacement,self.source)
        fresh=observe_path(str(self.source));self.assertNotEqual(fresh.physical[:3],saved.physical[:3])
        self.prepare();entry=self.plan();self.reserve([entry])
        self.backend.write('maintenance_bind_scope_owner',self.opid,self.claim())
        with self.assertRaisesRegex(LibraryError,'source_physical_requires_review'):
            self.backend.write('maintenance_plan_batch',self.opid,[(0,entry)],self.root_identity)
        self.assertIsNone(self.backend.read('get_maintenance_step',self.opid,'one')['step'])
        self.assertEqual(self.backend.read('get_copy_version_state',self.key),self.before)

    def test_all_production_writes_have_explicit_guard_policy(self):
        from core.library_service import WRITES
        from core.library_maintenance_operations import RESOLVERS,MAINTENANCE_COMMANDS,PREPARE
        self.assertEqual(WRITES-set(RESOLVERS)-MAINTENANCE_COMMANDS-{PREPARE},set())

    def test_destination_lookup_rejects_forged_operation_attribute(self):
        from core.library_maintenance_operations import destination_occupants
        self.prepare();self.load([self.plan()])
        conn=sqlite3.connect(self.database,isolation_level=None)
        try:
            conn.execute('BEGIN')
            repo=LibraryRepository(conn);repo._maintenance_writer=True
            repo._maintenance_relocation_operation=self.opid
            repo._maintenance_bound_roots={self.opid}
            with self.assertRaisesRegex(ValueError,'context_invalid'):
                destination_occupants(repo,[self.target.as_posix().casefold()])
        finally:conn.rollback();conn.close()

    def test_pending_discovery_retains_review_and_hides_cancelled(self):
        self.assertEqual(self.backend.read('get_maintenance_pending',self.root_identity),dict(operations=[]))
        self.prepare();self.reserve([])
        expected=dict(operation_id=self.opid,root=self.root.as_posix(),phase='ready')
        self.assertEqual(self.backend.read('get_maintenance_pending',self.root_identity),dict(operations=[expected]))
        self.backend.write('maintenance_mark_review',self.opid,'worker stopped before activation')
        expected['phase']='review'
        self.assertEqual(self.backend.read('get_maintenance_pending',self.root_identity),dict(operations=[expected]))
        self.backend.write('maintenance_bind_scope_owner',self.opid,self.claim())
        self.backend.write('maintenance_resume',self.opid,self.root_identity,plan_hash([]))
        self.backend.write('maintenance_cancel_ready',self.opid,self.root_identity)
        self.assertEqual(self.backend.read('get_maintenance_pending',self.root_identity),dict(operations=[]))

    def test_scoped_names_and_native_rollback_together_after_ack_failure(self):
        from unittest.mock import patch
        from core.library_maintenance_operations import MaintenanceOperations,NAMES
        self.prepare();self.load([self.plan()])
        observed=observe_path(str(self.source));current=self.backend.read('get_copy_version_state',self.key)
        review=dict(step_id='one',fingerprint=current['fingerprint'],digest=self.digest,
            identity=dict(canonical=observed.canonical,physical=list(observed.physical)))
        self.backend.write('maintenance_intent',self.opid,'stage',[review],self.root_identity)
        self.staging.parent.mkdir(parents=True);os.rename(self.source,self.staging)
        published=observe_path(str(self.staging));ack=dict(step_id='one',digest=self.digest,size=self.staging.stat().st_size,
            identity=dict(canonical=published.canonical,physical=list(published.physical)))
        original=MaintenanceOperations._refresh_name
        def fail_after_refresh(context,*args):
            original(context,*args);raise RuntimeError('failure after indexed name refresh')
        with patch.object(MaintenanceOperations,'_refresh_name',fail_after_refresh):
            with self.assertRaisesRegex(LibraryError,'failure after indexed name refresh'):
                self.backend.write('maintenance_ack_relocations',self.opid,'stage',[ack],self.root_identity)
        self.assertEqual(self.backend.read('get_copy_version_state',self.key),self.before)
        with closing(open_reader(self.database)) as conn:
            self.assertEqual(conn.execute(f'SELECT path_key FROM {NAMES} WHERE copy_id=?',(self.key,)).fetchone(),
                             (self.source.as_posix().casefold(),))
        self.assertIsNone(self.backend.read('get_maintenance_receipt',self.opid,'one','stage')['receipt'])
        self.backend.write('maintenance_ack_relocations',self.opid,'stage',[ack],self.root_identity)
        self.assertEqual(self.backend.read('get_copy_version_state',self.key)['state']['file']['path'],self.staging.as_posix())

    def _intent_for_file(self,leg,source):
        observed=observe_path(str(source));current=self.backend.read('get_copy_version_state',self.key)
        return dict(step_id='one',fingerprint=current['fingerprint'],digest=self.digest,
            identity=dict(canonical=observed.canonical,physical=list(observed.physical)))

    def _stage_file(self):
        self.prepare();self.load([self.plan()])
        self.backend.write('maintenance_intent',self.opid,'stage',[self._intent_for_file('stage',self.source)],self.root_identity)
        self.staging.parent.mkdir(parents=True);os.rename(self.source,self.staging)
        observed=observe_path(str(self.staging))
        self.backend.write('maintenance_ack_relocations',self.opid,'stage',[dict(step_id='one',digest=self.digest,
            size=self.staging.stat().st_size,identity=dict(canonical=observed.canonical,physical=list(observed.physical)))],self.root_identity)

    def _external_target_alias(self):
        key=self.backend.read('get_file_id',self.outside.as_posix())
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute('UPDATE files SET path=?,filename=? WHERE id=?',
                         (self.target.as_posix().upper(),self.target.name,key));conn.commit()
        return key

    def _assert_one_target_owner(self):
        with closing(open_reader(self.database)) as conn:
            owners=[key for key,stored in conn.execute('SELECT id,path FROM files WHERE is_offline=0')
                    if stored.casefold()==self.target.as_posix().casefold()]
        self.assertEqual(len(owners),1)
        self.assertNotIn(self.key,owners)

    def test_external_case_alias_invalidates_bound_owner_before_intent(self):
        self._stage_file();self._external_target_alias()
        with self.assertRaisesRegex(LibraryError,'external_sql_change_requires_review'):
            self.backend.write('maintenance_intent',self.opid,'target',
                [self._intent_for_file('target',self.staging)],self.root_identity)
        self.assertTrue(self.staging.is_file());self.assertFalse(self.target.exists())
        self._assert_one_target_owner()

    def test_external_commit_between_intent_and_ack_keeps_native_unacknowledged(self):
        self._stage_file()
        self.backend.write('maintenance_intent',self.opid,'target',
            [self._intent_for_file('target',self.staging)],self.root_identity)
        os.rename(self.staging,self.target);self._external_target_alias()
        observed=observe_path(str(self.target));ack=dict(step_id='one',digest=self.digest,size=self.target.stat().st_size,
            identity=dict(canonical=observed.canonical,physical=list(observed.physical)))
        with self.assertRaisesRegex(LibraryError,'external_sql_change_requires_review'):
            self.backend.write('maintenance_ack_relocations',self.opid,'target',[ack],self.root_identity)
        self.assertIsNone(self.backend.read('get_maintenance_receipt',self.opid,'one','target')['receipt'])
        self.assertEqual(self.backend.read('get_copy_version_state',self.key)['state']['file']['path'],self.staging.as_posix())
        self._assert_one_target_owner()

    def test_rebind_refreshes_names_after_external_case_alias(self):
        self._stage_file();self.backend.write('maintenance_unbind_scope_owner',self.opid)
        outsider=self._external_target_alias()
        self.backend.write('maintenance_bind_scope_owner',self.opid,self.claim())
        members=self.backend.read('get_maintenance_scope_members',self.opid)['members']
        self.assertIn(outsider,{item['id'] for item in members})
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.backend.write('maintenance_intent',self.opid,'target',
                [self._intent_for_file('target',self.staging)],self.root_identity)
        self.assertTrue(self.staging.is_file());self._assert_one_target_owner()

    def test_restart_bind_refreshes_names_after_external_case_alias(self):
        self._stage_file();self._external_target_alias()
        self.service.close();self.service=self.namespaces.Service(self.database,require_ready=True);self.backend=Backend(self.service)
        self.backend.write('maintenance_bind_scope_owner',self.opid,self.claim())
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.backend.write('maintenance_intent',self.opid,'target',
                [self._intent_for_file('target',self.staging)],self.root_identity)
        self.assertTrue(self.staging.is_file());self._assert_one_target_owner()

    def test_fresh_bind_cannot_reuse_old_verification_after_external_commit(self):
        self.prepare();self.load([self.plan('unchanged')])
        review=self._intent_for_file('verify',self.source)
        self.backend.write('maintenance_verify_batch',self.opid,[review],self.root_identity)
        self.backend.write('maintenance_unbind_scope_owner',self.opid)
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute('UPDATE files SET rating=2 WHERE id=?',(self.key,));conn.commit()
        self.backend.write('maintenance_bind_scope_owner',self.opid,self.claim())
        self.assertIsNone(self.backend.read('get_maintenance_step',self.opid,'one')['step']['verified'])
        with self.assertRaisesRegex(LibraryError,'not_verified_or_unsettled'):
            self.backend.write('maintenance_complete',self.opid,self.root_identity)
        self.backend.write('maintenance_verify_batch',self.opid,[self._intent_for_file('verify',self.source)],self.root_identity)
        self.backend.write('maintenance_complete',self.opid,self.root_identity)

    def test_fresh_bind_rejects_unknown_trigger_before_snapshot_writes(self):
        from core.library_maintenance_operations import NAMES
        self.prepare();self.reserve([])
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute(f'CREATE TRIGGER foreign_scope_trigger AFTER INSERT ON {NAMES} '
                         'BEGIN UPDATE files SET rating=1; END');conn.commit()
        with self.assertRaisesRegex(LibraryError,'trigger_footprint_unknown'):
            self.backend.write('maintenance_bind_scope_owner',self.opid,self.claim())
        self.assertEqual(self.backend.read('get_copy_version_state',self.key),self.before)
        self.assertEqual(self.service.maintenance_context()['bound_operations'],[])


if __name__=='__main__':unittest.main(verbosity=2)

