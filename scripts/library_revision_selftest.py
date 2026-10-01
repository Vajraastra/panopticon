"""Private native/WAL revisions and Qt consumer invalidation."""
import os
import sqlite3
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QTimer, QCoreApplication, QEvent
from PySide6.QtWidgets import QWidget
from core.library_service import LibraryService
from core.library_repository import LibraryRepository
from core.library_revision import RevisionClock
from core.library_revision_async import LibraryRevisionWatcher
from core.library_async import LibraryReader
import curation_snapshot_selftest as fixture


class NativeRevisions(fixture.Fixture):
    def test_commit_visible_before_future_callback_finishes(self):
        before=self.service.revision_snapshot()
        entered,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        started,allow=threading.Event(),threading.Event(); self.addCleanup(allow.set)
        original=LibraryRepository.register_files_minimal
        def blocked(repo,*args,**kwargs):
            started.set(); allow.wait(3); return original(repo,*args,**kwargs)
        with patch.object(LibraryRepository,'register_files_minimal',blocked):
            future=self.service.submit('register_files_minimal',[dict(path='C:/private/a.png',filename='a.png',size=1,created=1)])
            self.assertTrue(started.wait(3))
            self.assertEqual(self.service.revision_snapshot(),before)
            future.add_done_callback(lambda future:(entered.set(),release.wait(3)))
            allow.set(); self.assertTrue(entered.wait(3))
            self.assertGreater(self.service.revision_snapshot()[1],before[1])
            release.set()
        self.assertTrue(future.result(3))

    def test_rollback_does_not_advance_revision(self):
        self.register('C:/private/a.png'); before=self.service.revision_snapshot()
        original=LibraryRepository.edit_curation
        def fail(repo,*args,**kwargs):
            original(repo,*args,**kwargs); raise sqlite3.OperationalError('private rollback')
        with patch.object(LibraryRepository,'edit_curation',fail):
            with self.assertRaises(Exception): self.service.submit('edit_curation','C:/private/a.png',add_tags=['new']).result(3)
        self.assertEqual(self.service.revision_snapshot(),before)
        self.assertEqual(self.service.read('get_all_tags'),[])

    def test_empty_write_does_not_advance_revision(self):
        before=self.service.revision_snapshot()
        self.service.submit('register_files_minimal',[]).result(3)
        self.assertEqual(self.service.revision_snapshot(),before)

    def test_cancelled_queue_item_does_not_advance_revision(self):
        before=self.service.revision_snapshot()
        entered,release=threading.Event(),threading.Event(); self.addCleanup(release.set)
        original=LibraryRepository.register_files_minimal
        def blocked(repo,*args,**kwargs):
            entered.set(); release.wait(3); return original(repo,*args,**kwargs)
        with patch.object(LibraryRepository,'register_files_minimal',blocked):
            first=self.service.submit('register_files_minimal',[])
            self.assertTrue(entered.wait(3))
            second=self.service.submit('register_files_minimal',[dict(path='C:/private/a.png',filename='a.png',size=1,created=1)])
            self.assertTrue(second.cancel()); release.set(); first.result(3)
            self.service.submit('register_files_minimal',[]).result(3)
        self.assertEqual(self.service.revision_snapshot(),before)
        self.assertEqual(self.service.read('count_files'),0)

    def test_external_commit_observed_on_persistent_owner(self):
        self.register('C:/private/a.png'); before=self.service.revision_snapshot()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('UPDATE files SET rating=4'); conn.commit()
        self.service.submit('register_files_minimal',[]).result(3)
        after=self.service.revision_snapshot()
        self.assertEqual(after[0],before[0]); self.assertGreater(after[1],before[1]); self.assertTrue(after[2])

    def test_external_rollback_is_not_a_change(self):
        self.register('C:/private/a.png'); before=self.service.revision_snapshot()
        conn=sqlite3.connect(self.service.db_path)
        try: conn.execute('UPDATE files SET rating=4'); conn.rollback()
        finally: conn.close()
        self.service.submit('register_files_minimal',[]).result(3)
        self.assertEqual(self.service.revision_snapshot(),before)

    def test_reopened_service_has_new_epoch(self):
        before=self.service.revision_snapshot(); self.service.close()
        reopened=LibraryService(self.service.db_path)
        try: self.assertNotEqual(reopened.revision_snapshot()[0],before[0])
        finally: reopened.close()

    def test_owner_closure_marks_monitor_unavailable(self):
        before=self.service.revision_snapshot(); self.service.close()
        after=self.service.revision_snapshot()
        self.assertEqual(after[0],before[0]); self.assertGreater(after[1],before[1]); self.assertFalse(after[2])

    def test_snapshot_has_no_gui_sql_or_stat(self):
        with (patch('core.library_service.open_reader',side_effect=AssertionError('GUI SQL')),
              patch('os.stat',side_effect=AssertionError('GUI stat'))):
            values=[self.service.revision_snapshot() for _ in range(1000)]
        self.assertTrue(all(value==values[0] for value in values))

    def test_monitor_failure_and_recovery_change_health_once(self):
        clock=RevisionClock()
        class Broken:
            def execute(self,sql): raise sqlite3.OperationalError('private monitor error')
        class Healthy:
            def execute(self,sql): return self
            def fetchone(self): return (5,)
        clock.poll(Broken()); failed=clock.snapshot()
        self.assertEqual(failed[1:],(1,False))
        clock.poll(Broken()); self.assertEqual(clock.snapshot(),failed)
        clock.poll(Healthy()); restored=clock.snapshot()
        self.assertEqual(restored[1:],(2,True))
        clock.poll(Healthy()); self.assertEqual(clock.snapshot(),restored)


