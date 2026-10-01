"""Archivado de inventario por ID/huella; ninguna versión nueva se retira por ruta."""
from contextlib import closing
from copy import deepcopy
import hashlib,sqlite3,sys,unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.inventory_archive import archive_missing
from core.inventory import InventoryStatus as Status
from modules.librarian.logic import indexer
from modules.librarian.module import LibrarianModule
from core.paths import ProjectPaths
from PySide6.QtWidgets import QMessageBox
import indexer_safety_selftest as prior


class Archive(prior.IndexerSafety):
    def prepare(self,names=('a.png','b.png')):
        self.paths=[self.folder/name for name in names]
        self.before=[self.seed(path) for path in self.paths]
        current=[]
        for start in range(0,len(self.paths),256):
            current.extend(self.db.get_duplicate_snapshot([path.as_posix() for path in self.paths[start:start+256]]))
        self.approved=[dict(path=c['path'],copy_id=c['state']['file']['id'],fingerprint=c['fingerprint']) for c in current]
        self.states=current
        return current

    def archive(self,approved=None,folder=None):
        return self.db.archive_missing_copies(self.approved if approved is None else approved,
            folder=self.folder.as_posix() if folder is None else folder)

    def assert_active(self):
        for path,before in zip(self.paths,self.before):
            self.assert_preserved(path,before);self.assertEqual(self.db.get_file_id(path.as_posix()),before[0])

    def test_owner_archives_only_offline_flag_and_preserves_all_fields_fts_and_receipts(self):
        self.prepare();receipt=self.folder/'catalog.db';receipt.write_bytes(b'portable Cherry receipts')
        self.assertTrue(self.archive())
        for copy in self.states:
            now=self.db.get_copy_version_state(copy['state']['file']['id'])
            expected=deepcopy(copy['state']);expected['file']['is_offline']=1
            self.assertEqual(now['state'],expected)
        self.assertEqual(self.inspect_conn.execute('SELECT count(*) FROM files_fts').fetchone()[0],2)
        self.assertEqual(receipt.read_bytes(),b'portable Cherry receipts')

    def test_state_change_in_any_member_rejects_entire_block(self):
        self.prepare();self.db.add_tag_to_file(self.paths[1].as_posix(),'late')
        self.assertFalse(self.archive())
        for path,before in zip(self.paths,self.before):self.assertEqual(self.db.get_file_id(path.as_posix()),before[0])
        self.assertIn('late',self.db.get_tags_for_file(self.paths[1].as_posix()))

    def test_wrong_id_or_fingerprint_duplicate_and_outside_folder_reject_before_update(self):
        self.prepare()
        for change in ('id','fingerprint','duplicate','outside','oversized'):
            approved=deepcopy(self.approved)
            if change=='id':approved[1]['copy_id']=999999
            if change=='fingerprint':approved[1]['fingerprint']='0'*64
            if change=='duplicate':approved.append(approved[0])
            if change=='outside':approved[1]['path']=(self.root/'outside.png').as_posix()
            if change=='oversized':approved=approved*129
            self.assertFalse(self.archive(approved));self.assert_active()

    def test_second_update_failure_rolls_back_entire_block_and_fts(self):
        self.prepare()
        self.inspect_conn.execute(f"CREATE TRIGGER fail_second BEFORE UPDATE OF is_offline ON files WHEN OLD.id={self.before[1][0]} BEGIN SELECT RAISE(ABORT,'fixture'); END")
        self.inspect_conn.commit()
        self.assertFalse(self.archive());self.assert_active()
        self.assertEqual(self.inspect_conn.execute('SELECT count(*) FROM files_fts').fetchone()[0],2)

    def test_requires_owner_transaction_and_uses_one_update_without_fts_or_per_copy_queries(self):
        self.prepare()
        with closing(sqlite3.connect(self.db.db_path)) as conn:
            with self.assertRaisesRegex(RuntimeError,'requires_transaction'):
                archive_missing(conn,self.approved,folder=self.folder.as_posix(),normalize=lambda p:p)
            conn.execute('BEGIN IMMEDIATE');trace=[];conn.set_trace_callback(trace.append)
            self.assertTrue(archive_missing(conn,self.approved,folder=self.folder.as_posix(),normalize=lambda p:p))
            self.assertEqual(len([s for s in trace if s.startswith('UPDATE files SET is_offline')]),1)
            self.assertEqual(len([s for s in trace if s.startswith('SELECT')]),3)
            self.assertFalse(any('files_fts' in s or s.startswith(('INSERT','DELETE','CREATE')) for s in trace))
            conn.rollback()
        self.assert_active()

    def test_new_native_version_between_worker_snapshot_and_owner_remains_active(self):
        self.prepare(('a.png',));archive=self.db.archive_missing_copies
        def new_version(*args,**kwargs):
            path=self.paths[0];path.write_bytes(b'new copy')
            reviewed=self.db.get_copy_version_state(self.before[0][0])
            self.assertTrue(self.db.make_copy_online(self.before[0][0],expected_fingerprint=reviewed['fingerprint'],
                digest=hashlib.sha256(b'new copy').hexdigest(),observation=dict(filename=path.name,size=8,created=2)))
            return archive(*args,**kwargs)
        with patch.object(self.db,'archive_missing_copies',side_effect=new_version):worker=self.run_worker()
        self.assertEqual(worker.outcome,Status.ERROR);self.assertEqual(worker.archived_count,0)
        current=self.db.get_file_id(self.paths[0].as_posix())
        self.assertIsNotNone(current);self.assertNotEqual(current,self.before[0][0])
        self.assertEqual(self.paths[0].read_bytes(),b'new copy');self.assert_preserved(self.paths[0],self.before[0])
        self.assertEqual(self.db.get_tags_for_file(self.paths[0].as_posix()),[])

    def test_reappeared_file_after_snapshot_is_skipped_without_archival(self):
        self.prepare(('a.png',));read=self.db.get_duplicate_snapshot
        def reappeared(*args,**kwargs):
            result=read(*args,**kwargs);self.paths[0].write_bytes(b'reappeared');return result
        with patch.object(self.db,'get_duplicate_snapshot',side_effect=reappeared):worker=self.run_worker()
        self.assertEqual(worker.outcome,Status.COMPLETE);self.assertEqual(worker.archived_count,0);self.assert_active()

    def test_false_commit_and_late_curation_change_are_visible_and_preserve_active_version(self):
        self.prepare(('a.png',))
        with patch.object(self.db,'archive_missing_copies',return_value=False):worker=self.run_worker()
        self.assertEqual(worker.outcome,Status.ERROR);self.assertEqual(worker.archived_count,0);self.assert_active()
        archive=self.db.archive_missing_copies
        def curated(*args,**kwargs):self.db.update_file_rating(self.paths[0].as_posix(),4);return archive(*args,**kwargs)
        with patch.object(self.db,'archive_missing_copies',side_effect=curated):worker=self.run_worker()
        self.assertEqual(worker.outcome,Status.ERROR);self.assertEqual(worker.archived_count,0)
        self.assertEqual(self.db.get_file_rating(self.paths[0].as_posix()),4)
        self.assertEqual(self.db.get_file_id(self.paths[0].as_posix()),self.before[0][0])

    def test_cancel_after_snapshot_admits_no_archival_but_admitted_commit_is_counted(self):
        self.prepare(('a.png',));worker=indexer.IndexerWorker(self.db,[str(self.folder)],deep_clean=True)
        read=self.db.get_duplicate_snapshot
        def cancel(*args,**kwargs):result=read(*args,**kwargs);worker.stop();return result
        with patch.object(self.db,'get_duplicate_snapshot',side_effect=cancel):worker.run()
        self.assertEqual(worker.outcome,Status.CANCELLED);self.assertEqual(worker.archived_count,0);self.assert_active()
        worker=indexer.IndexerWorker(self.db,[str(self.folder)],deep_clean=True);archive=self.db.archive_missing_copies
        def admitted(*args,**kwargs):result=archive(*args,**kwargs);worker.stop();return result
        with patch.object(self.db,'archive_missing_copies',side_effect=admitted):worker.run()
        self.assertEqual(worker.outcome,Status.CANCELLED);self.assertEqual(worker.archived_count,1)
        self.assert_preserved(self.paths[0],self.before[0]);self.assertIsNone(self.db.get_file_id(self.paths[0].as_posix()))

    def test_failed_admitted_command_is_not_hidden_by_concurrent_cancel(self):
        self.prepare(('a.png',));worker=indexer.IndexerWorker(self.db,[str(self.folder)],deep_clean=True)
        def failed(*args,**kwargs):worker.stop();return False
        with patch.object(self.db,'archive_missing_copies',side_effect=failed):worker.run()
        self.assertEqual(worker.outcome,Status.ERROR);self.assertEqual(worker.archived_count,0);self.assert_active()
        self.paths[0].write_bytes(b'present');worker=indexer.IndexerWorker(self.db,[str(self.folder)],deep_clean=True)
        def failed_ingest(*args,**kwargs):worker.stop();return False
        with patch.object(self.db,'register_files_minimal',side_effect=failed_ingest):worker.run()
        self.assertEqual(worker.outcome,Status.ERROR);self.assertEqual(worker.archived_count,0);self.assert_active()

    def test_later_block_failure_preserves_confirmed_blocks_and_does_not_hide_partial_count(self):
        self.prepare(tuple(f'{index:03d}.png' for index in range(260)))
        archive=self.db.archive_missing_copies;calls=[]
        def second(*args,**kwargs):
            calls.append(len(args[0]));return archive(*args,**kwargs) if len(calls)==1 else False
        with patch.object(self.db,'archive_missing_copies',side_effect=second):worker=self.run_worker()
        self.assertEqual(calls,[256,4]);self.assertEqual(worker.outcome,Status.ERROR);self.assertEqual(worker.archived_count,256)
        self.assertEqual(self.inspect_conn.execute('SELECT count(*) FROM files WHERE is_offline=1').fetchone()[0],256)
        self.assertEqual(self.inspect_conn.execute('SELECT count(*) FROM file_tags').fetchone()[0],260)

    def test_gui_final_dialog_and_directed_scan_show_confirmed_partial_counts(self):
        with patch.object(ProjectPaths,'root',return_value=self.root):
            module=LibrarianModule();module.on_load(dict(data_service=self.db.service));view=module.get_view()
            module._auto_scan_timer.stop()
            try:
                worker=SimpleNamespace(outcome=Status.ERROR,archived_count=256)
                module.indexer_thread=module._reindex_thread=worker
                with patch.object(module,'refresh_ui'),patch.object(QMessageBox,'warning') as warning:
                    module.scan_finished();module._quiet_reindex_finished()
                self.assertIn('256',warning.call_args.args[2]);self.assertIn('256',module.lbl_status.text())
                self.assertIn('Confirmed batches remain',warning.call_args.args[2])
                worker.outcome=Status.COMPLETE
                with patch.object(module,'refresh_ui'),patch.object(QMessageBox,'information') as done:
                    module.scan_finished()
                self.assertIn('256',done.call_args.args[2])
            finally:
                module.indexer_thread=module._reindex_thread=None;module.on_unload();view.close()
                self.app.processEvents()


if __name__=='__main__':
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('Archive.'+name,sys.modules[__name__])
        for name in Archive.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
