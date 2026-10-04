"""External Gallery filters: private Native SQL, Qt delivery, complete scopes."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from core.library_repository import LibraryRepository
from core.library_service import LibraryService
from modules.gallery.logic.query_engine import QueryEngine
from modules.gallery.logic.state import GalleryState
from modules.gallery.ui.grid import GalleryGrid
from gallery_data_selftest import pump


class External(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LibraryService(Path(self.temp.name) / 'private.db')
        self.addCleanup(self.service.close)
        self.a = 'C:/external/uno_%_😀.png'
        self.b = 'C:/external/dos.png'
        self.c = 'C:/external/absent.png'
        self.service.submit('register_files_minimal', [dict(path=path, filename=Path(path).name,
            size=1, created=1, hash_original='a'*64) for path in (self.a, self.b)]).result(3)
        self.service.submit('edit_curation', self.a, add_tags=['portrait', 'curated'], rating=5).result(3)
        self.service.submit('edit_curation', self.b, add_tags=['portrait'], rating=2).result(3)
        self.service.submit('update_file_metadata', self.a,
                            dict(model='model-one', tool='A1111', positive='original prompt')).result(3)

    def page(self, paths=None, **filters):
        return self.service.read('get_external_files_page', paths if paths is not None else [self.b, self.c, self.a], **filters)

    def test_combined_filters_apply_to_entire_set_before_pagination(self):
        total, rows = self.page(tags=['portrait', 'curated'], search_terms=['model-one', 'prompt'], min_rating=4)
        self.assertEqual((total, rows), (1, [(self.a, 5)]))
        self.assertEqual(self.page(tags=['portrait'], limit=1), (2, [(self.b, 2)]))
        self.assertEqual(self.page(tags=['portrait'], limit=1, offset=1), (2, [(self.a, 5)]))
        self.assertEqual(self.page(tags=['portrait'], limit=1, offset=2), (2, []))

    def test_order_duplicates_and_original_spelling_are_preserved(self):
        alternate = self.a.replace('/', '\\')
        self.assertEqual(self.page([alternate, self.b, alternate], min_rating=1),
                         (3, [(alternate, 5), (self.b, 2), (alternate, 5)]))

    def test_unregistered_paths_filter_by_filename_and_zero_rating_only(self):
        self.assertEqual(self.page([self.c], search_terms=['absent']), (1, [(self.c, 0)]))
        self.assertEqual(self.page([self.c], tags=['portrait']), (0, []))
        self.assertEqual(self.page([self.c], min_rating=1), (0, []))
        self.assertEqual(self.page([self.c], search_terms=['prompt']), (0, []))
        self.assertIsNone(self.service.read('get_file_id', self.c))

    def test_like_wildcards_unicode_quotes_and_injection_are_parameters(self):
        self.assertEqual(self.page(search_terms=['uno_%_😀']), (1, [(self.a, 5)]))
        self.assertEqual(self.page(search_terms=['%']), (3, [(self.b, 2), (self.c, 0), (self.a, 5)]))
        self.assertEqual(self.page(search_terms=["' OR 1=1 --"]), (0, []))
        self.assertEqual(self.page(tags=["portrait' OR 1=1 --"]), (0, []))
        self.assertEqual(self.service.read('count_files'), 2)

    def test_offline_history_does_not_supply_old_curation(self):
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('UPDATE files SET is_offline=1 WHERE path=?', (self.a,))
            conn.commit()
        self.assertEqual(self.page([self.a], tags=['curated']), (0, []))
        self.assertEqual(self.page([self.a], search_terms=['uno']), (1, [(self.a, 0)]))

    def test_empty_set_and_bad_inputs_return_no_partial_prefix(self):
        self.assertEqual(self.page([]), (0, []))
        for filters in (dict(min_rating=True), dict(min_rating=6), dict(tags='tag'),
                        dict(search_terms=['']), dict(limit=0)):
            with self.subTest(filters=filters), self.assertRaises(Exception):
                self.page(**filters)
        for paths in ([self.a] * 10001, [self.a, '\x00bad'], [self.a, 'x'*4097]):
            with self.assertRaises(Exception):
                self.page(paths)

    def test_pagination_crosses_multiple_sql_blocks_with_full_count(self):
        paths = [f'C:/external/extra-{i}.png' for i in range(1000)]
        paths[257] = self.a
        paths[900] = self.b
        self.assertEqual(self.page(paths, tags=['portrait'], limit=1, offset=1), (2, [(self.b, 2)]))
        self.assertEqual(self.page(paths, search_terms=['extra-'], limit=3, offset=995),
                         (998, [(paths[i], 0) for i in (997, 998, 999)]))

    def test_reader_is_query_only_and_no_ddl_dml_or_revision_change(self):
        before = self.service.revision_snapshot()
        seen = []
        real = LibraryRepository.get_external_files_page
        def checked(repo, *args, **kwargs):
            self.assertEqual(repo.conn.execute('PRAGMA query_only').fetchone()[0], 1)
            repo.conn.set_trace_callback(seen.append)
            return real(repo, *args, **kwargs)
        with patch.object(LibraryRepository, 'get_external_files_page', checked):
            self.page(tags=['portrait'])
        self.assertEqual(self.service.revision_snapshot(), before)
        self.assertFalse(any(sql.lstrip().split()[0].upper() in
                             {'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP', 'ALTER'} for sql in seen))

    def test_concurrent_native_edit_does_not_mix_blocks_of_one_snapshot(self):
        paths = [self.a] * 600
        entered, release = threading.Event(), threading.Event()
        result, failures = [], []
        real = LibraryRepository._normalize_path
        calls = 0
        def normalize(repo, path):
            nonlocal calls
            calls += 1
            if calls == 257:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('fixture release timeout')
            return real(repo, path)
        def run():
            try:
                result.append(self.page(paths, min_rating=4, limit=10))
            except Exception as error:
                failures.append(error)
        with patch.object(LibraryRepository, '_normalize_path', normalize):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertTrue(self.service.submit('edit_curation', self.a, rating=1).result(3))
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures, failures)
        self.assertEqual(result, [(600, [(self.a, 5)] * 10)])
        self.assertEqual(self.page(paths, min_rating=4), (0, []))

    def grid(self):
        state = GalleryState()
        engine = QueryEngine(service=self.service)
        grid = GalleryGrid(state, engine)
        def close():
            grid.shutdown()
            self.assertTrue(pump(lambda: grid.reader.shutdown_ready() and grid.loader.shutdown_ready()))
            grid.deleteLater()
            QApplication.processEvents()
        self.addCleanup(close)
        self.assertTrue(pump(lambda: not grid._loading))
        return state, engine, grid

    def test_gui_filter_delivery_is_off_thread_and_counts_filtered_set(self):
        state, engine, grid = self.grid()
        main = threading.get_ident()
        threads = []
        real = LibraryRepository.get_external_files_page
        def read(repo, *args, **kwargs):
            threads.append(threading.get_ident())
            return real(repo, *args, **kwargs)
        with patch.object(LibraryRepository, 'get_external_files_page', read), \
                patch.object(grid.loader, 'get_thumbnail_image', return_value=None):
            state.set_mode(state.VIEW_CUSTOM, custom_paths=[self.b, self.c, self.a])
            self.assertTrue(pump(lambda: not grid._loading))
            state.set_rating_filter(4)
            self.assertTrue(pump(lambda: not grid._loading))
        self.assertEqual(state._total_items, 1)
        self.assertEqual(grid.grid_layout.count(), 1)
        self.assertEqual(grid.grid_layout.itemAt(0).widget().path, self.a)
        self.assertTrue(threads)
        self.assertNotIn(main, threads)
        self.assertEqual(engine.fetch_images(state), (1, [(self.a, 5)]))

    def test_scope_limit_is_visible_and_never_displays_filtered_prefix(self):
        state, _, grid = self.grid()
        state.tags = ['portrait']
        state.set_mode(state.VIEW_CUSTOM, custom_paths=[self.a] * 10001)
        self.assertTrue(pump(lambda: not grid._loading))
        from core.locale_manager import LocaleManager
        self.assertEqual(grid.lbl_page.text(), LocaleManager().tr('gallery.external_scope_limit'))
        self.assertIn('read_payload_too_large', grid.lbl_page.toolTip())
        self.assertEqual(grid.grid_layout.count(), 0)
        self.assertFalse(grid.btn_next.isEnabled())


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main()
