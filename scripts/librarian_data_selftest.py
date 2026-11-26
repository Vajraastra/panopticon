"""Lecturas Librarian con SQLite temporal y Qt offscreen; sin biblioteca real."""
import json
import os
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from core.event_bus import EventBus
from core.library_connections import open_reader
from core.library_repository import LibraryRepository
from core.library_service import LibraryError
from core.theme_manager import ThemeManager
from modules.librarian.module import LibrarianModule
import curation_snapshot_selftest as helpers


class SelectionSQL(helpers.Fixture):
    def test_overview_uses_three_selects_and_detached_values(self):
        self.register('C:/images/a.png')
        self.service.submit('add_watched_folder', 'C:/images').result(3)
        self.service.submit('edit_curation', 'C:/images/a.png', add_tags=['alpha']).result(3)
        with closing(open_reader(self.service.db_path)) as conn:
            sql = []
            conn.set_trace_callback(sql.append)
            value = LibraryRepository(conn).get_library_overview()
            self.assertEqual(value, dict(folders=['C:/images'], count=1, tags=['alpha']))
            self.assertEqual(len(sql), 3)
        value['folders'].clear()
        self.assertEqual(self.service.read('get_library_overview')['folders'], ['C:/images'])

    def test_overview_stays_coherent_across_concurrent_commit(self):
        self.register('C:/images/a.png')
        self.service.submit('add_watched_folder', 'C:/images').result(3)
        from core import library_service
        real_open = library_service.open_reader
        def reader(path):
            conn = real_open(path)
            def trace(sql):
                if sql.startswith('SELECT count(*) FROM files'):
                    self.register('C:/images/b.png')
                    self.service.submit('add_watched_folder', 'C:/other').result(3)
                    self.service.submit('edit_curation', 'C:/images/a.png', add_tags=['new']).result(3)
            conn.set_trace_callback(trace)
            return conn
        with patch.object(library_service, 'open_reader', reader):
            self.assertEqual(self.service.read('get_library_overview'),
                             dict(folders=['C:/images'], count=1, tags=[]))
        self.assertEqual(self.service.read('get_library_overview')['count'], 2)

    def test_folder_count_and_sample_share_snapshot(self):
        self.register('C:/images/a.png', 'C:/elsewhere/a.png')
        from core import library_service
        real_open = library_service.open_reader
        def reader(path):
            conn = real_open(path)
            conn.set_trace_callback(lambda sql: self.register('C:/images/b.png')
                                    if sql.startswith('SELECT path FROM files') else None)
            return conn
        with patch.object(library_service, 'open_reader', reader):
            value = self.service.read('get_library_selection', folder='C:/images', limit=1)
        self.assertEqual(value, dict(count=1, paths=['C:/images/a.png']))
        self.assertEqual(self.service.read('get_library_selection', folder='C:/images')['count'], 2)

    def test_search_sample_counts_all_and_keeps_existing_terms_semantics(self):
        self.register('C:/images/alpha.png', 'C:/images/alpha-beta.png', 'C:/images/beta.png')
        value = self.service.read('get_library_selection', terms=['alpha'], limit=1)
        self.assertEqual((value['count'], len(value['paths'])), (2, 1))
        self.assertEqual(self.service.read('get_library_selection', terms=['alpha', 'beta'])['paths'],
                         ['C:/images/alpha-beta.png'])

    def test_invalid_selection_fails_before_sql(self):
        with closing(open_reader(self.service.db_path)) as conn:
            sql = []
            conn.set_trace_callback(sql.append)
            repo = LibraryRepository(conn)
            for args in (dict(terms='abc'), dict(terms=[''] ), dict(terms=['a'] * 257),
                         dict(terms=['a' * 257]), dict(folder=3), dict(folder=''),
                         dict(limit=True), dict(limit=0), dict(limit=10001)):
                with self.assertRaises(ValueError):
                    repo.get_library_selection(**args)
            self.assertEqual(repo.get_library_selection(), dict(count=0, paths=[]))
            self.assertEqual(sql, [])


