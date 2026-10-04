"""Galería/entrega Qt con fixtures; no abre biblioteca ni catálogos Cherry."""
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget
from core.library_async import LibraryReader, LibraryWriter
from core.library_repository import LibraryRepository
from core.library_service import LibraryService, LibraryError
from modules.gallery.logic.query_engine import QueryEngine
from modules.gallery.logic.state import GalleryState
from modules.gallery.ui.grid import GalleryGrid


def pump(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(.001)
    return False


class QtDelivery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LibraryService(Path(self.temp.name) / 'fixture.db')
        self.addCleanup(self.service.close)
        self.readers = []
        self.addCleanup(self.stop_readers)

    def stop_readers(self):
        for reader in self.readers:
            reader.close()
        for reader in self.readers:
            if reader._thread:
                reader._thread.join(3)
                self.assertFalse(reader._thread.is_alive())
        self.app.processEvents()

    def reader(self, service=None, parent=None):
        reader = LibraryReader(service or self.service, parent)
        self.readers.append(reader)
        return reader

    def test_reader_is_off_gui_and_delivery_is_on_gui(self):
        reader = self.reader()
        main = threading.get_ident()
        seen, delivered = [], []
        original = self.service.read

        def read(*args, **kwargs):
            seen.append(threading.get_ident())
            return original(*args, **kwargs)

        reader.result.connect(lambda *args: delivered.append((threading.get_ident(), args)))
        with patch.object(self.service, 'read', read):
            reader.request('count_files')
            self.assertTrue(pump(lambda: delivered))
        self.assertNotEqual(seen, [main])
        self.assertEqual(delivered[0][0], main)
        self.assertEqual(delivered[0][1][1:], (0, None))

    def test_latest_only_coalesces_and_freezes_mutable_payload(self):
        entered, release = threading.Event(), threading.Event()
        calls, output = [], []

        class Fake:
            def read(self, operation, values, **kwargs):
                calls.append(list(values))
                if len(calls) == 1:
                    entered.set()
                    release.wait(3)  # Simular I/O que no respeta cancelación.
                return values

        reader = self.reader(Fake())
        reader.result.connect(lambda *args: output.append(args))
        reader.request('fake', [0])
        self.assertTrue(entered.wait(3))
        self.addCleanup(release.set)
        for number in range(1, 100):
            payload = [number]
            reader.request('fake', payload)
            payload[0] = -1
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        self.assertTrue(pump(lambda: ticks))
        self.assertEqual(calls, [[0]])
        release.set()
        self.assertTrue(pump(lambda: output))
        self.assertEqual(calls, [[0], [99]])
        self.assertEqual(output, [(100, [99], None)])

    def test_invalidate_suppresses_completed_but_queued_result(self):
        reader = self.reader()
        output = []
        reader.result.connect(lambda *args: output.append(args))
        reader.request('count_files')
        reader._thread.join(3)
        reader.invalidate()
        self.app.processEvents()
        self.assertEqual(output, [])
        reader.request('count_files')
        self.assertTrue(pump(lambda: output))

    def test_close_drops_pending_and_never_waits_for_blocked_read(self):
        entered, release = threading.Event(), threading.Event()
        calls, output = [], []

        class Fake:
            def read(self, *args, **kwargs):
                calls.append(True)
                entered.set()
                release.wait(3)
                return 0

        reader = self.reader(Fake())
        reader.result.connect(lambda *args: output.append(args))
        reader.request('first')
        self.assertTrue(entered.wait(3))
        reader.request('pending')
        started = time.monotonic()
        reader.close()
        self.assertLess(time.monotonic() - started, .1)
        release.set()
        reader._thread.join(3)
        self.app.processEvents()
        self.assertEqual(calls, [True])
        self.assertEqual(output, [])
        with self.assertRaises(RuntimeError):
            reader.request('after_close')

    def test_destroyed_owner_cancels_and_late_result_is_safe(self):
        entered, release = threading.Event(), threading.Event()
        events = []

        class Fake:
            def read(self, *args, cancel_event=None, **kwargs):
                events.append(cancel_event)
                entered.set()
                release.wait(3)
                return 0

        parent = QWidget()
        reader = self.reader(Fake(), parent)
        reader.request('slow')
        self.assertTrue(entered.wait(3))
        parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertTrue(events[0].is_set())
        release.set()
        reader._thread.join(3)
        self.assertFalse(reader._thread.is_alive())

    def test_cancellation_interrupts_sql_and_closes_reader_connection(self):
        entered = threading.Event()
        cancel = threading.Event()
        errors = []

        def heavy(repo):
            entered.set()
            return repo.conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                                     'SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n').fetchone()[0]

        def run():
            try:
                self.service.read('count_files', cancel_event=cancel)
            except LibraryError as exc:
                errors.append(exc.reason)

        with patch.object(LibraryRepository, 'count_files', heavy):
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(entered.wait(3))
            cancel.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['cancelled'])
        self.assertEqual(self.service.read('count_files'), 0)

    def test_closed_service_delivers_error_not_empty_success(self):
        self.service.close()
        reader = self.reader()
        results = []
        reader.result.connect(lambda *args: results.append(args))
        reader.request('count_files')
        self.assertTrue(pump(lambda: results))
        self.assertIsNone(results[0][1])
        self.assertEqual(results[0][2].reason, 'service_closed')

    def test_write_bridge_marshals_immediate_and_worker_futures(self):
        writer = LibraryWriter(self.service)
        output = []
        writer.result.connect(lambda *args: output.append((threading.get_ident(), args)))
        main = threading.get_ident()
        writer.submit('add_watched_folder', 'C:/fixture')
        self.assertTrue(pump(lambda: output))
        self.assertEqual(output[0][0], main)
        self.assertEqual(output[0][1][1:], (True, None))
        self.service.close()
        writer.submit('add_watched_folder', 'C:/other')
        self.assertTrue(pump(lambda: len(output) == 2))
        self.assertEqual(output[1][1][2].reason, 'service_closed')

    def test_write_bridge_close_keeps_accepted_write_but_drops_delivery(self):
        future = Future()
        class Fake:
            def submit(self, *args, **kwargs):
                return future
        writer = LibraryWriter(Fake())
        output = []
        writer.result.connect(lambda *args: output.append(args))
        writer.submit('fake')
        writer.close()
        future.set_result(True)
        self.app.processEvents()
        self.assertFalse(future.cancelled())
        self.assertEqual(output, [])

    def test_grid_shrinking_total_clamps_page_and_exact_multiple_has_no_extra_page(self):
        state = GalleryState()
        engine = QueryEngine(self.service)
        grid = GalleryGrid(state, engine)
        self.readers.append(grid.reader)
        self.addCleanup(grid.shutdown)
        self.addCleanup(grid.deleteLater)
        self.assertTrue(pump(lambda: not grid._loading))
        state.set_total_items(100)
        state._page = 1
        grid.update_pagination_ui()
        self.assertFalse(grid.btn_next.isEnabled())
        self.assertTrue(grid.btn_prev.isEnabled())
        state._page = 2
        grid.on_query_result(0, (50, []), None)
        self.assertEqual(state._page, 0)
        self.assertTrue(pump(lambda: not grid._loading))

    def test_grid_switch_to_custom_discards_old_album_result(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service.read

        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)

        state = GalleryState()
        with patch.object(self.service, 'read', slow):
            grid = GalleryGrid(state, QueryEngine(self.service))
            self.readers.append(grid.reader)
            self.addCleanup(grid.shutdown)
            self.addCleanup(grid.deleteLater)
            self.assertTrue(entered.wait(3))
            state.set_mode(state.VIEW_CUSTOM, custom_paths=[])
            self.assertFalse(grid._loading)
            release.set()
            grid.reader._thread.join(3)
            self.app.processEvents()
            self.assertEqual(state.mode, state.VIEW_CUSTOM)
            self.assertEqual(state._total_items, 0)
            self.assertFalse(grid._loading)

    def test_grid_write_failure_is_visible_and_reloads_confirmed_data(self):
        state = GalleryState()
        grid = GalleryGrid(state, QueryEngine(self.service))
        self.readers.append(grid.reader)
        self.addCleanup(grid.shutdown)
        self.addCleanup(grid.deleteLater)
        self.assertTrue(pump(lambda: not grid._loading))
        with patch.object(QMessageBox, 'warning') as warning:
            grid.update_rating('C:/missing.png', 5)
            self.assertTrue(pump(lambda: warning.called))
            warning.assert_called_once()
        self.assertTrue(pump(lambda: not grid._loading))

    def test_blocked_writer_does_not_block_grid_rating_or_qt(self):
        path = 'C:/fixture.png'
        self.service.submit('register_files_minimal', [dict(path=path, filename='fixture.png',
                            size=1, created=1)]).result(3)
        entered, release = threading.Event(), threading.Event()
        original = LibraryRepository.add_watched_folder

        def blocked(repo, folder):
            entered.set()
            release.wait(3)
            return original(repo, folder)

        state = GalleryState()
        state.set_mode(state.VIEW_IMAGES)
        grid = GalleryGrid(state, QueryEngine(self.service))
        self.readers.append(grid.reader)
        self.addCleanup(grid.shutdown)
        self.addCleanup(grid.deleteLater)
        self.assertTrue(pump(lambda: not grid._loading))
        with patch.object(LibraryRepository, 'add_watched_folder', blocked):
            barrier = self.service.submit('add_watched_folder', 'C:/fixture')
            self.assertTrue(entered.wait(3))
            start = time.monotonic()
            grid.update_rating(path, 4)
            self.assertLess(time.monotonic() - start, .1)
            ticks = []
            QTimer.singleShot(0, lambda: ticks.append(True))
            self.assertTrue(pump(lambda: ticks))
            self.assertEqual(self.service.read('get_file_rating', path), 0)
            release.set()
            self.assertTrue(barrier.result(3))
        self.assertTrue(pump(lambda: self.service.read('get_file_rating', path) == 4))

    def test_grid_rejects_rating_for_replaced_copy_in_all_image_modes(self):
        for mode in ('images', 'custom', 'filtered'):
            with self.subTest(mode=mode):
                path = f'C:/fixture/{mode}.png'
                self.service.submit('register_files_minimal', [dict(path=path,
                    filename=mode+'.png', size=1, created=1, hash_original='a'*64)]).result(3)
                state = GalleryState()
                if mode == 'images':
                    state.set_mode(state.VIEW_IMAGES)
                else:
                    state.set_mode(state.VIEW_CUSTOM, custom_paths=[path])
                    if mode == 'filtered':
                        state.search_terms = [mode]
                grid = GalleryGrid(state, QueryEngine(self.service))
                self.readers.append(grid.reader)
                self.addCleanup(grid.shutdown)
                self.addCleanup(grid.deleteLater)
                self.assertTrue(pump(lambda: not grid._loading))
                thumb = next(grid.grid_layout.itemAt(i).widget()
                    for i in range(grid.grid_layout.count())
                    if grid.grid_layout.itemAt(i).widget().path == path)
                old = self.service.read('get_curation_snapshot', [path], include_revision=True)[0]
                self.service.submit('archive_copy', old['id'], expected_fingerprint=old['fingerprint']).result(3)
                archived = self.service.read('get_copy_version_state', old['id'])
                self.service.submit('make_copy_online', old['id'], expected_fingerprint=archived['fingerprint'],
                    digest='b'*64, observation=dict(filename=mode+'.png', size=1, created=1)).result(3)
                with patch.object(QMessageBox, 'warning') as warning:
                    thumb.rating_changed.emit(path, 5)
                    self.assertTrue(pump(lambda: warning.called and not grid._loading))
                current = self.service.read('get_curation_snapshot', [path])[0]
                self.assertNotEqual(current['id'], old['id'])
                self.assertEqual(current['rating'], 0)
                self.assertEqual(self.service.read('get_copy_version_state', old['id'])['state']['file']['rating'], 0)
                grid.shutdown()

    def test_grid_stale_rating_does_not_overwrite_newer_curation(self):
        path = 'C:/fixture/concurrent.png'
        self.service.submit('register_files_minimal', [dict(path=path,
            filename='concurrent.png', size=1, created=1)]).result(3)
        state = GalleryState()
        state.set_mode(state.VIEW_CUSTOM, custom_paths=[path])
        grid = GalleryGrid(state, QueryEngine(self.service))
        self.readers.append(grid.reader)
        self.addCleanup(grid.shutdown)
        self.addCleanup(grid.deleteLater)
        self.assertTrue(pump(lambda: not grid._loading))
        self.service.submit('edit_curation', path, rating=4).result(3)
        with patch.object(QMessageBox, 'warning') as warning:
            grid.grid_layout.itemAt(0).widget().rating_changed.emit(path, 1)
            self.assertTrue(pump(lambda: warning.called and not grid._loading))
        self.assertEqual(self.service.read('get_file_rating', path), 4)
        grid.update_rating(path, 5)
        self.assertTrue(pump(lambda: self.service.read('get_file_rating', path) == 5 and not grid._loading))

    def test_sidebar_quoted_text_reaches_metadata_query(self):
        from modules.gallery.ui.sidebar import GallerySidebar
        state = GalleryState()
        sidebar = GallerySidebar(state, QueryEngine(self.service))
        self.readers.append(sidebar.reader)
        self.addCleanup(sidebar.shutdown)
        self.addCleanup(sidebar.deleteLater)
        sidebar.txt_search.setText('tag, "girl paint", ""')
        sidebar.on_search_submit()
        self.assertEqual(state.tags, ['tag'])
        self.assertEqual(state.search_terms, ['girl paint'])


