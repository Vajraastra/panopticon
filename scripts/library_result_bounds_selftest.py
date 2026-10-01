"""Respuestas paginadas privadas: nunca aprobar un prefijo ni abrir datos reales."""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from core.library_async import LibraryReader
from core.library_connections import open_reader
from core.library_repository import LibraryRepository
from core.library_results import ReadResultError, ResultBudget, collect_rows
from core.library_service import LibraryError, LibraryService
from gallery_data_selftest import pump


class PoisonConnection:
    def cursor(self):
        raise AssertionError('SQL must not run for invalid paging')


class CursorProbe:
    def __init__(self, values):
        self.values = values
        self.pulled = 0
        self.closed = False

    def __iter__(self):
        for value in self.values:
            self.pulled += 1
            yield value

    def close(self):
        self.closed = True


class PageValidation(unittest.TestCase):
    def calls(self, repo, limit):
        return [lambda: repo.get_files_recursive('C:/images', limit=limit),
                lambda: repo.get_folder_preview('C:/images', limit),
                lambda: repo.search_by_terms([], limit),
                lambda: repo.search_files_paginated(limit=limit),
                lambda: repo.get_folders_paginated(limit=limit),
                lambda: repo.get_files_missing_hash(limit=limit)]

    def test_negative_zero_bool_float_and_huge_limits_before_sql(self):
        repo = LibraryRepository(PoisonConnection())
        for limit in [-1, 0, True, 1.0, '5', 10001, 2**100]:
            for call in self.calls(repo, limit):
                with self.subTest(limit=limit, call=call):
                    with self.assertRaisesRegex(ReadResultError, 'invalid_read_page'):
                        call()

    def test_offsets_and_hash_cursor_are_exact_nonnegative_sqlite_integers(self):
        repo = LibraryRepository(PoisonConnection())
        for value in [-1, True, .5, None, '0', 2**63]:
            for call in [lambda: repo.search_files_paginated(offset=value),
                         lambda: repo.get_folders_paginated(offset=value),
                         lambda: repo.get_files_missing_hash(after_id=value)]:
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ReadResultError, 'invalid_read_page'):
                        call()

    def test_collect_stops_before_materializing_remaining_rows_and_closes(self):
        cursor = CursorProbe((('x'*200,) for _ in range(10000)))
        with patch('core.library_results.MAX_RESULT_BYTES', 1000):
            with self.assertRaisesRegex(ReadResultError, 'read_result_too_large'):
                collect_rows(cursor, lambda row: row[0])
        self.assertLess(cursor.pulled, 10)
        self.assertTrue(cursor.closed)

    def test_cell_and_row_caps_close_without_partial_return(self):
        cursor = CursorProbe([('small',), ('x'*1000,), ('never',)])
        with patch('core.library_results.MAX_CELL_BYTES', 200):
            with self.assertRaises(ReadResultError):
                collect_rows(cursor, lambda row: row[0])
        self.assertEqual(cursor.pulled, 2)
        self.assertTrue(cursor.closed)
        cursor = CursorProbe([(1,), (2,), (3,), (4,)])
        with patch('core.library_results.MAX_PAGE_ROWS', 2):
            with self.assertRaises(ReadResultError):
                collect_rows(cursor)
        self.assertEqual(cursor.pulled, 3)
        self.assertTrue(cursor.closed)

    def test_success_and_transform_failure_close_cursor(self):
        cursor = CursorProbe([(1,), (2,)])
        self.assertEqual(collect_rows(cursor), [(1,), (2,)])
        self.assertTrue(cursor.closed)
        cursor = CursorProbe([(1,)])
        with self.assertRaises(ZeroDivisionError):
            collect_rows(cursor, lambda row: 1/0)
        self.assertTrue(cursor.closed)


class ResultSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'private.db'
        self.service = LibraryService(self.path)
        self.addCleanup(self.service.close)
        self.folder = 'C:/private/images'
        self.paths = [self.folder + '/image-%02d.png' % n for n in range(12)]
        self.assertTrue(self.service.submit('register_files_minimal', [
            dict(path=path, filename=path.rsplit('/', 1)[-1], size=1, created=1)
            for path in self.paths]).result(3))
        self.assertTrue(self.service.submit('add_watched_folder', self.folder).result(3))
        self.assertTrue(self.service.submit('update_file_metadata', self.paths[0],
            dict(positive='unique needle')).result(3))
        self.schema = self.sql("SELECT name, sql FROM sqlite_master ORDER BY name")

    def sql(self, statement, parameters=()):
        with closing(sqlite3.connect(self.path)) as conn:
            value = conn.execute(statement, parameters).fetchall()
            conn.commit()
            return value

    def assert_reason(self, operation, reason, *args, **kwargs):
        with self.assertRaises(LibraryError) as caught:
            self.service.read(operation, *args, **kwargs)
        self.assertEqual((caught.exception.operation, caught.exception.reason),
                         (operation, reason))

    def test_service_rejects_unbounded_page_values_and_remains_usable(self):
        for operation in ['search_files_paginated', 'get_folders_paginated',
                          'get_files_missing_hash']:
            self.assert_reason(operation, 'invalid_read_page', limit=-1)
        self.assertEqual(self.service.read('count_files'), 12)
        metrics = self.service.metrics_snapshot()
        self.assertEqual(metrics['reads_failed'], 3)
        self.assertEqual(metrics['reads_rejected_result'], 0)
        self.assertEqual(metrics['reads_active'], 0)

    def test_full_count_and_offset_page_preserved(self):
        total, rows = self.service.read('search_files_paginated', limit=3, offset=4)
        self.assertEqual(total, 12)
        self.assertEqual(rows, [(p, 0) for p in self.paths[::-1][4:7]])
        self.assertEqual(self.service.read('search_files_paginated', limit=3, offset=100),
                         (12, []))
        self.assertEqual(self.service.read('search_files_paginated', limit=1, offset=2**63-1),
                         (12, []))

    def test_folder_preview_and_recursive_requested_limits(self):
        for operation in ['get_folder_preview', 'get_files_recursive']:
            value = self.service.read(operation, self.folder, limit=3)
            self.assertEqual(len(value), 3)
            self.assertTrue(set(value).issubset(self.paths))
        self.assertEqual(self.service.read('get_folder_preview', 'C:/absent', limit=1), [])

    def test_terms_keep_full_count_and_empty_terms_validate_limit(self):
        count, values = self.service.read('search_by_terms', ['unique needle'], limit=1)
        self.assertEqual((count, values), (1, [self.paths[0]]))
        self.assert_reason('search_by_terms', 'invalid_read_page', [], limit=-1)
        self.assertEqual(self.service.read('search_by_terms', [], limit=1), (0, []))

    def test_backfill_keyset_has_no_repeat_and_offline_excluded(self):
        rows = self.service.read('get_files_missing_hash', limit=4)
        next_rows = self.service.read('get_files_missing_hash', after_id=rows[-1][0], limit=4)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(next_rows), 4)
        self.assertFalse(set(rows).intersection(next_rows))
        self.assertTrue(self.service.submit('remove_files', [rows[0][1]]).result(3))
        self.assertNotIn(rows[0], self.service.read('get_files_missing_hash', limit=10))

    def test_legacy_complete_inventory_is_not_silently_truncated(self):
        with patch('core.library_results.MAX_PAGE_ROWS', 2):
            self.assertEqual(set(self.service.read('get_files_recursive', self.folder)), set(self.paths))
            self.assertEqual(self.service.read('get_known_files_in_folder', self.folder), set(self.paths))

    def test_accumulated_result_rejected_without_mutating_inventory_or_schema(self):
        operations = [('get_files_recursive', (self.folder,), dict(limit=12)),
                      ('get_folder_preview', (self.folder,), dict(limit=12)),
                      ('search_files_paginated', (), dict(limit=12)),
                      ('get_files_missing_hash', (), dict(limit=12))]
        with patch('core.library_results.MAX_RESULT_BYTES', 240):
            for operation, args, kwargs in operations:
                self.assert_reason(operation, 'read_result_too_large', *args, **kwargs)
        self.assertEqual(self.service.read('count_files'), 12)
        self.assertEqual(self.sql("SELECT name, sql FROM sqlite_master ORDER BY name"), self.schema)
        metrics = self.service.metrics_snapshot()
        self.assertEqual(metrics['reads_rejected_result'], 4)
        self.assertEqual(metrics['reads_failed'], 4)
        self.assertEqual(metrics['reads_active'], 0)

    def test_terms_reject_oversized_preview_without_count_only_success(self):
        with patch('core.library_results.MAX_RESULT_BYTES', 60):
            self.assert_reason('search_by_terms', 'read_result_too_large', ['unique needle'])

    def test_folder_dict_budget_includes_names_counts_and_cover(self):
        with patch('core.library_results.MAX_RESULT_BYTES', 200):
            self.assert_reason('get_folders_paginated', 'read_result_too_large', limit=1)
        total, rows = self.service.read('get_folders_paginated', limit=1)
        self.assertEqual(total, 1)
        self.assertEqual((rows[0]['path'], rows[0]['count']), (self.folder, 12))
        self.assertIn(rows[0]['cover'], self.paths)

    def test_historical_oversized_path_not_delivered_and_reader_is_closed(self):
        huge = 'C:/private/' + 'x'*1024*1024
        self.sql('UPDATE files SET path=? WHERE id=1', (huge,))
        connections = []
        def tracked(path):
            conn = open_reader(path)
            connections.append(conn)
            return conn
        with patch('core.library_service.open_reader', tracked):
            self.assert_reason('get_files_missing_hash', 'read_result_too_large', limit=1)
        self.assertEqual(len(connections), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute('SELECT 1')
        self.assertEqual(self.service.read('count_files'), 12)

    def test_cancel_wins_over_result_budget_rejection(self):
        event = threading.Event()
        def interrupted(budget, value):
            event.set()
            raise ReadResultError('read_result_too_large')
        with patch.object(ResultBudget, 'add', interrupted):
            self.assert_reason('search_files_paginated', 'cancelled', cancel_event=event)
        metrics = self.service.metrics_snapshot()
        self.assertEqual(metrics['reads_cancelled'], 1)
        self.assertEqual(metrics['reads_rejected_result'], 0)
        self.assertEqual(metrics['reads_active'], 0)

    def test_deadline_wins_over_result_budget_rejection(self):
        clock = [0]
        def expired(budget, value):
            clock[0] = 11
            raise ReadResultError('read_result_too_large')
        with patch('core.library_service.time.monotonic', lambda: clock[0]), \
                patch.object(ResultBudget, 'add', expired):
            self.assert_reason('search_files_paginated', 'read_budget_exceeded')
        metrics = self.service.metrics_snapshot()
        self.assertEqual(metrics['reads_failed'], 1)
        self.assertEqual(metrics['reads_rejected_result'], 0)
        self.assertEqual(metrics['reads_active'], 0)

    def test_qt_gets_error_only_then_can_request_a_normal_page(self):
        reader = LibraryReader(self.service)
        errors, values = [], []
        reader.result.connect(lambda ticket, value, error:
            (errors if error is not None else values).append((ticket, value, error)))
        try:
            with patch('core.library_results.MAX_RESULT_BYTES', 240):
                ticket = reader.request('search_files_paginated', limit=12)
                self.assertTrue(pump(lambda: len(errors) == 1))
                self.assertEqual(errors[0][0], ticket)
                self.assertIn('read_result_too_large', str(errors[0]))
                self.assertEqual(values, [])
            reader.request('search_files_paginated', limit=2)
            self.assertTrue(pump(lambda: len(values) == 1))
            self.assertEqual(values[0][1][0], 12)
            self.assertEqual(len(values[0][1][1]), 2)
        finally:
            reader.close()
            if reader._thread:
                reader._thread.join(3)
                self.assertFalse(reader._thread.is_alive())
            self.app.processEvents()

    def test_actual_large_count_page_keeps_ten_thousand_cap(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executemany('INSERT INTO files(path, filename) VALUES (?, ?)',
                ((self.folder+'/extra-%05d.png' % n, 'extra.png') for n in range(10000)))
            conn.commit()
        total, rows = self.service.read('search_files_paginated', limit=10000)
        self.assertEqual(total, 10012)
        self.assertEqual(len(rows), 10000)
        self.assert_reason('search_files_paginated', 'invalid_read_page', limit=10001)

    def test_selection_page_and_complete_transfer_never_deliver_oversized_prefix(self):
        with patch('core.library_results.MAX_RESULT_BYTES', 240):
            self.assert_reason('get_library_page', 'read_result_too_large',
                               folder=self.folder, limit=4)
            self.assert_reason('resolve_library_selection', 'read_result_too_large',
                               folder=self.folder, limit=12)
            self.assert_reason('get_library_selection', 'read_result_too_large',
                               folder=self.folder, limit=12)
        page = self.service.read('get_library_page', folder=self.folder, limit=4, offset=4)
        self.assertEqual((page['count'], page['paths']), (12, self.paths[4:8]))
        complete = self.service.read('resolve_library_selection', folder=self.folder, limit=12)
        self.assertEqual((complete['count'], complete['paths']), (12, self.paths))

    def test_complete_transfer_size_contract_and_last_page_clamp_preserved(self):
        with self.assertRaisesRegex(ValueError, 'selection_requires_streaming'):
            self.service.read('resolve_library_selection', folder=self.folder, limit=5)
        page = self.service.read('get_library_page', folder=self.folder, limit=5, offset=100)
        self.assertEqual((page['count'], page['offset'], page['paths']),
                         (12, 10, self.paths[10:]))


if __name__ == '__main__':
    unittest.main()