class RevisionQt(fixture.Fixture):
    setUpClass=classmethod(fixture.CurationQt.setUpClass.__func__)
    def setUp(self):
        super().setUp(); self.watchers=[]; self.views=[]; self.modules=[]; self.releases=[]
        self.addCleanup(self.stop)

    def stop(self):
        for release in self.releases: release.set()
        for watcher in self.watchers: watcher.close()
        for view in self.views:
            view.shutdown(); view.close()
            for thread in view.preview._threads: thread.join(3)
            for reader in (view.curation_reader,view.tags_reader):
                for thread in reader._threads: thread.join(3)
            view.deleteLater()
        for module in self.modules:
            module.on_unload(); self.assertTrue(fixture.pump(module.shutdown_ready))
            view=getattr(module,'view',None) or getattr(module,'view_widget',None)
            if view: view.close(); view.deleteLater()
        self.app.processEvents()

    def watcher(self,**kwargs):
        watcher=LibraryRevisionWatcher(self.service,**kwargs); self.watchers.append(watcher)
        return watcher

    def viewer(self,path):
        from modules.gallery.ui.viewer_window import AdvancedViewer
        view=AdvancedViewer([path],service=self.service); self.views.append(view)
        self.assertTrue(fixture.pump(lambda:view._curation_ready and not view.tag_completions.busy))
        return view

    def test_busy_consumer_coalesces_multiple_commits_on_gui(self):
        busy=[True]; watcher=self.watcher(busy=lambda:busy[0]); delivered=[]
        watcher.changed.connect(lambda value:delivered.append((value,threading.get_ident())))
        for n in range(3): self.register(f'C:/private/{n}.png'); watcher.poll()
        self.assertEqual(delivered,[]); busy[0]=False; watcher.poll(); watcher.poll()
        self.assertEqual(delivered,[(self.service.revision_snapshot(),threading.get_ident())])

    def test_long_read_is_not_cancelled_by_revision_watcher(self):
        entered,release=threading.Event(),threading.Event(); self.releases.append(release)
        original=LibraryRepository.count_files
        def hold(repo): entered.set(); release.wait(3); return original(repo)
        reader=LibraryReader(self.service); self.addCleanup(reader.close)
        watcher=self.watcher(busy=lambda:reader._active); delivered=[]; watcher.changed.connect(delivered.append)
        with patch.object(LibraryRepository,'count_files',hold):
            reader.request('count_files'); self.assertTrue(entered.wait(3))
            self.register('C:/private/a.png'); watcher.poll()
            self.assertFalse(reader._cancel.is_set()); self.assertEqual(delivered,[])
            release.set(); self.assertTrue(fixture.pump(lambda:not reader._active))
            watcher.poll(); self.assertEqual(len(delivered),1)
        for thread in reader._threads: thread.join(3)

    def test_close_discards_pending_and_stops_timer(self):
        watcher=self.watcher(busy=lambda:True); delivered=[]; watcher.changed.connect(delivered.append)
        self.register('C:/private/a.png'); watcher.poll(); watcher.close(); watcher.close(); watcher.poll()
        self.assertEqual(delivered,[]); self.assertFalse(watcher.timer.isActive())

    def test_initial_read_gap_is_not_lost(self):
        before=self.service.revision_snapshot(); self.register('C:/private/a.png')
        watcher=self.watcher(initial=before); delivered=[]; watcher.changed.connect(delivered.append)
        watcher.poll(); self.assertEqual(delivered,[self.service.revision_snapshot()])

    def test_two_open_viewers_update_without_file_reextraction(self):
        path='C:/private/a.png'; self.register(path)
        views=[self.viewer(path),self.viewer(path)]
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract',side_effect=AssertionError('curation reextract')):
            self.service.submit('edit_curation',path,add_tags=['curated'],rating=4).result(3)
            self.assertTrue(fixture.pump(lambda:all(view._current_rating==4 and view.tags_layout.count()==1 for view in views)))
        self.assertTrue(all(view._file_tags==() for view in views))

    def test_authoritative_empty_and_zero_refresh_other_view(self):
        path='C:/private/a.png'; self.register(path)
        self.service.submit('edit_curation',path,add_tags=['old'],rating=4).result(3)
        view=self.viewer(path)
        self.service.submit('edit_curation',path,remove_tags=['old'],rating=0).result(3)
        self.assertTrue(fixture.pump(lambda:view._current_rating==0 and view.tags_layout.count()==0 and view._curation_ready))

    def test_external_rating_commit_refreshes_viewer(self):
        path='C:/private/a.png'; self.register(path); view=self.viewer(path)
        with closing(sqlite3.connect(self.service.db_path)) as conn: conn.execute('UPDATE files SET rating=3'); conn.commit()
        self.assertTrue(fixture.pump(lambda:view._current_rating==3))

    def librarian(self):
        from modules.librarian.module import LibrarianModule
        module=LibrarianModule(); module._auto_scan_pending=False
        module.on_load(dict(data_service=self.service)); module.get_view(); self.modules.append(module)
        self.assertTrue(fixture.pump(lambda:module._overview_ready and not module._revision_busy()))
        return module

    def test_librarian_preserves_page_filter_and_tag_cursor(self):
        paths=[f'C:/private/{n:03d}.png' for n in range(220)]
        self.register(*paths); self.service.submit('add_watched_folder','C:/private').result(3)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.executemany('INSERT INTO tags(name) VALUES (?)',[(f'tag-{n:03d}',) for n in range(450)]); conn.commit()
        module=self.librarian(); module.load_folder_paths('C:/private'); module._request_selection(1)
        self.assertTrue(fixture.pump(lambda:not module._revision_busy()))
        module.tag_completions.next(); self.assertTrue(fixture.pump(lambda:not module.tag_completions.busy))
        cursor=list(module.tag_completions.cursors)
        self.service.submit('edit_curation',paths[0],add_tags=['new'],rating=4).result(3)
        self.assertTrue(fixture.pump(lambda:module.revisions._observed==self.service.revision_snapshot() and not module._revision_busy()))
        self.assertEqual(module.current_folder_path,'C:/private'); self.assertEqual(module.current_page,1)
        self.assertEqual(module.tag_completions.cursors,cursor)

    def test_librarian_external_selection_survives_unrelated_commit(self):
        module=self.librarian(); module.run_headless(['C:/external/a.png','C:/external/b.png'])
        before=module._external_paths
        self.register('C:/other/new.png')
        self.assertTrue(fixture.pump(lambda:module.revisions._observed==self.service.revision_snapshot() and not module._revision_busy()))
        self.assertEqual(module._external_paths,before)
        self.assertEqual(module.total_paths,list(before))

    def test_librarian_clamps_page_when_external_commit_removes_last_page(self):
        paths=[f'C:/private/{n:03d}.png' for n in range(220)]
        self.register(*paths); self.service.submit('add_watched_folder','C:/private').result(3)
        module=self.librarian(); module.load_folder_paths('C:/private'); module._request_selection(2)
        self.assertTrue(fixture.pump(lambda:not module._revision_busy()))
        self.assertEqual(module.current_page,2)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.executemany('UPDATE files SET is_offline=1 WHERE path=?',[(path,) for path in paths[200:]])
            conn.commit()
        self.assertTrue(fixture.pump(lambda:module.current_page==1 and module._browse_ready))
        self.assertEqual(module._selection_count,200)
        self.assertEqual(module.total_paths,paths[100:200])

    def test_gallery_module_refreshes_custom_curation(self):
        from modules.gallery.module import GalleryModule
        path='C:/private/a.png'; self.register(path)
        module=GalleryModule(); module.on_load(dict(data_service=self.service)); module.load_image_set([path]); self.modules.append(module)
        grid=module.view_widget.grid
        self.assertTrue(fixture.pump(lambda:not grid.reader._active))
        self.service.submit('edit_curation',path,rating=4).result(3)
        self.assertTrue(fixture.pump(lambda:not grid._loading and grid.grid_layout.itemAt(0).widget().rating==4))
        self.assertEqual(module.state._custom_paths,[path])

    def test_destroyed_owner_closes_watcher(self):
        parent=QWidget(); watcher=self.watcher(parent=parent)
        parent.deleteLater(); QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        self.assertTrue(watcher._closed)


if __name__=='__main__': unittest.main()
