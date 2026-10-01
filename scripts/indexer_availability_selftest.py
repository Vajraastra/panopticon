"""Una raíz sana no prueba ausencia bajo un padre desconectado o cambiado."""
import os,subprocess,sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.inventory import InventoryStatus as Status, directory_chain_identity
from modules.librarian.logic import indexer
import indexer_safety_selftest as prior


class Availability(prior.IndexerSafety):
    def missing(self):
        parent=self.folder/'child';parent.mkdir()
        path=parent/'absent.png';before=self.seed(path)
        return parent,path,before

    def assert_active(self,path,before):
        self.assert_preserved(path,before)
        self.assertEqual(self.db.get_file_id(path.as_posix()),before[0])

    def test_absent_parent_does_not_archive_rows_under_healthy_root(self):
        parent,path,before=self.missing();parent.rmdir()
        worker=self.run_worker()
        self.assertEqual(worker.outcome,Status.UNAVAILABLE);self.assert_active(path,before)

    def test_parent_disappears_after_complete_inventory_preserves_all_candidates(self):
        parent,path,before=self.missing()
        healthy=self.folder/'also-missing.png';other=self.seed(healthy)
        worker=indexer.IndexerWorker(self.db,[str(self.folder)],deep_clean=True)
        discover=worker._discover_tree
        def disconnected(*args):
            result=discover(*args);parent.rename(self.root/'disconnected');return result
        with patch.object(worker,'_discover_tree',disconnected):worker.run()
        self.assertEqual(worker.outcome,Status.UNAVAILABLE)
        self.assert_active(path,before);self.assert_active(healthy,other)

    def test_parent_permission_or_listing_error_preserves_curation_and_availability(self):
        parent,path,before=self.missing()
        def denied(*args):raise PermissionError('parent cannot be listed')
        with patch.object(indexer,'directory_chain_identity',side_effect=denied):worker=self.run_worker()
        self.assertEqual(worker.outcome,Status.UNAVAILABLE);self.assert_active(path,before)

    def test_parent_replacement_after_absence_detection_rejects_entire_cleanup(self):
        parent,path,before=self.missing();lstat=os.lstat;replaced=[]
        def replacement(name,*args,**kwargs):
            if Path(name)==path:
                if not replaced:
                    # First lstat discovers absence; replacement on the recheck
                    # occurs after the initial accessible parent identity is saved.
                    replaced.append(False)
                elif not replaced[0]:
                    parent.rename(self.root/'old-parent');parent.mkdir();replaced[0]=True
            return lstat(name,*args,**kwargs)
        with patch.object(indexer.os,'lstat',side_effect=replacement):worker=self.run_worker()
        self.assertTrue(replaced[0]);self.assertEqual(worker.outcome,Status.UNAVAILABLE);self.assert_active(path,before)

    def test_file_reappears_during_parent_access_check_is_not_archived(self):
        parent,path,before=self.missing();probe=directory_chain_identity
        def reappeared(name):
            value=probe(name);path.write_bytes(b'now present');return value
        with patch.object(indexer,'directory_chain_identity',side_effect=reappeared):worker=self.run_worker()
        self.assertEqual(worker.outcome,Status.COMPLETE);self.assert_active(path,before)

    def test_cancel_during_parent_check_preserves_all_candidates(self):
        parent,path,before=self.missing()
        worker=indexer.IndexerWorker(self.db,[str(self.folder)],deep_clean=True)
        probe=directory_chain_identity
        def cancelled(name):value=probe(name);worker.stop();return value
        with patch.object(indexer,'directory_chain_identity',side_effect=cancelled):worker.run()
        self.assertEqual(worker.outcome,Status.CANCELLED);self.assert_active(path,before)

    def test_missing_entries_share_bounded_parent_checks_and_keep_fts_and_history(self):
        paths=[self.folder/f'absent-{index}.png' for index in range(40)]
        before=[self.seed(path) for path in paths]
        probe=directory_chain_identity
        with patch.object(indexer,'directory_chain_identity',wraps=probe) as checks:worker=self.run_worker()
        self.assertEqual(worker.outcome,Status.COMPLETE);self.assertLessEqual(checks.call_count,4)
        self.assertEqual(worker.archived_count,40)
        for path,value in zip(paths,before):
            self.assert_preserved(path,value);self.assertIsNone(self.db.get_file_id(path.as_posix()))
        self.assertEqual(self.inspect_conn.execute('SELECT count(*) FROM files_fts').fetchone()[0],40)

    def test_actual_windows_junction_parent_requires_review_without_touching_targets(self):
        target=self.root/'junction-target';target.mkdir()
        link=self.folder/'junction'
        completed=subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(link),str(target)],
            capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
        self.assertEqual(completed.returncode,0,completed.stderr)
        path=link/'absent.png';before=self.seed(path)
        with self.assertRaisesRegex(OSError,'linked_directory_requires_review'):directory_chain_identity(link)
        worker=self.run_worker()
        self.assertEqual(worker.outcome,Status.UNAVAILABLE);self.assert_active(path,before)
        self.assertTrue(target.is_dir());self.assertTrue(link.is_junction())


if __name__=='__main__':
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('Availability.'+name,sys.modules[__name__])
        for name in Availability.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
