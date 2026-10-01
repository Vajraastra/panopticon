"""Transición explícita a catálogo frío; sólo fixtures, sin tocar disco/Cherry."""
from concurrent.futures import Future
from contextlib import closing
from copy import deepcopy
import hashlib,json,os,sqlite3,sys,threading,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from core.folder_archive import preview,archive
from core.library_repository import LibraryRepository
from core.library_service import LibraryError
from modules.librarian.logic.folder_archive import FolderArchiveTasks,FolderArchiveWorker
from curation_snapshot_selftest import Fixture,pump


class FolderArchive(Fixture):
    def prepare(self):
        self.folder=(Path(self.temp.name)/'collection').resolve();self.folder.mkdir()
        self.service.submit('add_watched_folder',self.folder.as_posix()).result(3)
        self.paths=[self.folder/'a.png',self.folder/'child/b.png']
        for path in self.paths:
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture image')
            self.service.submit('register_files_minimal',[dict(path=path.as_posix(),filename=path.name,size=13,created=1,
                hash_original=hashlib.sha256(b'fixture image').hexdigest())]).result(3)
            self.service.submit('edit_curation',path.as_posix(),add_tags=['keep'],rating=5).result(3)
        self.ids=[self.service.read('get_file_id',path.as_posix()) for path in self.paths]
        self.before=[self.service.read('get_copy_version_state',key) for key in self.ids]
        self.tasks=FolderArchiveTasks(self.service)
        self.report=self.tasks.read('preview',folder=self.folder.as_posix())['preview']
        return self.report

    def apply(self,event=None,fingerprint=None):
        return self.tasks.read('archive',folder=self.folder.as_posix(),expected_fingerprint=fingerprint or self.report['fingerprint'],cancel_event=event)

    def assert_active(self):
        self.assertEqual(self.service.read('get_watched_folders'),[self.folder.as_posix()])
        for path,key in zip(self.paths,self.ids):self.assertEqual(self.service.read('get_file_id',path.as_posix()),key)

    def test_readonly_preview_is_compact_and_transition_preserves_bytes_full_curation_fts_receipts(self):
        self.prepare();receipt=self.folder/'catalog.db';receipt.write_bytes(b'Cherry hash/URL/alias receipts')
        with patch.object(self.service,'submit',side_effect=AssertionError('preview writes')):
            value=self.tasks.read('preview',folder=self.folder.as_posix())
        self.assertEqual(value['preview'],self.report);self.assertLess(len(json.dumps(value)),1000)
        result=self.apply();self.assertTrue(result['committed']);self.assertEqual(result['archived'],2)
        self.assertEqual(self.service.read('get_watched_folders'),[]);self.assertEqual(self.service.read('count_files'),0)
        for key,before,path in zip(self.ids,self.before,self.paths):
            expected=deepcopy(before['state']);expected['file']['is_offline']=1
            self.assertEqual(self.service.read('get_copy_version_state',key)['state'],expected)
            self.assertEqual(path.read_bytes(),b'fixture image')
        self.assertEqual(receipt.read_bytes(),b'Cherry hash/URL/alias receipts')
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM files_fts').fetchone()[0],2)

    def test_archive_disconnected_folder_never_stats_or_hashes_paths(self):
        self.prepare();self.folder.rename(self.folder.with_name('disconnected'))
        with patch('modules.librarian.logic.folder_archive.os.stat',side_effect=AssertionError('FS stat')),patch.object(Path,'open',side_effect=AssertionError('FS read')):
            value=self.tasks.read('preview',folder=self.folder.as_posix());result=self.apply()
        self.assertEqual(value['preview'],self.report);self.assertTrue(result['committed'])
        self.assertEqual(self.service.read('count_files'),0)

    def test_nested_watched_folders_are_visible_and_remain_registered_but_their_copies_archive(self):
        self.prepare();nested=(self.folder/'child').as_posix()
        self.service.submit('add_watched_folder',nested).result(3)
        self.report=self.tasks.read('preview',folder=self.folder.as_posix())['preview']
        self.assertEqual(self.report['nested_watched'],[nested]);result=self.apply()
        self.assertEqual(result['archived'],2);self.assertEqual(self.service.read('get_watched_folders'),[nested])
        self.assertEqual(self.service.read('count_files'),0)

    def test_new_member_or_online_offline_transition_requires_new_confirmation(self):
        self.prepare()
        self.service.submit('register_files_minimal',[dict(path=(self.folder/'new.png').as_posix(),filename='new.png',size=1,created=1)]).result(3)
        with self.assertRaisesRegex(ValueError,'members_changed'):self.apply()
        self.assert_active();self.assertEqual(self.service.read('count_files'),3)

    def test_curation_updated_since_preview_is_preserved_without_overwrite(self):
        self.prepare()
        self.service.submit('edit_curation',self.paths[0].as_posix(),add_tags=['new-curation'],rating=4).result(3)
        current=self.service.read('get_copy_version_state',self.ids[0])
        self.assertEqual(self.tasks.read('preview',folder=self.folder.as_posix())['preview']['fingerprint'],self.report['fingerprint'])
        self.apply();expected=deepcopy(current['state']);expected['file']['is_offline']=1
        self.assertEqual(self.service.read('get_copy_version_state',self.ids[0])['state'],expected)

    def test_root_membership_race_after_worker_review_is_rejected_by_owner(self):
        self.prepare();submit=self.service.submit
        def changed(operation,*args,**kwargs):
            submit('register_files_minimal',[dict(path=(self.folder/'race.png').as_posix(),filename='race.png',size=1,created=1)]).result(3)
            return submit(operation,*args,**kwargs)
        with patch.object(self.service,'submit',side_effect=changed):
            with self.assertRaisesRegex(LibraryError,'members_changed'):self.apply()
        self.assert_active()

    def test_sql_failure_after_unwatch_rolls_back_root_and_all_offline_flags(self):
        self.prepare()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute("CREATE TRIGGER block_archive BEFORE UPDATE OF is_offline ON files BEGIN SELECT RAISE(ABORT,'fixture'); END");conn.commit()
        with self.assertRaisesRegex(LibraryError,'commit_unconfirmed'):self.apply()
        self.assert_active();self.assertEqual(self.service.read('count_files'),2)

    def test_false_future_and_error_during_cancel_are_visible_while_admitted_success_completes(self):
        self.prepare();event=threading.Event();future=Future();future.set_result(False)
        def false(*args,**kwargs):event.set();return future
        with patch.object(self.service,'submit',side_effect=false):
            with self.assertRaisesRegex(ValueError,'commit_unconfirmed'):self.apply(event)
        self.assert_active();event.clear();submit=self.service.submit
        def success(*args,**kwargs):result=submit(*args,**kwargs);event.set();return result
        with patch.object(self.service,'submit',side_effect=success):value=self.apply(event)
        self.assertTrue(value['committed']);self.assertEqual(value['archived'],2)

    def test_pre_cancel_or_invalid_scope_and_fingerprint_changes_nothing(self):
        self.prepare();event=threading.Event();event.set()
        with self.assertRaisesRegex(RuntimeError,'curation_cancelled'):self.apply(event)
        with self.assertRaisesRegex(ValueError,'members_changed'):self.apply(fingerprint='0'*64)
        with self.assertRaises(ValueError):self.tasks.read('preview',folder='relative')
        self.assert_active()

    def test_read_and_owner_budgets_and_limits_reject_without_prefix_or_partial_archival(self):
        self.prepare()
        with patch('core.folder_archive.MAX_COPIES',1):
            with self.assertRaisesRegex(ValueError,'copy_limit'):self.tasks.read('preview',folder=self.folder.as_posix())
        with patch('core.folder_archive.MAX_SECONDS',-1):
            with self.assertRaisesRegex(ValueError,'check_budget'):self.tasks.read('preview',folder=self.folder.as_posix())
        with patch('core.folder_archive.MAX_WRITE_SECONDS',-1):
            with self.assertRaises(LibraryError):self.apply()
        self.assert_active()

    def test_wildcard_scope_escapes_names_and_previews_offline_history_without_counting_it_active(self):
        self.prepare();scope=(Path(self.temp.name)/'literal%_').resolve()
        self.service.submit('add_watched_folder',scope.as_posix()).result(3)
        for parent in (scope,scope.with_name('literalZZ')):
            self.service.submit('register_files_minimal',[dict(path=(parent/'image.png').as_posix(),filename='image.png',size=1,created=1)]).result(3)
        value=self.tasks.read('preview',folder=scope.as_posix())['preview'];self.assertEqual(value['active'],1)
        key=self.service.read('get_file_id',(scope/'image.png').as_posix());state=self.service.read('get_copy_version_state',key)
        self.service.submit('archive_copy',key,expected_fingerprint=state['fingerprint']).result(3)
        value=self.tasks.read('preview',folder=scope.as_posix())['preview'];self.assertEqual((value['active'],value['offline']), (0,1))
        result=self.tasks.read('archive',folder=scope.as_posix(),expected_fingerprint=value['fingerprint'])
        self.assertEqual(result['archived'],0);self.assertIsNotNone(self.service.read('get_file_id',(scope.with_name('literalZZ')/'image.png').as_posix()))

    def test_worker_terminal_once_pre_cancel_and_accepted_failure_not_masked(self):
        self.prepare()
        for operation in ('preview','archive'):
            worker=FolderArchiveWorker(self.service,operation=operation,folder=self.folder.as_posix(),expected_fingerprint=self.report['fingerprint'],token=33)
            values=[];worker.finished_signal.connect(values.append);worker.stop();worker.start()
            try:self.assertTrue(pump(lambda:values));self.assertEqual(len(values),1);self.assertTrue(values[0]['cancelled'])
            finally:worker.wait(5000)
        self.assert_active()


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
