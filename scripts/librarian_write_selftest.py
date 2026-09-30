"""Escrituras Librarian con Qt offscreen y SQLite temporal; sin biblioteca real."""
import os
import sys
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from core.event_bus import EventBus
from core.library_connections import open_reader
from core.library_repository import LibraryRepository
from core.library_service import LibraryError
import curation_snapshot_selftest as helpers
import librarian_data_selftest as read_helpers


class FolderWrites(helpers.Fixture):
    view = read_helpers.LibrarianQt.view
    ready = read_helpers.LibrarianQt.ready
    shutdown = read_helpers.LibrarianQt.shutdown

    def setUp(self):
        super().setUp()
        self.modules = []
        self.releases = []
        self.addCleanup(self.shutdown)
        self.bus = EventBus()

    def block_write(self, operation):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = getattr(LibraryRepository, operation)
        calls = []
        def blocked(repo, path):
            calls.append((path, threading.get_ident()))
            entered.set()
            if not release.wait(3):
                raise RuntimeError('fixture timeout')
            return original(repo, path)
        return patch.object(LibraryRepository, operation, blocked), entered, release, calls

    def add(self, module, path):
        with patch.object(QFileDialog, 'getExistingDirectory', return_value=path):
            module.add_folder()

    def remove(self, module, path, answer=QMessageBox.Yes):
        module.folder_list.setCurrentRow(next(
            i for i in range(module.folder_list.count()) if module.folder_list.item(i).text() == path))
        with patch.object(QMessageBox, 'question', return_value=answer):
            module.remove_folder()

    def test_add_keeps_qt_responsive_and_coalesces_duplicate_actions(self):
        module = self.view()
        self.ready(module)
        block, entered, release, calls = self.block_write('add_watched_folder')
        with block, patch.object(module.db, 'add_watched_folder', side_effect=AssertionError('sync facade')):
            tick = []
            QTimer.singleShot(0, lambda: tick.append(True))
            start = time.monotonic()
            self.add(module, 'C:/new')
            self.assertLess(time.monotonic() - start, .3)
            self.assertTrue(entered.wait(1))
            self.assertTrue(helpers.pump(lambda: tick))
            self.assertFalse(module.btn_add.isEnabled())
            self.assertFalse(module.btn_remove.isEnabled())
            self.add(module, 'C:/duplicate')
            self.assertEqual([c[0] for c in calls], ['C:/new'])
            self.assertNotEqual(calls[0][1], threading.get_ident())
            self.assertEqual(self.service.read('get_watched_folders'), [])
            self.assertNotIn('change saved', module.lbl_folder_write.text())
            release.set()
            self.assertTrue(helpers.pump(lambda: module._folder_write is None and module.folder_list.count() == 1))
        self.assertTrue(module.btn_add.isEnabled())
        self.assertEqual(self.service.read('get_watched_folders'), ['C:/new'])

    def test_remove_captures_path_and_preserves_new_selection(self):
        for folder in ('C:/A', 'C:/B'):
            self.service.submit('add_watched_folder', folder).result(3)
        self.register('C:/A/a.png', 'C:/B/b.png')
        self.service.submit('edit_curation', 'C:/A/a.png', add_tags=['alpha'], rating=4).result(3)
        module = self.view()
        self.ready(module)
        module.load_folder_paths('C:/A')
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        block, entered, release, calls = self.block_write('remove_watched_folder')
        with block, patch.object(module.db, 'remove_watched_folder', side_effect=AssertionError('sync facade')):
            self.remove(module, 'C:/A')
            self.assertTrue(entered.wait(1))
            module.load_folder_paths('C:/B')
            self.assertTrue(helpers.pump(lambda: module._browse_ready))
            self.assertEqual(self.service.read('count_files'), 2)
            release.set()
            self.assertTrue(helpers.pump(lambda: module._folder_write is None and module.folder_list.count() == 1
                                       and module._browse_ready))
        self.assertEqual([c[0] for c in calls], ['C:/A'])
        self.assertEqual(module.current_folder_path, 'C:/B')
        self.assertEqual(module.total_paths, ['C:/B/b.png'])
        with closing(open_reader(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM file_tags').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT count(*) FROM files_fts').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT count(*) FROM files WHERE is_offline=1').fetchone()[0], 1)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_remove_current_folder_clears_confirmed_selection_only_after_commit(self):
        self.service.submit('add_watched_folder', 'C:/A').result(3)
        self.register('C:/A/a.png')
        module = self.view()
        self.ready(module)
        module.load_folder_paths('C:/A')
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.remove(module, 'C:/A')
        self.assertTrue(helpers.pump(lambda: module._folder_write is None and module._browse_ready))
        self.assertIsNone(module.current_folder_path)
        self.assertEqual(module._get_active_paths(), [])

    def test_removal_failure_rolls_back_files_curation_fts_and_folder(self):
        self.service.submit('add_watched_folder', 'C:/A').result(3)
        self.register('C:/A/a.png')
        self.service.submit('edit_curation', 'C:/A/a.png', add_tags=['alpha'], rating=4).result(3)
        module = self.view()
        self.ready(module)
        original = LibraryRepository.remove_watched_folder
        def fail_after_delete(repo, path):
            original(repo, path)
            return False
        with patch.object(LibraryRepository, 'remove_watched_folder', fail_after_delete):
            self.remove(module, 'C:/A')
            self.assertTrue(helpers.pump(lambda: module._folder_write is None))
        self.assertIn('operation_failed', module.lbl_folder_write.text())
        self.assertEqual(module.folder_list.count(), 1)
        self.assertEqual(self.service.read('get_watched_folders'), ['C:/A'])
        row = self.service.read('get_curation_snapshot', ['C:/A/a.png'])[0]
        self.assertEqual((row['rating'], row['tags']), (4, ('alpha',)))
        with closing(open_reader(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM files_fts').fetchone()[0], 1)
        self.remove(module, 'C:/A')
        self.assertTrue(helpers.pump(lambda: module._folder_write is None and module.folder_list.count() == 0))

    def test_queue_full_is_visible_without_losing_selection(self):
        self.service.submit('add_watched_folder', 'C:/A').result(3)
        module = self.view()
        self.ready(module)
        block, entered, release, _ = self.block_write('add_watched_folder')
        with block:
            first = self.service.submit('add_watched_folder', 'C:/block')
            self.assertTrue(entered.wait(1))
            queued = [self.service.submit('add_watched_folder', f'C:/queued/{i}') for i in range(64)]
            self.remove(module, 'C:/A')
            self.assertTrue(helpers.pump(lambda: module._folder_write is None))
            self.assertIn('queue_full', module.lbl_folder_write.text())
            self.assertTrue(module.btn_remove.isEnabled())
            self.assertEqual(module.folder_list.currentItem().text(), 'C:/A')
            release.set()
            self.assertTrue(first.result(3))
            self.assertTrue(all(f.result(3) for f in queued))
        self.assertIn('C:/A', self.service.read('get_watched_folders'))

    def test_submission_exception_restores_controls_and_reports_error(self):
        module = self.view()
        self.ready(module)
        with patch.object(self.service, 'submit', side_effect=LibraryError('add_watched_folder', 'service_closed')):
            self.add(module, 'C:/failure')
        self.assertIsNone(module._folder_write)
        self.assertTrue(module.btn_add.isEnabled())
        self.assertIn('service_closed', module.lbl_folder_write.text())
        self.assertEqual(self.service.read('get_watched_folders'), [])

    def test_unload_keeps_accepted_add_and_drops_late_delivery(self):
        module = self.view()
        self.ready(module)
        block, entered, release, _ = self.block_write('add_watched_folder')
        with block:
            self.add(module, 'C:/accepted')
            self.assertTrue(entered.wait(1))
            text = module.lbl_folder_write.text()
            start = time.monotonic()
            module.on_unload()
            self.assertLess(time.monotonic() - start, .1)
            release.set()
            self.service.submit('add_watched_folder', 'C:/barrier').result(3)
            QApplication.processEvents()
        self.assertIn('C:/accepted', self.service.read('get_watched_folders'))
        self.assertEqual(module.lbl_folder_write.text(), text)
        with patch.object(QFileDialog, 'getExistingDirectory') as dialog:
            module.add_folder()
            dialog.assert_not_called()

    def test_destroyed_qobject_keeps_accepted_remove(self):
        self.service.submit('add_watched_folder', 'C:/A').result(3)
        self.register('C:/A/a.png')
        module = self.view()
        self.ready(module)
        block, entered, release, _ = self.block_write('remove_watched_folder')
        with block:
            self.remove(module, 'C:/A')
            self.assertTrue(entered.wait(1))
            module.on_unload()
            module.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.modules.remove(module)
            module.view.close()
            module.view.deleteLater()
            release.set()
            self.service.submit('add_watched_folder', 'C:/barrier').result(3)
            QApplication.processEvents()
        self.assertEqual(self.service.read('count_files'), 0)
        self.assertNotIn('C:/A', self.service.read('get_watched_folders'))

    def test_cancelled_confirmation_and_unload_during_dialog_do_not_submit(self):
        self.service.submit('add_watched_folder', 'C:/A').result(3)
        module = self.view()
        self.ready(module)
        with patch.object(self.service, 'submit', side_effect=AssertionError('unexpected write')):
            self.remove(module, 'C:/A', QMessageBox.No)
            def close_in_dialog(*args):
                module.on_unload()
                return 'C:/never'
            with patch.object(QFileDialog, 'getExistingDirectory', close_in_dialog):
                module.add_folder()
        self.assertEqual(self.service.read('get_watched_folders'), ['C:/A'])

    def test_active_workers_and_pending_scan_block_folder_changes(self):
        module = self.view()
        self.ready(module)
        with patch.object(QFileDialog, 'getExistingDirectory') as dialog:
            for name in ('indexer_thread', 'backfill_thread', '_reindex_thread'):
                setattr(module, name, Mock(isRunning=Mock(return_value=True)))
                module.add_folder()
                setattr(module, name, None)
            module._scan_request = (False, False)
            module.add_folder()
            module._scan_request = None
            dialog.assert_not_called()
        self.assertIn('Wait for indexing', module.lbl_folder_write.text())

    def test_pending_write_defers_scan_backfill_dirty_reindex_and_auto_scan(self):
        module = self.view()
        self.ready(module)
        module._auto_scan_timer.start(500)
        block, entered, release, _ = self.block_write('add_watched_folder')
        with block:
            self.add(module, 'C:/new')
            self.assertTrue(entered.wait(1))
            self.assertFalse(module._auto_scan_timer.isActive())
            with patch.object(module.scan_reader, 'request', side_effect=AssertionError('scan while writing')):
                module.toggle_scan()
                module.toggle_backfill()
            module._dirty_folders.add('C:/dirty')
            module._flush_dirty_reindex()
            self.assertEqual(module._dirty_folders, {'C:/dirty'})
            self.assertTrue(module._dirty_timer.isActive())
            self.assertIsNone(module.backfill_thread)
            self.assertIsNone(module._reindex_thread)
            release.set()
            self.assertTrue(helpers.pump(lambda: module._folder_write is None))
        self.assertTrue(module._auto_scan_timer.isActive())

    def test_late_initial_overview_defers_auto_scan_until_write_finishes(self):
        module = self.view()
        self.ready(module)
        module._auto_scan_pending = True
        block, entered, release, _ = self.block_write('add_watched_folder')
        with block:
            self.add(module, 'C:/new')
            self.assertTrue(entered.wait(1))
            module._on_overview(0, dict(folders=['C:/old'], tags=[], count=0), None)
            self.assertFalse(module._auto_scan_timer.isActive())
            release.set()
            self.assertTrue(helpers.pump(lambda: module._folder_write is None))
        self.assertTrue(module._auto_scan_timer.isActive())

    def test_remove_changes_db_only_original_bytes_survive(self):
        path = Path(self.temp.name) / 'original.png'
        data = b'fixture-original-immutable'
        path.write_bytes(data)
        folder = path.parent.as_posix()
        self.register(path.as_posix())
        self.service.submit('add_watched_folder', folder).result(3)
        module = self.view()
        self.ready(module)
        self.remove(module, folder)
        self.assertTrue(helpers.pump(lambda: module._folder_write is None and module.folder_list.count() == 0))
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.service.read('count_files'), 0)

    def test_commit_invalidates_overview_captured_before_write(self):
        module = self.view()
        self.ready(module)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        read = self.service.read
        blocked = False
        def stale(name, *args, **kwargs):
            nonlocal blocked
            value = read(name, *args, **kwargs)
            if name == 'get_library_overview' and not blocked:
                blocked = True
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('fixture timeout')
            return value
        with patch.object(self.service, 'read', stale):
            module.update_global_stats()
            self.assertTrue(entered.wait(1))
            self.add(module, 'C:/new')
            self.assertTrue(helpers.pump(lambda: module._folder_write is None))
            self.assertEqual(module.folder_list.count(), 0)
            release.set()
            self.assertTrue(helpers.pump(lambda: module.folder_list.count() == 1))
        self.assertEqual(module.folder_list.item(0).text(), 'C:/new')


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
