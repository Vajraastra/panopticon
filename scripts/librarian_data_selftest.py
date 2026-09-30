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
                if sql == 'SELECT count(*) FROM files':
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
            for reader in (module.overview_reader, module.browse_reader, module.scan_reader, module.handoff_reader, module.export_reader):
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
        read = self.service.read
        calls = []
        def blocked(name, *args, **kwargs):
            self.assertNotEqual(threading.current_thread(), threading.main_thread())
            calls.append((name, kwargs))
            if name == operation and match(kwargs):
                entered.set()
                release.wait(3)
            return read(name, *args, **kwargs)
        return entered, release, calls, patch.object(self.service, 'read', blocked)

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
        self.assertEqual([name for name, _ in calls], ['get_library_overview'])
        self.assertEqual(module.completer_model.stringList(), ['alpha'])
        self.assertEqual(module.tag_list.item(0).text(), 'alpha')
        self.assertIn('1', module.lbl_stats.text())

    def test_latest_folder_wins_and_only_latest_pending_runs(self):
        self.register('C:/a/one.png', 'C:/b/two.png', 'C:/c/three.png')
        module = self.view()
        self.ready(module)
        module.combo_actions.setCurrentIndex(1)
        entered, release, calls, hook = self.block('get_library_page', lambda kw: kw['folder'] == 'C:/a')
        with hook:
            module.load_folder_paths('C:/a')
            self.assertTrue(entered.wait(1))
            module.load_folder_paths('C:/b')
            module.load_folder_paths('C:/c')
            self.assertFalse(module.btn_execute.isEnabled())
            self.assertEqual(module._get_active_paths(), [])
            release.set()
            self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertEqual([kw['folder'] for name, kw in calls], ['C:/a', 'C:/c'])
        self.assertEqual(module.total_paths, ['C:/c/three.png'])
        self.assertTrue(module.btn_execute.isEnabled())

    def test_clearing_search_invalidates_inflight_result(self):
        self.register('C:/images/alpha.png')
        module = self.view()
        self.ready(module)
        entered, release, _, hook = self.block('get_library_page')
        with hook:
            module.active_tags = ['alpha']
            module.perform_search()
            self.assertTrue(entered.wait(1))
            module.active_tags.clear()
            module.perform_search()
            release.set()
            self.assertTrue(helpers.pump(lambda: not module.browse_reader._active))
        self.assertEqual(module.total_paths, [])
        self.assertTrue(module._browse_ready)

    def test_latest_search_captures_mutable_terms_and_replaces_pending(self):
        self.register('C:/images/alpha.png', 'C:/images/beta.png', 'C:/images/gamma.png')
        module = self.view()
        self.ready(module)
        entered, release, calls, hook = self.block('get_library_page',
                                                  lambda kw: kw['terms'] == ('alpha',))
        with hook:
            module.active_tags[:] = ['alpha']
            module.perform_search()
            self.assertTrue(entered.wait(1))
            module.active_tags[:] = ['beta']
            module.perform_search()
            module.active_tags[:] = ['gamma']
            module.perform_search()
            release.set()
            self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertEqual([kw['terms'] for _, kw in calls], [('alpha',), ('gamma',)])
        self.assertEqual(module.total_paths, ['C:/images/gamma.png'])

    def test_failure_is_visible_and_recovers_without_stale_actions(self):
        self.register('C:/images/a.png')
        module = self.view()
        self.ready(module)
        module.combo_actions.setCurrentIndex(1)
        with patch.object(self.service, 'read', side_effect=LibraryError('read', 'fixture_failure')):
            module.load_folder_paths('C:/images')
            self.assertTrue(helpers.pump(lambda: 'fixture_failure' in module.lbl_folder_stats.text()))
        self.assertFalse(module.btn_execute.isEnabled())
        self.assertEqual(module.total_paths, [])
        module.load_folder_paths('C:/images')
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertTrue(module.btn_execute.isEnabled())

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
        self.assertEqual(module.total_paths, [])

    def test_explicit_page_handoff_does_not_read_db_or_truncate(self):
        module = self.view()
        self.ready(module)
        paths = [f'C:/images/{i}.png' for i in range(100)]
        module.combo_scope.setCurrentIndex(1)
        module.total_paths, module._browse_ready = paths, True
        received = []
        module.request_open_gallery.connect(lambda rows, title: received.append(rows))
        with patch.object(self.service, 'read', side_effect=AssertionError('GUI read')):
            module.send_to_gallery()
        self.assertEqual(received, [paths])
        self.assertIn('1,000', module.btn_execute.toolTip())

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

    def test_large_folder_reports_full_count_and_sql_page(self):
        paths = [f'C:/images/{i}.png' for i in range(10001)]
        self.register(*paths)
        module = self.view()
        self.ready(module)
        module.load_folder_paths('C:/images')
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertEqual(len(module.total_paths), 100)
        self.assertEqual(module._selection_count, 10001)
        self.assertIn('Page contains 100 of 10001', module.lbl_folder_stats.text())


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
