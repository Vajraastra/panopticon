"""Archivado manual: alcance concreto, Qt responsive, cierre y carreras; sólo fixtures."""
import os,sys,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QCoreApplication,QEvent,QTimer,Qt
from PySide6.QtWidgets import QApplication,QMessageBox
from core.library_service import LibraryError
from modules.librarian.logic.folder_archive import FolderArchiveWorker
from modules.librarian.ui.folder_archive import _LIVE_WORKERS
import curation_snapshot_selftest as helpers
import librarian_data_selftest as reads
import librarian_write_selftest as writes


class FolderArchiveQt(helpers.Fixture):
    view=reads.LibrarianQt.view
    ready=reads.LibrarianQt.ready
    shutdown=reads.LibrarianQt.shutdown
    block_write=writes.FolderWrites.block_write

    def setUp(self):
        super().setUp()
        from core.event_bus import EventBus
        self.modules=[];self.releases=[];self.bus=EventBus()
        self.addCleanup(self.shutdown)
        for folder in ('C:/A','C:/B'):
            self.service.submit('add_watched_folder',folder).result(3)
        self.register('C:/A/a.png','C:/A/sub/b.png','C:/B/c.png')
        self.service.submit('edit_curation','C:/A/a.png',add_tags=['curated'],rating=5).result(3)

    def selected(self):
        module=self.view();self.ready(module)
        module.folder_list.setCurrentRow(0)
        return module

    def finish(self,module):
        self.assertTrue(helpers.pump(lambda:module._folder_write is None and module.folder_archive.shutdown_ready()))

    def assert_active(self):
        self.assertEqual(self.service.read('count_files'),3)
        self.assertIn('C:/A',self.service.read('get_watched_folders'))

    def test_preview_keeps_qt_responsive_and_coalesces_double_click_without_gui_db_or_fs(self):
        module=self.selected();entered,release=threading.Event(),threading.Event();self.releases.append(release)
        read=self.service.read;calls=[]
        def blocked(name,*args,**kwargs):
            if name=='get_folder_archive_preview':
                calls.append(threading.get_ident());entered.set();release.wait(3)
            return read(name,*args,**kwargs)
        with patch.object(self.service,'read',blocked),patch.object(module.folder_archive,'confirm',return_value=False):
            tick=[];QTimer.singleShot(0,lambda:tick.append(True))
            started=time.monotonic();module.remove_folder()
            self.assertLess(time.monotonic()-started,.3);self.assertTrue(entered.wait(1))
            self.assertTrue(helpers.pump(lambda:tick));module.remove_folder();module.toggle_scan();module.toggle_backfill()
            self.assertEqual(len(calls),1);self.assertNotEqual(calls[0],threading.get_ident())
            self.assertFalse(module.btn_add.isEnabled());self.assertFalse(module.btn_remove.isEnabled())
            self.assert_active();release.set();self.finish(module)
        self.assertTrue(module.btn_add.isEnabled());self.assertTrue(module.btn_remove.isEnabled())

    def test_modal_is_plaintext_cancel_default_with_counts_nested_scope_and_tooltips(self):
        self.service.submit('add_watched_folder','C:/A/sub').result(3)
        key=self.service.read('get_file_id','C:/A/sub/b.png')
        state=self.service.read('get_copy_version_state',key)
        self.service.submit('archive_copy',key,expected_fingerprint=state['fingerprint']).result(3)
        module=self.selected();seen=[]
        def inspect(box):
            self.assertEqual(box.textFormat(),Qt.PlainText)
            self.assertEqual(box.defaultButton(),box.button(QMessageBox.No))
            for value in ('C:/A','1 active','1 copies already','2 identities','C:/A/sub','Cherry'):
                self.assertIn(value,box.text())
            self.assertTrue(all(box.button(button).toolTip() for button in (QMessageBox.Yes,QMessageBox.No)))
            self.assertIsNone(module.folder_archive.worker)
            seen.append(True);return QMessageBox.No
        with patch.object(QMessageBox,'exec',inspect):
            module.remove_folder();self.finish(module)
        self.assertEqual(seen,[True]);self.assertEqual(self.service.read('count_files'),2)
        self.assertIn('cancelled',module.lbl_folder_write.text())

    def test_new_members_during_modal_reject_whole_confirmed_scope_and_allow_retry(self):
        module=self.selected()
        def mutate(report):
            self.assertEqual(report['active'],2)
            self.register('C:/A/new.png');return True
        with patch.object(module.folder_archive,'confirm',mutate):
            module.remove_folder();self.finish(module)
        self.assertIn('folder_archive_members_changed',module.lbl_folder_write.text())
        self.assertEqual(self.service.read('count_files'),4)
        with patch.object(module.folder_archive,'confirm',return_value=True):
            module.remove_folder();self.finish(module)
        self.assertIn('3 copies confirmed offline',module.lbl_folder_write.text())
        self.assertEqual(self.service.read('count_files'),1)

    def test_latest_curation_during_modal_survives_confirmed_archival(self):
        module=self.selected();key=self.service.read('get_file_id','C:/A/a.png')
        def mutate(report):
            self.service.submit('edit_curation','C:/A/a.png',add_tags=['new'],rating=4).result(3)
            return True
        with patch.object(module.folder_archive,'confirm',mutate):
            module.remove_folder();self.finish(module)
        state=self.service.read('get_copy_version_state',key)['state']
        self.assertEqual(state['file']['rating'],4);self.assertEqual({tag['name'] for tag in state['tags']},{'curated','new'})
        self.assertEqual(state['file']['is_offline'],1)

    def test_selection_changed_inside_modal_cancels_without_redirecting_scope(self):
        module=self.selected()
        def move_selection(report):module.folder_list.setCurrentRow(1);return True
        with patch.object(module.folder_archive,'confirm',move_selection):
            module.remove_folder();self.finish(module)
        self.assert_active();self.assertEqual(module.folder_list.currentItem().text(),'C:/B')

    def test_new_watched_root_inside_modal_invalidates_preview(self):
        module=self.selected()
        def mutate(report):self.service.submit('add_watched_folder','C:/A/sub').result(3);return True
        with patch.object(module.folder_archive,'confirm',mutate):
            module.remove_folder();self.finish(module)
        self.assertIn('folder_archive_members_changed',module.lbl_folder_write.text());self.assert_active()

    def test_close_inside_modal_submits_nothing_and_never_touches_closed_widget(self):
        module=self.selected();seen=[]
        def close(report):
            module.on_unload();seen.append(module.lbl_folder_write.text());return True
        with patch.object(module.folder_archive,'confirm',close):
            module.remove_folder();self.assertTrue(helpers.pump(lambda:seen))
        self.assert_active();self.assertEqual(module.lbl_folder_write.text(),seen[0])
        self.assertTrue(module.shutdown_ready())

    def test_close_pending_preview_keeps_worker_until_native_finished_and_no_commit(self):
        module=self.selected();entered,release=threading.Event(),threading.Event();self.releases.append(release)
        read=self.service.read
        def blocked(name,*args,**kwargs):
            if name=='get_folder_archive_preview':entered.set();release.wait(3)
            return read(name,*args,**kwargs)
        with patch.object(self.service,'read',blocked),patch.object(module.folder_archive,'confirm',side_effect=AssertionError('closed modal')):
            module.remove_folder();self.assertTrue(entered.wait(1));worker=module.folder_archive.worker
            module.on_unload();text=module.lbl_folder_write.text()
            self.assertFalse(module.shutdown_ready());self.assertIn(worker,_LIVE_WORKERS)
            release.set();self.assertTrue(helpers.pump(module.shutdown_ready))
            self.assertTrue(helpers.pump(lambda:worker not in _LIVE_WORKERS))
            self.assertEqual(module.lbl_folder_write.text(),text)
        self.assert_active()

    def test_close_after_owner_admission_finishes_commit_and_holds_service_readiness(self):
        module=self.selected();block,entered,release,_=self.block_write('remove_watched_folder')
        with block,patch.object(module.folder_archive,'confirm',return_value=True):
            module.remove_folder();self.assertTrue(helpers.pump(lambda:entered.is_set()))
            worker=module.folder_archive.worker;module.on_unload();text=module.lbl_folder_write.text()
            self.assertFalse(module.shutdown_ready());release.set()
            self.assertTrue(helpers.pump(module.shutdown_ready));self.assertTrue(helpers.pump(lambda:worker not in _LIVE_WORKERS))
            self.assertEqual(module.lbl_folder_write.text(),text)
        self.assertEqual(self.service.read('count_files'),1)
        self.assertNotIn('C:/A',self.service.read('get_watched_folders'))

    def test_destroyed_module_keeps_admitted_worker_alive_and_database_commit(self):
        module=self.selected();block,entered,release,_=self.block_write('remove_watched_folder')
        with block,patch.object(module.folder_archive,'confirm',return_value=True):
            module.remove_folder();self.assertTrue(helpers.pump(lambda:entered.is_set()))
            worker=module.folder_archive.worker;module.on_unload();module.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);self.modules.remove(module)
            module.view.close();module.view.deleteLater();self.assertIn(worker,_LIVE_WORKERS)
            release.set();self.assertTrue(helpers.pump(lambda:not worker.isRunning()))
            self.assertTrue(helpers.pump(lambda:worker not in _LIVE_WORKERS))
        self.assertEqual(self.service.read('count_files'),1)

    def test_read_failure_never_opens_confirmation_and_can_retry(self):
        module=self.selected();read=self.service.read
        def fail(name,*args,**kwargs):
            if name=='get_folder_archive_preview':raise LibraryError(name,'fixture_read_failure')
            return read(name,*args,**kwargs)
        with patch.object(self.service,'read',fail),patch.object(module.folder_archive,'confirm',side_effect=AssertionError('failed preview modal')):
            module.remove_folder();self.finish(module)
        self.assertIn('fixture_read_failure',module.lbl_folder_write.text());self.assert_active()
        with patch.object(module.folder_archive,'confirm',return_value=True):
            module.remove_folder();self.finish(module)
        self.assertEqual(self.service.read('count_files'),1)

    def test_worker_start_failure_resets_controls_and_retention(self):
        module=self.selected();live=set(_LIVE_WORKERS)
        with patch.object(FolderArchiveWorker,'start',side_effect=RuntimeError('fixture_start_failure')):
            module.remove_folder()
        self.assertIsNone(module._folder_write);self.assertTrue(module.btn_remove.isEnabled())
        self.assertEqual(_LIVE_WORKERS,live);self.assertIn('fixture_start_failure',module.lbl_folder_write.text())
        self.assert_active()

    def test_destroyed_modal_parent_prevents_archive_and_drops_late_widget_access(self):
        module=self.selected();seen=[]
        def destroy_parent(box):
            module.view.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
            seen.append(True);return QMessageBox.Yes
        with patch.object(QMessageBox,'exec',destroy_parent):
            module.remove_folder();self.assertTrue(helpers.pump(lambda:seen))
        self.assertTrue(module.folder_archive.closed);self.assertTrue(module.shutdown_ready());self.assert_active()
        module.on_unload();self.modules.remove(module)
        for reader in (module.overview_reader,module.browse_reader,module.scan_reader,module.handoff_reader,module.export_reader):
            if reader._thread:reader._thread.join(3)

    def test_indexer_unload_cancels_without_wait_and_retains_qthread_until_finished(self):
        self.check_background_close('indexer_thread')

    def test_hashing_unload_cancels_without_wait_and_retains_qthread_until_finished(self):
        self.check_background_close('backfill_thread')

    def test_directed_reindex_unload_cancels_without_wait_and_retains_qthread_until_finished(self):
        self.check_background_close('_reindex_thread')

    def check_background_close(self, name):
        module=self.selected();entered,release=threading.Event(),threading.Event();self.releases.append(release)
        from modules.librarian.logic.indexer import IndexerWorker
        from modules.librarian.logic.hash_backfill import HashBackfillWorker
        cls=HashBackfillWorker if name=='backfill_thread' else IndexerWorker
        def blocked(worker):
            entered.set();release.wait(3)
            # El servicio todavía debe estar disponible cuando termina el productor.
            self.assertEqual(worker.db.service.read('count_files'),3)
        with patch.object(cls,'run',blocked),patch('modules.librarian.module.os.path.isdir',return_value=True):
            if name=='indexer_thread':module._start_scan(['C:/A'],False,True)
            elif name=='backfill_thread':module.toggle_backfill()
            else:module._dirty_folders.add('C:/A');module._flush_dirty_reindex()
            worker=getattr(module,name);self.assertTrue(entered.wait(1))
            self.assertIn(worker,_LIVE_WORKERS)
            started=time.monotonic();module.on_unload();self.assertLess(time.monotonic()-started,.1)
            self.assertFalse(module.shutdown_ready());release.set()
            self.assertTrue(helpers.pump(module.shutdown_ready))
            self.assertTrue(helpers.pump(lambda:worker not in _LIVE_WORKERS))


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
