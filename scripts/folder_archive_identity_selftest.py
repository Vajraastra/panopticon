"""Prepared catalogue archival, without collection/Cherry writes or image I/O."""
from contextlib import closing
from copy import deepcopy
import os,sqlite3,sys,threading,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from core.folder_catalogue_scope import possible_alias
from core.library_service import LibraryError
from core.path_identity import PathIdentity,observe_path
from modules.librarian.logic.folder_archive import FolderArchiveTasks
from curation_snapshot_selftest import Fixture
import folder_archive_selftest as legacy


class IdentityArchive(Fixture):
    prepare=legacy.FolderArchive.prepare
    apply=legacy.FolderArchive.apply
    assert_active=legacy.FolderArchive.assert_active

    def prepare_identity(self):
        self.prepare()
        self.assertTrue(self.service.submit('prepare_path_identity').result(3))
        self.report=self.tasks.read('preview',folder=self.folder.as_posix())['preview']

    def test_pending_bindings_do_not_block_explicit_catalogue_archival_and_offline_bindings_commit_together(self):
        self.prepare_identity()
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],2)
        self.assertEqual(self.report['scope_mode'],'observed_directory')
        with patch.object(Path,'open',side_effect=AssertionError('image read')):
            result=self.apply()
        self.assertEqual(result['archived'],2)
        status=self.service.read('get_path_identity_status')
        self.assertEqual((status['pending'],status['unresolved']),(0,0))
        for key,before,path in zip(self.ids,self.before,self.paths):
            current=self.service.read('get_copy_version_state',key)['state']
            expected=deepcopy(before['state']);expected['file']['is_offline']=1
            self.assertEqual(current,expected);self.assertEqual(path.read_bytes(),b'fixture image')
        self.assertTrue(self.service.read('check_fts')['consistent'])

    def test_actual_unicode_directory_alias_includes_active_offline_history_and_nested_watch(self):
        self.folder=(Path(self.temp.name)/'Árbol%_').resolve();self.folder.mkdir()
        actual=self.folder.as_posix();alias=actual.replace('Árbol','árbol')
        self.service.submit('add_watched_folder',actual).result(3)
        self.service.submit('add_watched_folder',alias+'/child').result(3)
        self.register(actual+'/a.png',alias+'/b.png',alias+'/child/c.png')
        self.service.submit('edit_curation',alias+'/b.png',add_tags=['curated'],rating=5).result(3)
        key=self.service.read('get_file_id',alias+'/b.png');state=self.service.read('get_copy_version_state',key)
        self.service.submit('archive_copy',key,expected_fingerprint=state['fingerprint']).result(3)
        self.service.submit('prepare_path_identity').result(3)
        self.tasks=FolderArchiveTasks(self.service)
        self.report=self.tasks.read('preview',folder=actual)['preview']
        self.assertEqual((self.report['active'],self.report['offline']),(2,1))
        self.assertEqual(self.report['nested_watched'],[alias+'/child'])
        self.assertTrue(self.apply()['committed'])
        self.assertEqual(self.service.read('count_files'),0)
        self.assertEqual(self.service.read('get_copy_version_state',key)['state'],state['state']|{'file':state['state']['file']|{'is_offline':1}})
        self.assertEqual(self.service.read('get_watched_folders'),[alias+'/child'])

    def test_case_sensitive_sibling_is_excluded_using_actual_directory_evidence(self):
        root,other='C:/Fixture/Root','C:/Fixture/root'
        for path in (root,other):self.service.submit('add_watched_folder',path).result(3)
        self.register(root+'/a.png',other+'/b.png')
        self.service.submit('prepare_path_identity').result(3)
        def observed(path,**kwargs):
            return PathIdentity(path,'resolved',path,'directory',(1,0,1 if path==root else 2,0,0,0,0))
        tasks=FolderArchiveTasks(self.service)
        with patch('core.folder_catalogue_scope.observe_path',side_effect=observed):
            report=tasks.read('preview',folder=root)['preview']
            self.assertEqual(report['active'],1);self.assertEqual(report['nested_watched'],[])
            result=tasks.read('archive',folder=root,expected_fingerprint=report['fingerprint'])
        self.assertEqual(result['archived'],1)
        self.assertIsNone(self.service.read('get_file_id',root+'/a.png'))
        self.assertIsNotNone(self.service.read('get_file_id',other+'/b.png'))
        self.assertEqual(self.service.read('get_watched_folders'),[other])

    def test_prepared_disconnected_folder_uses_visible_literal_scope_without_image_reads(self):
        self.prepare_identity();self.folder.rename(self.folder.with_name('disconnected'))
        with patch.object(Path,'open',side_effect=AssertionError('image read')):
            self.report=self.tasks.read('preview',folder=self.folder.as_posix())['preview']
            self.assertEqual(self.report['scope_mode'],'literal_offline')
            self.assertTrue(self.apply()['committed'])
        self.assertEqual(self.service.read('count_files'),0)

    def test_unavailable_alias_requires_review_and_keeps_every_copy_online(self):
        self.prepare()
        alias=self.folder.as_posix().replace('/collection','/COLLECTION')
        self.register(alias+'/alias.png')
        self.service.submit('prepare_path_identity').result(3)
        self.folder.rename(self.folder.with_name('disconnected'))
        with self.assertRaisesRegex(ValueError,'scope_requires_review'):
            self.tasks.read('preview',folder=self.folder.as_posix())
        self.assert_active();self.assertEqual(self.service.read('count_files'),3)

    def test_directory_replacement_after_sql_preview_rejects_scope(self):
        self.prepare_identity();original=observe_path(self.folder.as_posix());calls=0
        def observed(path,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:return original
            physical=list(original.physical);physical[2]^=1
            return PathIdentity(path,'resolved',original.canonical,'directory',tuple(physical))
        with patch('core.folder_catalogue_scope.observe_path',side_effect=observed):
            with self.assertRaisesRegex(ValueError,'scope_changed'):
                self.tasks.read('preview',folder=self.folder.as_posix())
        self.assert_active()

    def test_new_directory_prefix_after_worker_review_rejects_whole_owner_commit(self):
        self.prepare_identity();submit=self.service.submit
        def changed(operation,*args,**kwargs):
            if operation=='archive_watched_folder_reviewed':
                submit('register_files_minimal',[dict(path=(self.folder.parent/'Other/a.png').as_posix(),filename='a.png',size=1,created=1)]).result(3)
            return submit(operation,*args,**kwargs)
        with patch.object(self.service,'submit',side_effect=changed):
            with self.assertRaisesRegex(LibraryError,'members_changed'):self.apply()
        self.assert_active();self.assertEqual(self.service.read('get_path_identity_status')['pending'],3)

    def test_curation_changed_after_review_survives_prepared_archive(self):
        self.prepare_identity()
        self.service.submit('edit_curation',self.paths[0].as_posix(),add_tags=['new'],rating=4).result(3)
        state=self.service.read('get_copy_version_state',self.ids[0])['state']
        self.assertTrue(self.apply()['committed'])
        self.assertEqual(self.service.read('get_copy_version_state',self.ids[0])['state'],state|{'file':state['file']|{'is_offline':1}})

    def test_unobserved_direct_prepared_archival_is_rejected(self):
        self.prepare_identity()
        with self.assertRaisesRegex(LibraryError,'scope_requires_review'):
            self.service.submit('remove_watched_folder',self.folder.as_posix()).result(3)
        with self.assertRaisesRegex(LibraryError,'scope_requires_review'):
            self.service.submit('archive_watched_folder_reviewed',self.folder.as_posix(),expected_fingerprint=self.report['fingerprint']).result(3)
        self.assert_active()

    def test_preparation_between_legacy_review_and_admission_is_rejected(self):
        self.prepare();submit=self.service.submit
        def changed(operation,*args,**kwargs):
            if operation=='archive_watched_folder_reviewed':submit('prepare_path_identity').result(3)
            return submit(operation,*args,**kwargs)
        with patch.object(self.service,'submit',side_effect=changed):
            with self.assertRaisesRegex(LibraryError,'scope_requires_review'):self.apply()
        self.assert_active()

    def test_binding_sql_failure_rolls_back_watch_availability_and_all_history(self):
        self.prepare_identity()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute("CREATE TRIGGER block_binding BEFORE INSERT ON path_identity_records BEGIN SELECT RAISE(ABORT,'binding failure'); END");conn.commit()
        with self.assertRaisesRegex(LibraryError,'commit_unconfirmed'):self.apply()
        self.assert_active();self.assertEqual(self.service.read('get_path_identity_status')['pending'],2)

    def test_scope_limits_and_cancel_never_approve_a_prefix(self):
        self.prepare_identity()
        with patch('core.folder_catalogue_scope.MAX_PREFIXES',0):
            with self.assertRaisesRegex(ValueError,'scope_limit'):self.tasks.read('preview',folder=self.folder.as_posix())
        with patch('core.folder_catalogue_scope.MAX_ROWS',1):
            with self.assertRaisesRegex(ValueError,'scope_limit'):self.tasks.read('preview',folder=self.folder.as_posix())
        with patch('core.folder_catalogue_scope.MAX_SECONDS',-1):
            with self.assertRaisesRegex(ValueError,'check_budget'):self.tasks.read('preview',folder=self.folder.as_posix())
        event=threading.Event();event.set()
        with self.assertRaisesRegex(RuntimeError,'curation_cancelled'):self.apply(event)
        self.assert_active()

    def test_probes_are_outside_sql_snapshots_and_no_preparation_or_file_reads_occur(self):
        self.prepare_identity();observed=observe_path
        def check(path,**kwargs):
            self.assertEqual(self.service.metrics_snapshot()['reads_active'],0)
            return observed(path,**kwargs)
        with patch('core.folder_catalogue_scope.observe_path',side_effect=check),patch.object(self.service,'submit',side_effect=AssertionError('preview writes')),patch.object(Path,'open',side_effect=AssertionError('image read')):
            self.assertEqual(self.tasks.read('preview',folder=self.folder.as_posix())['preview'],self.report)

    def test_windows_ordinal_is_only_an_ambiguity_check_and_does_not_expand_sharp_s(self):
        self.assertTrue(possible_alias('C:/Árbol','c:/árbol'))
        self.assertFalse(possible_alias('C:/straße','C:/strasse'))

    def test_accepted_cancel_settles_native_archive_before_releasing_worker(self):
        self.prepare_identity();event=threading.Event();submit=self.service.submit
        def accepted(*args,**kwargs):
            future=submit(*args,**kwargs);event.set();return future
        with patch.object(self.service,'submit',side_effect=accepted):
            self.assertTrue(self.apply(event)['committed'])
        self.assertEqual(self.service.read('count_files'),0)
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)

    def test_pending_physical_transfer_source_is_not_archived_mid_publication(self):
        self.prepare_identity();self.service.submit('prepare_transfers').result(3)
        from core.library_transfers import NAME
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('INSERT INTO '+NAME+'(operation_id,source_copy_id,action,source_path,result_path,digest,source_fingerprint,request_fingerprint,phase) VALUES(?,?,?,?,?,?,?,?,?)',
                ('fixture-pending',self.ids[0],'move',self.paths[0].as_posix(),(self.folder.parent/'target/a.png').as_posix(),'a'*64,'b'*64,'c'*64,'approved'))
            conn.commit()
        with self.assertRaisesRegex(LibraryError,'transfer_ingestion_pending'):self.apply()
        self.assert_active();self.assertEqual(self.service.read('get_path_identity_status')['pending'],2)

    def test_identity_probe_cancel_emits_one_cancelled_terminal_without_native_write(self):
        self.prepare_identity()
        from modules.librarian.logic.folder_archive import FolderArchiveWorker
        from curation_snapshot_selftest import pump
        worker=FolderArchiveWorker(self.service,operation='preview',folder=self.folder.as_posix())
        values=[];worker.finished_signal.connect(values.append)
        def cancelled(path,**kwargs):
            worker.stop();raise ValueError('identity_cancelled')
        with patch('core.folder_catalogue_scope.observe_path',side_effect=cancelled):
            worker.start()
            try:
                self.assertTrue(pump(lambda:values));self.assertEqual(len(values),1)
                self.assertTrue(values[0]['cancelled']);self.assertIsNone(values[0]['error'])
            finally:worker.wait(5000)
        self.assert_active()


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