class QuerySemantics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'queries.db'
        self.service = LibraryService(self.path)
        self.addCleanup(self.service.close)
        self.folder = 'C:/space rating:4_100%'
        self.paths = [f'{self.folder}/{i}.png' for i in range(6)]
        self.service.submit('register_files_minimal', [dict(path=p, filename=f'{i}.png',
                            size=1, created=1) for i, p in enumerate(self.paths)]).result(3)
        metadata = [dict(positive='Girl ÁRBOL 日本語 a_b 100% quote" OR', model='M') ,
                    dict(model='girlfriend', tool='T'), dict(tool='girl paint'),
                    dict(negative='girl', positive='nothing'), dict(positive='Árbol árbol'),
                    dict(positive=None, tool=None, model=None)]
        for i, meta in enumerate(metadata):
            self.service.submit('update_file_metadata', self.paths[i], meta).result(3)
            self.service.submit('update_file_rating', self.paths[i], i).result(3)
        self.service.submit('add_tag_to_file', self.paths[4], 'girl').result(3)

    def baseline(self, terms):
        conditions, params = [], []
        for term in terms:
            conditions.append('(meta_positive LIKE ? OR meta_model LIKE ? OR meta_tool LIKE ? OR filename LIKE ?)')
            params.extend([f'%{term}%'] * 4)
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute('SELECT path,rating FROM files' + where + ' ORDER BY id DESC', params).fetchall()
        return len(rows), rows

    def test_fts_equals_old_four_column_like_across_patterns(self):
        for term in ('girl', 'GIRL', 'gir', 'ir', '', '%', '_', 'a_b', '100%', 'quote"',
                     ' OR', '日本語', 'ÁRBOL', 'árbol', 'Árbol', '不存在', 'g%rl', 'g_rl', 'foo\\bar'):
            with self.subTest(term=term):
                self.assertEqual(self.service.read('search_files_paginated', search_terms=[term]),
                                 self.baseline([term]))

    def test_multiple_terms_are_and_and_negative_metadata_tags_excluded(self):
        result = self.service.read('search_files_paginated', search_terms=['girl'])
        self.assertEqual(result[0], 3)
        self.assertNotIn(self.paths[3], [p for p, r in result[1]])
        self.assertNotIn(self.paths[4], [p for p, r in result[1]])
        for terms in (['girl', 'paint'], ['girl', 'ÁRBOL'], ['girl', 'nothing']):
            self.assertEqual(self.service.read('search_files_paginated', search_terms=terms), self.baseline(terms))

    def test_fts_plan_uses_trigram_like_constraint(self):
        with closing(sqlite3.connect(self.path)) as conn:
            plan = conn.execute('EXPLAIN QUERY PLAN SELECT rowid FROM files_fts WHERE meta_positive LIKE ?',
                                ('%girl%',)).fetchall()
        self.assertTrue(any('L1' in row[3] for row in plan), plan)

    def test_dense_candidates_fallback_without_truncating_results(self):
        paths = [f'C:/dense/girl-{i}.png' for i in range(600)]
        self.service.submit('register_files_minimal', [dict(path=p, filename=Path(p).name,
                            size=1, created=1) for p in paths]).result(3)
        total, rows = self.service.read('search_files_paginated', search_terms=['girl'], limit=1000)
        self.assertEqual(total, 603)
        self.assertEqual((total, rows), self.baseline(['girl']))

    def test_stale_fts_candidate_is_verified_against_current_file(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("UPDATE files_fts SET meta_positive='girl' WHERE rowid=6")
            conn.commit()
        self.assertEqual(self.service.read('search_files_paginated', search_terms=['girl']), self.baseline(['girl']))

    def test_minimum_rating_and_literal_folder_avoid_query_string_parse(self):
        state = GalleryState()
        state.set_mode(state.VIEW_IMAGES, folder=self.folder)
        state.set_rating_filter(3)
        engine = QueryEngine(self.service)
        total, rows = engine.fetch_images(state)
        self.assertEqual(total, 3)
        self.assertEqual([rating for path, rating in rows], [5, 4, 3])
        # La API de texto histórica conserva rating exacto para otros consumidores.
        self.assertEqual(self.service.read('search_files_paginated', query='rating:3')[0], 1)

    def test_tags_terms_folder_and_rating_apply_to_count_and_page(self):
        result = self.service.read('search_files_paginated', tags=['girl'],
                                   search_terms=['Árbol'], folder=self.folder, min_rating=4, limit=1)
        self.assertEqual(result, (1, [(self.paths[4], 4)]))
        self.assertEqual(self.service.read('search_files_paginated', tags=['girl'],
                                         search_terms=['Árbol'], offset=1), (1, []))

    def test_page_counts(self):
        state = GalleryState()
        for count, expected in ((0, 1), (1, 1), (49, 1), (50, 1), (51, 2), (100, 2), (101, 3)):
            state.set_total_items(count)
            self.assertEqual(state.total_pages, expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
