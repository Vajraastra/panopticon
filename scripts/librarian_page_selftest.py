"""Páginas y envío explícito: SQLite temporal, lectores Qt, sin biblioteca real."""
import os
import json
import sys
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from core.library_connections import open_reader
from core.library_repository import LibraryRepository
from core.library_service import LibraryError
import curation_snapshot_selftest as helpers
import librarian_data_selftest as prior
from core.event_bus import EventBus


class PageSQL(helpers.Fixture):
    def test_every_page_beyond_old_cap_has_exact_stable_identity_order(self):
        paths = [f'C:/images/{i:05}.png' for i in range(10023)]
        self.register(*paths)
        received = []
        for offset in range(0, len(paths), 256):
            value = self.service.read('get_library_page', folder='C:/images', limit=256, offset=offset)
            self.assertEqual(value['count'], len(paths))
            self.assertLessEqual(len(value['paths']), 256)
            self.assertEqual(value['offset'], offset)
            received.extend(value['paths'])
        self.assertEqual(received, paths)

    def test_shrinking_selection_clamps_to_last_existing_page(self):
        paths = [f'C:/images/{i}.png' for i in range(203)]
        self.register(*paths)
        self.service.submit('remove_files', paths[103:]).result(3)
        value = self.service.read('get_library_page', folder='C:/images', offset=200)
        self.assertEqual((value['offset'], value['count'], value['paths']), (100, 103, paths[100:103]))

    def test_empty_or_deleted_selection_has_valid_page_zero(self):
        self.assertEqual(self.service.read('get_library_page'), dict(count=0, paths=[], offset=0, limit=100))
        value = self.service.read('get_library_page', folder='C:/missing', offset=100)
        self.assertEqual(value, dict(count=0, paths=[], offset=0, limit=100))

    def test_validation_precedes_sql(self):
        with closing(open_reader(self.service.db_path)) as conn:
            queries = []
            conn.set_trace_callback(queries.append)
            repo = LibraryRepository(conn)
            for kwargs in (dict(limit=True), dict(limit=0), dict(limit=257), dict(offset=True),
                           dict(offset=-1), dict(offset=1), dict(offset=2**63),
                           dict(terms=['']), dict(terms=['a'] * 257), dict(folder='')):
                with self.assertRaises(ValueError):
                    repo.get_library_page(**kwargs)
            for kwargs in (dict(limit=0), dict(limit=True), dict(limit=1001), dict(terms='alpha')):
                with self.assertRaises(ValueError):
                    repo.resolve_library_selection(**kwargs)
            self.assertEqual(queries, [])

    def test_search_semantics_match_legacy_and_terms_override_folder(self):
        paths = ['C:/images/alpha-x.png', 'C:/elsewhere/alpha-x.png', 'C:/images/alpha.png']
        self.register(*paths)
        expected = self.service.read('get_library_selection', terms=['alpha', 'x'])
        value = self.service.read('get_library_page', terms=['alpha', 'x'], folder='C:/ignored', limit=1)
        other = self.service.read('get_library_page', terms=['alpha', 'x'], offset=1, limit=1)
        self.assertEqual(value['count'], expected['count'])
        self.assertEqual(value['paths'] + other['paths'], expected['paths'])

    def test_escaped_folder_and_root_do_not_leak_neighbor(self):
        self.register('C:/a%_/one.png', 'C:/abc/one.png', 'C:/a%_/sub/two.png')
        value = self.service.read('get_library_page', folder='C:/a%_')
        self.assertEqual(value['paths'], ['C:/a%_/one.png', 'C:/a%_/sub/two.png'])
        self.assertEqual(self.service.read('get_library_page', folder='C:/')['count'], 3)

    def test_count_page_are_one_snapshot_across_writer_commit(self):
        self.register('C:/images/a.png')
        from core import library_service
        real_open = library_service.open_reader
        def reader(path):
            conn = real_open(path)
            conn.set_trace_callback(lambda sql: self.register('C:/images/b.png')
                                    if sql.startswith('SELECT path FROM files') else None)
            return conn
        with patch.object(library_service, 'open_reader', reader):
            value = self.service.read('get_library_page', folder='C:/images')
        self.assertEqual((value['count'], value['paths']), (1, ['C:/images/a.png']))
        self.assertEqual(self.service.read('get_library_page', folder='C:/images')['count'], 2)

    def test_large_complete_selection_fails_without_path_query_or_dml(self):
        self.register(*[f'C:/images/{i}.png' for i in range(1001)])
        with closing(open_reader(self.service.db_path)) as conn:
            queries = []
            conn.set_trace_callback(queries.append)
            with self.assertRaisesRegex(ValueError, 'selection_requires_streaming'):
                LibraryRepository(conn).resolve_library_selection(folder='C:/images')
            self.assertEqual(len(queries), 1)
            self.assertTrue(queries[0].startswith('SELECT count'))
            self.assertEqual(conn.total_changes, 0)

    def test_complete_small_selection_covers_all_pages_and_exact_limit(self):
        paths = [f'C:/images/{i}.png' for i in range(1000)]
        self.register(*paths)
        value = self.service.read('resolve_library_selection', folder='C:/images')
        self.assertEqual((value['count'], value['paths']), (1000, paths))

    def test_complete_resolution_snapshot_does_not_mix_concurrent_insert(self):
        self.register('C:/images/a.png')
        from core import library_service
        real_open = library_service.open_reader
        def reader(path):
            conn = real_open(path)
            conn.set_trace_callback(lambda sql: self.register('C:/images/b.png')
                                    if sql.startswith('SELECT path FROM files') else None)
            return conn
        with patch.object(library_service, 'open_reader', reader):
            value = self.service.read('resolve_library_selection', folder='C:/images')
        self.assertEqual((value['count'], value['paths']), (1, ['C:/images/a.png']))

    def test_unicode_quoted_terms_and_tags_preserve_literal_fts_search(self):
        path = 'C:/images/área-"alpha".png'
        self.register(path, 'C:/images/other.png')
        self.service.submit('edit_curation', path, add_tags=['bosque']).result(3)
        for terms in (['área'], ['"alpha"'], ['bosque'], ['bosque', 'á']):
            value = self.service.read('get_library_page', terms=terms)
            self.assertEqual(value['paths'], [path])