class LibrarianQt(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.modules = []
        self.releases = []
        self.addCleanup(self.shutdown)
        self.bus = EventBus()

    def shutdown(self):
        for release in self.releases:
            release.set()
        for module in self.modules:
            module.on_unload()
            self.assertTrue(helpers.pump(module.shutdown_ready))
            for reader in (module.overview_reader, module.browse_reader, module.scan_reader, module.export_reader):
                if reader._thread:
                    reader._thread.join(3)
                    self.assertFalse(reader._thread.is_alive())
            module.view.close()
            module.view.deleteLater()
        QApplication.processEvents()

    def view(self, auto=False):
        module = LibrarianModule()
        module._auto_scan_pending = auto
        module.on_load(dict(data_service=self.service, event_bus=self.bus, theme_manager=ThemeManager()))
        self.modules.append(module)
        module.get_view()
        return module

    def ready(self, module):
        self.assertTrue(helpers.pump(lambda: module._overview_ready and module._browse_ready))

    def block(self, operation, match=lambda kwargs: True):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        read = self.service.read_worker
        calls = []
        def blocked(name, *args, **kwargs):
            self.assertNotEqual(threading.current_thread(), threading.main_thread())
            calls.append((name, kwargs))
            if name == operation and match(kwargs):
                entered.set()
                release.wait(3)
            return read(name, *args, **kwargs)
        return entered, release, calls, patch.object(self.service, 'read_worker', blocked)

    def test_initial_read_never_blocks_qt_and_populates_single_vocabulary(self):
        self.register('C:/images/a.png')
        self.service.submit('edit_curation', 'C:/images/a.png', add_tags=['alpha']).result(3)
        entered, release, calls, hook = self.block('get_library_overview')
        with hook:
            before = time.monotonic()
            module = self.view()
            self.assertLess(time.monotonic() - before, .3)
            self.assertTrue(entered.wait(1))
            tick = []
            QTimer.singleShot(0, lambda: tick.append(True))
            self.assertTrue(helpers.pump(lambda: tick))
            release.set()
            self.ready(module)
        self.assertEqual([name for name, _ in calls].count('get_library_overview'), 1)
        self.assertFalse(any(name == 'get_all_tags' for name, _ in calls))
        self.assertIn('1', module.lbl_stats.text())

    def test_latest_folder_wins_and_only_latest_pending_runs(self):
        self.register('C:/a/one.png', 'C:/b/two.png', 'C:/c/three.png')
        module = self.view()
        self.ready(module)
        entered, release, calls, hook = self.block('get_library_page', lambda kw: kw['folder'] == 'C:/a')
        with hook:
            module.load_folder_paths('C:/a')
            self.assertTrue(entered.wait(1))
            module.load_folder_paths('C:/b')
            module.load_folder_paths('C:/c')
            self.assertFalse(module.btn_export_curation.isEnabled())
            release.set()
            self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertEqual([kw['folder'] for name, kw in calls], ['C:/a', 'C:/c'])
        self.assertTrue(all(kw['limit'] == 1 for _, kw in calls))  # solo el recuento
        self.assertEqual(module._folder_count, 1)
        self.assertIn('c:', module.lbl_folder_stats.text().lower().replace('📂 ', ''))
        self.assertTrue(module.btn_export_curation.isEnabled())

    def test_failure_is_visible_and_recovers_without_stale_actions(self):
        self.register('C:/images/a.png')
        module = self.view()
        self.ready(module)
        with patch.object(self.service, 'read', side_effect=LibraryError('read', 'fixture_failure')):
            module.load_folder_paths('C:/images')
            self.assertTrue(helpers.pump(lambda: 'fixture_failure' in module.lbl_folder_stats.text()))
        self.assertFalse(module.btn_export_curation.isEnabled())
        self.assertIsNone(module._folder_count)
        module.load_folder_paths('C:/images')
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertTrue(module.btn_export_curation.isEnabled())

    def test_overview_failure_keeps_confirmed_sidebar_and_recovers(self):
        self.service.submit('add_watched_folder', 'C:/images').result(3)
        module = self.view()
        self.ready(module)
        with patch.object(self.service, 'read', side_effect=LibraryError('read', 'busy_fixture')):
            module.update_global_stats()
            self.assertTrue(helpers.pump(lambda: 'busy_fixture' in module.lbl_stats.text()))
        self.assertEqual(module.folder_list.item(0).text(), 'C:/images')
        module.update_global_stats()
        self.ready(module)

    def test_removed_folder_clears_selection_and_late_data(self):
        self.register('C:/images/a.png')
        self.service.submit('add_watched_folder', 'C:/images').result(3)
        module = self.view()
        self.ready(module)
        module.load_folder_paths('C:/images')
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.service.submit('remove_watched_folder', 'C:/images').result(3)
        module.update_global_stats()
        self.assertTrue(helpers.pump(lambda: module.current_folder_path is None))
        self.assertIsNone(module._folder_count)
        self.assertFalse(module.btn_export_curation.isEnabled())

    def test_unload_cancels_reads_timer_and_event_subscription_without_wait(self):
        module = self.view()
        self.ready(module)
        entered, release, _, hook = self.block('get_library_page')
        with hook:
            module.load_folder_paths('C:/images')
            self.assertTrue(entered.wait(1))
            module._on_library_changed('C:/images')
            before = time.monotonic()
            module.on_unload()
            self.assertLess(time.monotonic() - before, .1)
            self.assertTrue(module.browse_reader._cancel.is_set())
            self.assertFalse(module._dirty_timer.isActive())
            self.assertNotIn('library_changed', self.bus._subscribers)
            status = module.lbl_folder_stats.text()
            release.set()
            module.browse_reader._thread.join(3)
            QApplication.processEvents()
        self.assertEqual(module.lbl_folder_stats.text(), status)

    def test_scan_uses_fresh_async_folders_and_coalesces_double_start(self):
        self.service.submit('add_watched_folder', 'C:/images').result(3)
        module = self.view()
        self.ready(module)
        self.service.submit('add_watched_folder', 'C:/other').result(3)
        entered, release, calls, hook = self.block('get_watched_folders')
        with hook, patch.object(module, '_start_scan') as start:
            module.toggle_scan(sync_only=True)
            self.assertTrue(entered.wait(1))
            module.toggle_scan()
            release.set()
            self.assertTrue(helpers.pump(lambda: start.called))
            start.assert_called_once_with(['C:/images', 'C:/other'], True, False)
        self.assertEqual(len(calls), 1)

    def test_scan_error_does_not_start_worker_and_allows_retry(self):
        module = self.view()
        self.ready(module)
        with patch.object(self.service, 'read', side_effect=LibraryError('scan', 'scan_fixture')), \
             patch.object(module, '_start_scan') as start:
            module.toggle_scan()
            self.assertTrue(helpers.pump(lambda: module._scan_request is None))
            start.assert_not_called()
            self.assertIn('scan_fixture', module.lbl_status.text())
        self.service.submit('add_watched_folder', 'C:/images').result(3)
        with patch.object(module, '_start_scan') as start:
            module.toggle_scan()
            self.assertTrue(helpers.pump(lambda: start.called))

    def test_backfill_started_during_scan_read_prevents_late_scan_start(self):
        from unittest.mock import Mock
        self.service.submit('add_watched_folder', 'C:/images').result(3)
        module = self.view()
        self.ready(module)
        entered, release, _, hook = self.block('get_watched_folders')
        with hook, patch.object(module, '_start_scan') as start:
            module.toggle_scan()
            self.assertTrue(entered.wait(1))
            module.backfill_thread = Mock()
            module.backfill_thread.isRunning.return_value = True
            release.set()
            self.assertTrue(helpers.pump(lambda: module._scan_request is None))
            start.assert_not_called()
            module.backfill_thread = None

    def test_auto_scan_is_scheduled_only_after_confirmed_overview_and_cancelled_on_unload(self):
        self.service.submit('add_watched_folder', 'C:/images').result(3)
        entered, release, _, hook = self.block('get_library_overview')
        with hook:
            module = self.view(auto=True)
            self.assertTrue(entered.wait(1))
            self.assertFalse(module._auto_scan_timer.isActive())
            release.set()
            self.ready(module)
        self.assertTrue(module._auto_scan_timer.isActive())
        module.on_unload()
        self.assertFalse(module._auto_scan_timer.isActive())

    def exercise_automatic_scan(self, *, close=False, check_controls=False):
        from PySide6.QtWidgets import QMessageBox
        from core.inventory import InventoryResult, InventoryStatus
        from modules.librarian.logic.indexer import IndexerWorker
        entered = threading.Event()
        def discovery(worker, *_):
            entered.set()
            if not worker._inventory_cancel.wait(3):
                raise RuntimeError('fixture cancellation timeout')
            result = InventoryResult()
            result.fail(InventoryStatus.CANCELLED)
            return result
        module = self.view()
        self.ready(module)
        with patch.object(IndexerWorker, '_discover_tree', discovery), \
             patch.object(QMessageBox, 'warning'), patch.object(QMessageBox, 'information'):
            module._start_scan([self.temp.name], True, True)
            self.assertTrue(entered.wait(1))
            try:
                tick = []
                QTimer.singleShot(0, lambda: tick.append(True))
                self.assertTrue(helpers.pump(lambda: tick))
                if check_controls:
                    self.assertEqual(module.btn_scan.text(), module.tr('lib.stop_indexer', '🛑 Stop Scanning'))
                    self.assertFalse(module.btn_sync.isEnabled())
                    self.assertFalse(module.btn_backfill.isEnabled())
                    self.assertFalse(module.progress_bar.isHidden())
                if close:
                    module.on_unload()
                else:
                    module.btn_scan.click()
                self.assertTrue(helpers.pump(lambda: module.indexer_thread.wait(0)))
                self.assertEqual(module.indexer_thread.outcome, InventoryStatus.CANCELLED)
            finally:
                module.indexer_thread.stop()
                self.assertTrue(helpers.pump(lambda: module.indexer_thread.wait(0)))
                QApplication.processEvents()

    def test_automatic_scan_exposes_running_controls(self):
        self.exercise_automatic_scan(check_controls=True)

    def test_automatic_scan_can_be_cancelled_from_button(self):
        self.exercise_automatic_scan()

    def test_automatic_scan_shutdown_finishes_worker(self):
        self.exercise_automatic_scan(close=True)

    def test_large_folder_reports_full_count_and_sql_page(self):
        paths = [f'C:/images/{i}.png' for i in range(10001)]
        self.register(*paths)
        module = self.view()
        self.ready(module)
        module.load_folder_paths('C:/images')
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertEqual(module._folder_count, 10001)
        self.assertIn('10001', module.lbl_folder_stats.text())


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
