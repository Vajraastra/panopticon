"""Snapshots/visor con SQLite temporal y Qt offscreen; nunca biblioteca real."""
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from core.library_connections import open_reader
from core.library_repository import LibraryRepository
from core.library_service import LibraryService
from core.metadata.bundle import MetadataBundle
from modules.gallery.logic.query_engine import QueryEngine
from modules.gallery.logic.state import GalleryState
from modules.gallery.ui.grid import GalleryGrid
from modules.gallery.ui.viewer_window import AdvancedViewer


def pump(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(.001)
    return False


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LibraryService(Path(self.temp.name) / 'fixture.db')
        self.addCleanup(self.service.close)

    def register(self, *paths):
        # Seed large fixtures through bounded requests, like actual ingestion.
        for start in range(0, len(paths), 100):
            self.assertTrue(self.service.submit('register_files_minimal', [
                dict(path=p, filename=p.rsplit('/', 1)[-1], size=1, created=1)
                for p in paths[start:start+100]]).result(3))


class SnapshotSQL(Fixture):
    def test_order_duplicates_normalization_missing_and_authoritative_empty(self):
        self.register('C:/images/a.png', 'C:/images/b.png')
        self.service.submit('update_file_rating', 'C:/images/b.png', 4).result(3)
        self.service.submit('add_tag_to_file', 'C:/images/b.png', 'Zulu').result(3)
        self.service.submit('add_tag_to_file', 'C:/images/b.png', 'Alpha').result(3)
        rows = self.service.read('get_curation_snapshot',
                                 ['C:\\images\\b.png', 'C:/absent.png', 'C:/images/a.png', 'C:/images/b.png'])
        self.assertEqual([r['path'] for r in rows],
                         ['C:/images/b.png', 'C:/absent.png', 'C:/images/a.png', 'C:/images/b.png'])
        self.assertEqual(rows[0]['tags'], ('alpha', 'zulu'))
        self.assertEqual(rows[0]['rating'], 4)
        self.assertEqual(rows[0], rows[3])
        self.assertIsNot(rows[0], rows[3])
        self.assertIsNone(rows[1]['id'])
        self.assertIsNone(rows[1]['tags'])
        self.assertIsNotNone(rows[2]['id'])
        self.assertEqual((rows[2]['rating'], rows[2]['tags']), (0, ()))
        self.assertEqual(self.service.read('count_files'), 2)

    def test_256_paths_take_two_indexed_selects_not_per_file_queries(self):
        paths = [f'C:/images/{i}.png' for i in range(256)]
        self.register(*paths)
        with closing(open_reader(self.service.db_path)) as conn:
            statements = []
            conn.set_trace_callback(statements.append)
            rows = LibraryRepository(conn).get_curation_snapshot(paths)
            self.assertEqual(len(rows), 256)
            self.assertEqual(sum(s.startswith('SELECT') for s in statements), 2)
            plan = conn.execute('EXPLAIN QUERY PLAN SELECT id,path,rating FROM files WHERE path IN (?,?)',
                                paths[:2]).fetchall()
            self.assertTrue(any('INDEX' in row[3] for row in plan), plan)

    def test_limits_and_invalid_paths_fail_before_any_sql(self):
        with closing(open_reader(self.service.db_path)) as conn:
            statements = []
            conn.set_trace_callback(statements.append)
            repo = LibraryRepository(conn)
            self.assertEqual(repo.get_curation_snapshot([]), [])
            for paths in (['x'] * 257, 'C:/x.png', [None], ['']):
                with self.assertRaises(ValueError):
                    repo.get_curation_snapshot(paths)
            self.assertEqual(statements, [])

    def test_file_and_tags_share_snapshot_during_writer_commit(self):
        path = 'C:/images/a.png'
        self.register(path)
        self.service.submit('add_tag_to_file', path, 'old').result(3)
        from core import library_service
        real_open = library_service.open_reader
        writes = []

        def reader(db_path):
            conn = real_open(db_path)
            def trace(sql):
                if 'SELECT ft.file_id' in sql:
                    self.service.submit('update_file_rating', path, 5).result(3)
                    self.service.submit('add_tag_to_file', path, 'new').result(3)
                    writes.append(True)
            conn.set_trace_callback(trace)
            return conn

        with patch.object(library_service, 'open_reader', reader):
            row = self.service.read('get_curation_snapshot', (path,))[0]
        self.assertEqual(writes, [True])
        self.assertEqual((row['rating'], row['tags']), (0, ('old',)))
        current = self.service.read('get_curation_snapshot', (path,))[0]
        self.assertEqual((current['rating'], current['tags']), (5, ('new', 'old')))

    def test_closed_service_is_error_not_missing_rows(self):
        self.service.close()
        with self.assertRaises(Exception):
            self.service.read('get_curation_snapshot', ('C:/missing.png',))


class CurationQt(Fixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.views = []
        self.readers = []
        self.addCleanup(self.stop_views)
        self.extract = patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract',
                             return_value=MetadataBundle(tags=['file-tag'], rating=5))
        self.extract.start()
        self.addCleanup(self.extract.stop)

    def stop_views(self):
        for view in self.views:
            view.shutdown()
            view.close()
            view.deleteLater()
        for reader in self.readers:
            reader.close()
            if reader._thread:
                reader._thread.join(3)
                self.assertFalse(reader._thread.is_alive())
        self.app.processEvents()

    def viewer(self, paths):
        view = AdvancedViewer(paths, service=self.service)
        self.views.append(view)
        self.readers.extend((view.curation_reader, view.tags_reader))
        return view

    def grid(self, paths):
        state = GalleryState()
        state.set_mode(state.VIEW_CUSTOM, custom_paths=paths)
        view = GalleryGrid(state, QueryEngine(self.service))
        self.views.append(view)
        self.readers.append(view.reader)
        return state, view

    def test_viewer_does_not_call_service_read_on_gui_or_resurrect_empty_curation(self):
        path = 'C:/images/a.png'
        self.register(path)
        calls = []
        main = threading.get_ident()
        original = self.service.read
        def read(operation, *args, **kwargs):
            calls.append((operation, threading.get_ident()))
            return original(operation, *args, **kwargs)
        with patch.object(self.service, 'read', read):
            view = self.viewer([path])
            self.assertTrue(pump(lambda: view._curation_ready))
            self.assertTrue(pump(lambda: not view.tags_reader._active))
        self.assertEqual({op for op, _ in calls}, {'get_curation_snapshot', 'get_tags_page'})
        self.assertTrue(all(thread != main for _, thread in calls))
        self.assertEqual(view._current_rating, 0)
        self.assertEqual(view.tags_layout.count(), 0)

    def test_missing_file_uses_embedded_metadata_without_registering(self):
        view = self.viewer(['C:/external.png'])
        self.assertTrue(pump(lambda: view._curation_ready))
        self.assertEqual(view._current_rating, 5)
        self.assertEqual(view.tags_layout.count(), 1)
        self.assertEqual(self.service.read('count_files'), 0)

    def test_blocked_read_keeps_qt_responsive_and_only_latest_image_wins(self):
        paths = ['C:/images/a.png', 'C:/images/b.png']
        self.register(*paths)
        self.service.submit('update_file_rating', paths[0], 1).result(3)
        self.service.submit('update_file_rating', paths[1], 4).result(3)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.service.read
        calls = []
        def read(operation, *args, **kwargs):
            if operation == 'get_curation_snapshot':
                calls.append(args[0])
                if len(calls) == 1:
                    entered.set()
                    release.wait(3)
            return original(operation, *args, **kwargs)
        with patch.object(self.service, 'read', read):
            start = time.monotonic()
            view = self.viewer(paths)
            self.assertLess(time.monotonic() - start, .5)
            self.assertTrue(entered.wait(3))
            view.next_image()
            self.assertFalse(view.input_tag.isEnabled())
            ticks = []
            QTimer.singleShot(0, lambda: ticks.append(True))
            self.assertTrue(pump(lambda: ticks))
            release.set()
            self.assertTrue(pump(lambda: view._curation_ready))
        self.assertEqual(view.current_idx, 1)
        self.assertEqual(view._current_rating, 4)
        self.assertEqual(calls, [(paths[0],), (paths[1],)])

    def test_read_error_disables_edits_without_file_metadata_success(self):
        self.service.close()
        view = self.viewer(['C:/external.png'])
        self.assertTrue(pump(lambda: not view.curation_reader._active))
        self.assertFalse(view._curation_ready)
        self.assertFalse(view.lbl_rating_stars.isEnabled())
        self.assertFalse(view.input_tag.isEnabled())
        self.assertEqual(view.tags_layout.count(), 0)

    def test_reject_cancels_read_and_slideshow_without_waiting(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.service.read
        def read(operation, *args, **kwargs):
            if operation == 'get_curation_snapshot':
                entered.set()
                release.wait(3)
            return original(operation, *args, **kwargs)
        with patch.object(self.service, 'read', read):
            view = self.viewer(['C:/external.png'])
            self.assertTrue(entered.wait(3))
            view.timer.start(1000)
            start = time.monotonic()
            view.reject()
            self.assertLess(time.monotonic() - start, .1)
            self.assertFalse(view.timer.isActive())
            self.assertTrue(view.curation_reader._cancel.is_set())
            release.set()
            view.curation_reader._thread.join(3)
            self.app.processEvents()
            self.assertFalse(view._curation_ready)

    def test_empty_viewer_navigation_and_fullscreen_are_safe(self):
        view = self.viewer([])
        view.next_image()
        view.prev_image()
        view.toggle_fullscreen()
        view.cycle_current_rating()
        view.add_tag()
        self.assertIsNone(view.fs_window)

    def test_custom_large_set_only_fetches_visible_page_and_confirmed_rating(self):
        paths = [f'C:/images/{i}.png' for i in range(1000)]
        self.register(paths[51])
        self.service.submit('update_file_rating', paths[51], 4).result(3)
        original = self.service.read
        calls = []
        def read(operation, *args, **kwargs):
            if operation == 'get_curation_snapshot':
                calls.append(args[0])
            return original(operation, *args, **kwargs)
        with patch.object(self.service, 'read', read):
            state, grid = self.grid(paths)
            self.assertTrue(pump(lambda: not grid._loading))
            state.set_page(1)
            self.assertTrue(pump(lambda: not grid._loading))
        self.assertEqual(calls, [tuple(paths[:50]), tuple(paths[50:100])])
        self.assertEqual(state._total_items, 1000)
        self.assertEqual(grid.grid_layout.count(), 50)
        self.assertEqual(grid.grid_layout.itemAt(1).widget().rating, 4)
        sync_total, sync_rows = QueryEngine(self.service).fetch_images(state)
        self.assertEqual(sync_total, 1000)
        self.assertEqual(sync_rows[1], (paths[51], 4))

    def test_custom_rating_success_refreshes_snapshot_instead_of_zero_placeholder(self):
        path = 'C:/images/a.png'
        self.register(path)
        state, grid = self.grid([path])
        self.assertTrue(pump(lambda: not grid._loading))
        delivered = []
        grid.writer.result.connect(lambda *args: delivered.append(True))
        grid.update_rating(path, 3)
        self.assertTrue(pump(lambda: delivered and not grid._loading))
        self.assertEqual(self.service.read('get_file_rating', path), 3)
        self.assertEqual(grid.grid_layout.itemAt(0).widget().rating, 3)
        self.assertEqual(QueryEngine(self.service).fetch_images(state)[1], [(path, 3)])

    def test_custom_read_failure_does_not_display_unconfirmed_zero_stars(self):
        self.service.close()
        state, grid = self.grid(['C:/external.png'])
        self.assertTrue(pump(lambda: not grid._loading))
        self.assertEqual(grid.grid_layout.count(), 0)
        self.assertFalse(grid.btn_next.isEnabled())

    def test_custom_replacement_discards_delayed_previous_page(self):
        paths = ['C:/images/a.png', 'C:/images/b.png']
        self.register(*paths)
        self.service.submit('update_file_rating', paths[1], 4).result(3)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.service.read
        calls = []
        def read(operation, *args, **kwargs):
            calls.append(args[0])
            if len(calls) == 1:
                entered.set()
                release.wait(3)
            return original(operation, *args, **kwargs)
        with patch.object(self.service, 'read', read):
            state, grid = self.grid(paths[:1])
            self.assertTrue(entered.wait(3))
            state.set_mode(state.VIEW_CUSTOM, custom_paths=[paths[1], paths[1]])
            release.set()
            self.assertTrue(pump(lambda: not grid._loading))
        self.assertEqual(state._total_items, 2)
        self.assertEqual(grid.grid_layout.count(), 2)
        self.assertTrue(all(grid.grid_layout.itemAt(i).widget().rating == 4 for i in range(2)))
        self.assertTrue(all(grid.grid_layout.itemAt(i).widget().path == paths[1] for i in range(2)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
