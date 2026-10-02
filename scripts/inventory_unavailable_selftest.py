"""Known absence after backfill; never archive from a failed scan or permission error."""
from contextlib import closing
from pathlib import Path
import sqlite3,sys,threading,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import path_identity_ingest_selftest as helpers
from core.inventory import InventoryStatus,InventoryResult
from core.inventory_unavailable import UnavailableInventoryReader
from core.library_service import LibraryError
from core.path_identity import observe_path
from modules.librarian.logic.indexer import IndexerWorker


class UnavailableInventory(unittest.TestCase):
    setUp=helpers.Ingest.setUp
    image=helpers.Ingest.image
    row=helpers.Ingest.row
    seed=helpers.Ingest.seed
    sql=helpers.Ingest.sql

    def prepare_missing(self,count=1):
        self.present=self.image('Images/present.png');self.seed(self.present)
        self.missing=[self.root/'Images'/f'missing{i}.png' for i in range(count)]
        for start in range(0,count,100):
            self.service.submit('register_files_minimal',[self.row(p) for p in self.missing[start:start+100]]).result(3)
        self.service.submit('edit_curation',self.missing[0].as_posix(),rating=5,add_tags=['keep']).result(3)
        self.service.backfill_path_identity()
        self.before=self.service.read('get_copy_version_state',self.service.read('get_file_id',self.missing[0].as_posix()))['state']

    def run_sync(self,**kwargs):
        worker=IndexerWorker(self.db,[str(self.present.parent)],deep_clean=True,**kwargs)
        worker.run();return worker

    def test_initial_missing_binding_is_reconciled_before_ingestion_preserving_full_curated_history(self):
        self.prepare_missing();worker=self.run_sync()
        self.assertEqual((worker.outcome,worker.archived_count),(InventoryStatus.COMPLETE,1))
        status=self.service.read('get_path_identity_status')
        self.assertEqual((status['pending'],status['unresolved']),(0,0))
        current=self.service.read('get_copy_version_state',self.before['file']['id'])['state']
        self.assertEqual(current,self.before|{'file':self.before['file']|{'is_offline':1}})
        self.assertTrue(self.present.exists());self.assertTrue(self.service.read('check_fts')['consistent'])

    def test_300_missing_bindings_use_multiple_pages_and_commits_without_losing_ids_or_curation(self):
        self.prepare_missing(300);worker=self.run_sync()
        self.assertEqual((worker.outcome,worker.archived_count),(InventoryStatus.COMPLETE,300))
        self.assertEqual(self.sql('SELECT count(*) FROM files WHERE is_offline=1'),[(300,)])
        self.assertEqual(self.service.read('get_path_identity_status')['unresolved'],0)
        self.assertEqual(self.sql("SELECT t.name FROM tags t JOIN file_tags ft ON ft.tag_id=t.id"),[('keep',)])

    def test_incomplete_discovery_never_resolves_missing_bindings(self):
        self.prepare_missing()
        worker=IndexerWorker(self.db,[str(self.present.parent)],deep_clean=True)
        def failed(*args):
            result=InventoryResult();result.fail(InventoryStatus.ERROR,'catalog unreadable');return result
        with patch.object(worker,'_discover_tree',side_effect=failed):worker.run()
        self.assertEqual(worker.archived_count,0);self.assertNotEqual(worker.outcome,InventoryStatus.COMPLETE)
        self.assertEqual(self.sql('SELECT count(*) FROM files WHERE is_offline=1'),[(0,)])

    def test_disconnected_root_never_archives_from_known_error_two(self):
        self.prepare_missing();folder=self.present.parent;folder.rename(folder.with_name('disconnected'))
        worker=IndexerWorker(self.db,[str(folder)],deep_clean=True);worker.run()
        self.assertEqual(worker.archived_count,0);self.assertNotEqual(worker.outcome,InventoryStatus.COMPLETE)
        self.assertEqual(self.sql('SELECT count(*) FROM files WHERE is_offline=1'),[(0,)])

    def test_permission_or_unsupported_binding_blocks_before_any_archive(self):
        for reason,status in (('winerror_5','unavailable'),('identity_reparse_path','unsupported')):
            with self.subTest(reason=reason):
                if not hasattr(self,'missing'):self.prepare_missing(2)
                with closing(sqlite3.connect(self.service.db_path)) as conn:
                    conn.execute('UPDATE path_identity_records SET status=?,reason=? WHERE stored_path=?',(status,reason,self.missing[-1].as_posix()));conn.commit()
                worker=self.run_sync();self.assertEqual(worker.archived_count,0)
                self.assertNotEqual(worker.outcome,InventoryStatus.COMPLETE)
                self.assertEqual(self.sql('SELECT count(*) FROM files WHERE is_offline=1'),[(0,)])

    def test_file_reappearing_is_kept_active_for_explicit_identity_review(self):
        self.prepare_missing();self.missing[0].write_bytes(b'reappeared')
        worker=self.run_sync();self.assertEqual(worker.archived_count,0)
        self.assertEqual(self.sql('SELECT count(*) FROM files WHERE is_offline=1'),[(0,)])
        self.assertTrue(self.missing[0].exists())

    def test_unavailable_parent_does_not_authorize_archival(self):
        self.prepare_missing();path=self.root/'Images/deleted-parent/missing.png'
        self.service.submit('register_files_minimal',[self.row(path)]).result(3);self.service.backfill_path_identity()
        worker=self.run_sync();self.assertEqual(worker.archived_count,0)
        self.assertNotEqual(worker.outcome,InventoryStatus.COMPLETE)
        self.assertEqual(self.sql('SELECT count(*) FROM files WHERE is_offline=1'),[(0,)])

    def test_pre_cancel_and_pending_coverage_preserve_all_copies(self):
        self.prepare_missing();worker=IndexerWorker(self.db,[str(self.present.parent)],deep_clean=True);worker.stop();worker.run()
        self.assertEqual((worker.outcome,worker.archived_count),(InventoryStatus.CANCELLED,0))
        self.service.submit('register_files_minimal',[self.row(self.present.parent/'pending.png')]).result(3)
        worker=self.run_sync();self.assertEqual(worker.archived_count,0)
        self.assertNotEqual(worker.outcome,InventoryStatus.COMPLETE)
        self.assertEqual(self.sql('SELECT count(*) FROM files WHERE is_offline=1'),[(0,)])

    def test_sql_binding_failure_rolls_back_whole_selected_batch(self):
        self.prepare_missing()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute("CREATE TRIGGER block_offline BEFORE UPDATE OF is_offline ON files BEGIN SELECT RAISE(ABORT,'private'); END");conn.commit()
        worker=self.run_sync();self.assertEqual(worker.archived_count,0)
        self.assertNotEqual(worker.outcome,InventoryStatus.COMPLETE)
        self.assertEqual(self.service.read('get_path_identity_status')['unresolved'],1)
        self.assertEqual(self.sql('SELECT count(*) FROM files WHERE is_offline=1'),[(0,)])

    def test_reader_probes_have_no_active_sql_snapshot_and_never_read_image_bytes(self):
        self.prepare_missing();event=threading.Event();observed=observe_path
        def check(path,**kwargs):
            self.assertEqual(self.service.metrics_snapshot()['reads_active'],0)
            return observed(path,**kwargs)
        with patch('core.inventory_unavailable.observe_path',side_effect=check),patch('core.folder_catalogue_scope.observe_path',side_effect=check),patch.object(Path,'open',side_effect=AssertionError('image read')):
            with UnavailableInventoryReader(self.service,self.present.parent.as_posix(),event) as reader:
                value=reader.get_inventory_page(self.present.parent.as_posix(),limit=256,cancel_event=event)
                self.assertEqual(value['rows'],[(self.before['file']['id'],self.missing[0].as_posix())])

    def test_namespace_added_after_preview_rejects_archive_with_curation_unchanged(self):
        self.prepare_missing();event=threading.Event()
        with UnavailableInventoryReader(self.service,self.present.parent.as_posix(),event) as reader:
            value=reader.get_inventory_page(self.present.parent.as_posix(),limit=256,cancel_event=event)
            state=self.service.read('get_copy_version_state',value['rows'][0][0])
            self.service.submit('register_files_minimal',[self.row(self.root/'Other/new.png')]).result(3)
            self.service.backfill_path_identity()
            approved=[dict(path=self.missing[0].as_posix(),copy_id=self.before['file']['id'],fingerprint=state['fingerprint'])]
            with self.assertRaisesRegex(LibraryError,'members_changed'):
                reader.archive_missing_copies(approved,folder=self.present.parent.as_posix(),cancel_event=event)
        self.assertEqual(self.service.read('get_copy_version_state',self.before['file']['id'])['state'],self.before)

    def test_curation_race_and_pending_transfer_reject_the_selected_batch(self):
        self.prepare_missing();event=threading.Event()
        with UnavailableInventoryReader(self.service,self.present.parent.as_posix(),event) as reader:
            value=reader.get_inventory_page(self.present.parent.as_posix(),limit=256,cancel_event=event)
            copy_id=value['rows'][0][0];state=self.service.read('get_copy_version_state',copy_id)
            approved=[dict(path=self.missing[0].as_posix(),copy_id=copy_id,fingerprint=state['fingerprint'])]
            self.service.submit('edit_curation',self.missing[0].as_posix(),add_tags=['changed'],rating=4).result(3)
            with self.assertRaisesRegex(LibraryError,'identity_changed'):
                reader.archive_missing_copies(approved,folder=self.present.parent.as_posix(),cancel_event=event)
            self.service.submit('prepare_transfers').result(3)
            state=self.service.read('get_copy_version_state',copy_id);approved[0]['fingerprint']=state['fingerprint']
            from core.library_transfers import NAME
            with closing(sqlite3.connect(self.service.db_path)) as conn:
                conn.execute('INSERT INTO '+NAME+'(operation_id,source_copy_id,action,source_path,result_path,digest,source_fingerprint,request_fingerprint,phase) VALUES(?,?,?,?,?,?,?,?,?)',
                    ('fixture-pending',copy_id,'move',self.missing[0].as_posix(),(self.root/'target.png').as_posix(),'a'*64,'b'*64,'c'*64,'approved'));conn.commit()
            with self.assertRaisesRegex(LibraryError,'transfer_pending'):
                reader.archive_missing_copies(approved,folder=self.present.parent.as_posix(),cancel_event=event)
        self.assertEqual(self.sql('SELECT count(*) FROM files WHERE is_offline=1'),[(0,)])

    def test_cancel_during_namespace_probe_reports_cancelled_without_any_archive(self):
        self.prepare_missing();worker=IndexerWorker(self.db,[str(self.present.parent)],deep_clean=True)
        def cancel(*args,**kwargs):worker.stop();raise ValueError('identity_cancelled')
        with patch('core.inventory_unavailable.observe_path',side_effect=cancel):worker.run()
        self.assertEqual((worker.outcome,worker.archived_count),(InventoryStatus.CANCELLED,0))
        self.assertEqual(self.sql('SELECT count(*) FROM files WHERE is_offline=1'),[(0,)])

    def test_stop_after_accepted_archive_counts_the_commit_and_keeps_curated_history(self):
        self.prepare_missing();worker=IndexerWorker(self.db,[str(self.present.parent)],deep_clean=True)
        submit=self.service.submit
        def accepted(operation,*args,**kwargs):
            future=submit(operation,*args,**kwargs)
            if operation=='archive_missing_copies':
                future.result(3);worker.stop()
            return future
        with patch.object(self.service,'submit',side_effect=accepted):worker.run()
        self.assertEqual((worker.outcome,worker.archived_count),(InventoryStatus.CANCELLED,1))
        self.assertEqual(self.service.read('get_copy_version_state',self.before['file']['id'])['state'],self.before|{'file':self.before['file']|{'is_offline':1}})

    def test_unavailable_page_uses_one_unrelated_candidate_scan_without_correlated_subquery(self):
        self.prepare_missing();event=threading.Event()
        from core.library_repository import LibraryRepository
        with UnavailableInventoryReader(self.service,self.present.parent.as_posix(),event) as reader,closing(sqlite3.connect(self.service.db_path)) as conn:
            queries=[];conn.set_trace_callback(queries.append)
            repo=LibraryRepository(conn)
            value=repo.get_unavailable_inventory_page(self.present.parent.as_posix(),scope=reader.scope,limit=256)
            self.assertEqual(len(value['rows']),1)
            selects=[query for query in queries if 'SELECT max(id) FROM files' in query or 'SELECT id, path FROM files WHERE' in query]
            self.assertEqual(len(selects),2)
            for query in selects:
                plan=conn.execute('EXPLAIN QUERY PLAN '+query).fetchall()
                self.assertFalse(any('CORRELATED' in str(row).upper() for row in plan),plan)


if __name__=='__main__':unittest.main(verbosity=2)