class PageQt(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.modules = []
        self.releases = []
        self.addCleanup(self.shutdown)
        self.bus = EventBus()

    shutdown = prior.LibrarianQt.shutdown
    view = prior.LibrarianQt.view
    ready = prior.LibrarianQt.ready
    block = prior.LibrarianQt.block

    def selected(self, count=203):
        paths = [f'C:/images/{i}.png' for i in range(count)]
        self.register(*paths)
        module = self.view()
        self.ready(module)
        module.load_folder_paths('C:/images')
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        return module, paths

    def test_next_and_previous_read_only_requested_sql_page(self):
        module, paths = self.selected()
        module.next_page()
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertEqual((module.current_page, module.total_paths), (1, paths[100:200]))
        module.next_page()
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertEqual(module.total_paths, paths[200:])
        self.assertFalse(module.btn_next.isEnabled())
        module.prev_page()
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertEqual(module.total_paths, paths[100:200])

    def test_pending_page_prevents_double_navigation_and_handoff(self):
        module, paths = self.selected()
        entered, release, calls, hook = self.block('get_library_page')
        with hook:
            module.next_page()
            self.assertTrue(entered.wait(1))
            module.next_page()
            module.prev_page()
            module.send_to_gallery()
            self.assertEqual(module._get_active_paths(), [])
            release.set()
            self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertEqual(len(calls), 1)
        self.assertEqual(module.total_paths, paths[100:200])

    def test_all_handoff_async_complete_on_worker_without_clipping(self):
        module, paths = self.selected()
        received = []
        module.request_open_optimizer.connect(received.append)
        entered, release, calls, hook = self.block('resolve_library_selection')
        with hook:
            before = time.monotonic()
            module.send_to_optimizer()
            self.assertLess(time.monotonic() - before, .1)
            self.assertTrue(entered.wait(1))
            module.send_to_optimizer()
            self.assertFalse(module.btn_execute.isEnabled())
            release.set()
            self.assertTrue(helpers.pump(lambda: bool(received)))
        self.assertEqual(received, [paths])
        self.assertEqual(len(calls), 1)

    def test_large_complete_handoff_has_visible_error_and_zero_delivery_then_page(self):
        module, paths = self.selected(1001)
        received = []
        module.request_open_gallery.connect(lambda rows, title: received.append((rows, title)))
        with patch('modules.librarian.module.QMessageBox.warning') as warning:
            module.send_to_gallery()
            self.assertTrue(helpers.pump(lambda: warning.called))
            self.assertIn('No images were sent', warning.call_args.args[2])
        self.assertEqual(received, [])
        module.combo_scope.setCurrentIndex(1)
        with patch.object(self.service, 'read', side_effect=AssertionError('GUI read')):
            module.send_to_gallery()
        self.assertEqual(received[0][0], paths[:100])
        self.assertIn('Page 1', received[0][1])

    def test_filter_change_drops_pending_complete_handoff(self):
        module, _ = self.selected()
        received = []
        module.request_open_cropper.connect(received.append)
        entered, release, _, hook = self.block('resolve_library_selection')
        with hook:
            module.send_to_cropper()
            self.assertTrue(entered.wait(1))
            module.load_folder_paths('C:/elsewhere')
            release.set()
            self.assertTrue(helpers.pump(lambda: module._browse_ready and not module.handoff_reader._active))
        self.assertEqual(received, [])
        self.assertIsNone(module._handoff)

    def test_unload_returns_immediately_and_discards_pending_handoff(self):
        module, _ = self.selected()
        received = []
        module.request_open_pickler.connect(received.append)
        entered, release, _, hook = self.block('resolve_library_selection')
        with hook:
            module.send_to_pickler()
            self.assertTrue(entered.wait(1))
            before = time.monotonic()
            module.on_unload()
            self.assertLess(time.monotonic() - before, .1)
            self.assertTrue(module.handoff_reader._cancel.is_set())
            release.set()
            module.handoff_reader._thread.join(3)
            QApplication.processEvents()
        self.assertEqual(received, [])

    def test_handoff_read_error_retains_page_for_retry(self):
        module, paths = self.selected()
        received = []
        module.request_open_optimizer.connect(received.append)
        with patch.object(self.service, 'read', side_effect=LibraryError('read', 'busy_fixture')), \
             patch('modules.librarian.module.QMessageBox.warning') as warning:
            module.send_to_optimizer()
            self.assertTrue(helpers.pump(lambda: warning.called))
            self.assertIn('busy_fixture', warning.call_args.args[2])
        self.assertEqual(module.total_paths, paths[:100])
        self.assertTrue(module._browse_ready)
        module.send_to_optimizer()
        self.assertTrue(helpers.pump(lambda: bool(received)))
        self.assertEqual(received, [paths])

    def test_shrink_last_page_keeps_valid_navigation(self):
        module, paths = self.selected()
        module._load_page(2)
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.service.submit('remove_files', paths[103:]).result(3)
        module._load_page(2)
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertEqual((module.current_page, module.total_paths), (1, paths[100:103]))
        self.assertTrue(module.btn_prev.isEnabled())
        self.assertFalse(module.btn_next.isEnabled())

    def test_headless_external_paths_keep_all_and_page_navigation(self):
        module = self.view()
        self.ready(module)
        paths = [f'C:/external/{i}.png' for i in range(203)]
        module.run_headless(paths)
        received = []
        module.request_open_gallery.connect(lambda rows, title: received.append(rows))
        with patch.object(self.service, 'read', side_effect=AssertionError('external DB read')):
            module.next_page()
            self.assertEqual(module.total_paths, paths[100:200])
            module.send_to_gallery()
        self.assertEqual(received, [paths])

    def test_all_four_destinations_receive_exact_confirmed_page(self):
        module, paths = self.selected()
        module.combo_scope.setCurrentIndex(1)
        for destination in ('gallery', 'optimizer', 'cropper', 'pickler'):
            received = []
            signal = getattr(module, 'request_open_' + destination)
            callback = lambda rows, *rest: received.append(rows)
            signal.connect(callback)
            with patch.object(self.service, 'read', side_effect=AssertionError('page DB read')):
                getattr(module, 'send_to_' + destination)()
            self.assertEqual(received, [paths[:100]])
            signal.disconnect(callback)

    def test_scope_and_status_texts_exist_in_both_languages(self):
        root = Path(__file__).resolve().parents[1]
        for language in ('en', 'es'):
            strings = json.loads((root / 'locales' / f'{language}.json').read_text(encoding='utf-8'))
            for key in ('lib.scope.all', 'lib.scope.page', 'lib.scope_tip', 'lib.execute_tip',
                        'lib.handoff.too_large', 'lib.handoff.title', 'lib.handoff.page', 'lib.db.page'):
                self.assertTrue(strings[key].format(context='fixture', page=2, count=203, loaded=100))


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
