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


# La parte Qt (paginación y envío del explorador de Librarian) se retiró con U1:
# explorar y enviar vive en Gallery (scripts/gallery_send_selftest.py).


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
