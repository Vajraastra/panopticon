"""Guarda por lote frente a commits, límites y errores; sólo SQLite temporal."""
from contextlib import contextmanager, closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import library_service, metadata_destinations as product
from core.library_service import LibraryError, LibraryService


class Destinations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LibraryService(Path(self.temp.name)/'fixture.db')
        self.addCleanup(self.service.close)

    def register(self, *paths):
        self.service.submit('register_files_minimal', [dict(path=p, filename=p.rsplit('/', 1)[-1],
            size=1, created=1) for p in paths]).result(3)

    def occupied(self, guard, paths, **kwargs):
        return guard.read('get_metadata_path_status', paths, **kwargs)['occupied']

    @contextmanager
    def traced(self, *, row_factory=None, trace=None, event=None):
        real = library_service.open_reader
        sql, connections = [], []
        def open_reader(path):
            conn = real(path)
            connections.append(conn)
            def record(statement):
                sql.append(statement)
                if trace: trace(statement)
            conn.set_trace_callback(record)
            if row_factory: conn.row_factory = row_factory
            return conn
        with patch.object(library_service, 'open_reader', open_reader):
            with self.service.metadata_destination_guard(cancel_event=event) as guard:
                yield guard, sql, connections

    def scans(self, sql):
        return sum(s.startswith('SELECT substr(path,1,4097)') for s in sql)

    def test_unicode_aliases_offline_match_legacy(self):
        path = 'C:/Colección/Straße/Ñ.png'
        self.register(path)
        key = self.service.read('get_file_id', path)
        state = self.service.read('get_copy_version_state', key)
        self.service.submit('archive_copy', key, expected_fingerprint=state['fingerprint']).result(3)
        with self.traced() as (guard, sql, _):
            for paths in (path, 'c:\\COLECCIÓN\\STRASSE\\ñ.png', ['C:/other.png', path], 'C:/missing.png'):
                self.assertEqual(guard.read('get_metadata_path_status', paths),
                                 self.service.read('get_metadata_path_status', paths))
            self.assertTrue(self.occupied(guard, 'c:/colección/strasse/ñ.png'))
            self.assertEqual(self.scans(sql), 1)

    def test_thousand_absent_queries_scan_once_release_transactions_no_ddl(self):
        self.register(*(f'C:/library/{i}.png' for i in range(100)))
        with self.traced() as (guard, sql, conns):
            before = conns[0].execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall()
            for i in range(1000):
                self.assertFalse(self.occupied(guard, f'C:/output/{i}.png'))
                self.assertFalse(conns[0].in_transaction)
            self.assertEqual(self.scans(sql), 1)
            self.assertEqual(before, conns[0].execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall())
            self.assertEqual(conns[0].execute('PRAGMA query_only').fetchone(), (1,))
        with self.assertRaises(sqlite3.ProgrammingError): conns[0].execute('SELECT 1')
        self.assertEqual(self.service.metrics_snapshot()['reads_active'], 0)

    def test_owner_commit_invalidates_absent_result(self):
        with self.traced() as (guard, sql, _):
            self.assertFalse(self.occupied(guard, 'C:/output/a.png'))
            self.register('C:/output/a.png')
            self.assertTrue(self.occupied(guard, 'C:/output/a.png'))
            self.assertEqual(self.scans(sql), 2)

    def test_external_connection_updates_and_deletes_invalidate(self):
        self.register('C:/old.png')
        with self.traced() as (guard, sql, _):
            self.assertFalse(self.occupied(guard, 'C:/new.png'))
            with closing(sqlite3.connect(self.service.db_path, isolation_level=None)) as external:
                external.execute("UPDATE files SET path='C:/new.png',is_offline=1")
            self.assertTrue(self.occupied(guard, 'C:/new.png'))
            self.assertFalse(self.occupied(guard, 'C:/old.png'))
            with closing(sqlite3.connect(self.service.db_path, isolation_level=None)) as external:
                external.execute('DELETE FROM files')
            self.assertFalse(self.occupied(guard, 'C:/new.png'))
            self.assertEqual(self.scans(sql), 3)

    def test_commit_during_scan_discards_old_snapshot(self):
        self.register('C:/old.png')
        changed = []
        def row(cursor, values):
            if not changed and isinstance(values[0], str) and values[0].startswith('C:/'):
                changed.append(True)
                self.register('C:/concurrent.png')
            return values
        with self.traced(row_factory=row) as (guard, sql, conns):
            self.assertTrue(self.occupied(guard, 'C:/concurrent.png'))
            self.assertEqual(self.scans(sql), 2)
            self.assertFalse(conns[0].in_transaction)

    def test_unstable_revision_fails_without_partial_approval(self):
        self.register('C:/old.png')
        rounds = []
        def row(cursor, values):
            if isinstance(values[0], str) and values[0].startswith('C:/') and len(rounds) < 3:
                rounds.append(True)
                self.register(f'C:/concurrent-{len(rounds)}.png')
            return values
        # Una escritura por recorrido, después de fijar el snapshot.
        calls = set()
        def trace(sql):
            if sql.startswith('SELECT substr'): calls.clear()
        def once(cursor, values):
            if not calls and isinstance(values[0], str) and values[0].startswith('C:/'):
                calls.add(1)
                return row(cursor, values)
            return values
        with self.traced(row_factory=once, trace=trace) as (guard, sql, conns):
            with self.assertRaisesRegex(LibraryError, 'revision_unstable'):
                self.occupied(guard, 'C:/absent.png')
            self.assertEqual(self.scans(sql), 3)
            self.assertFalse(conns[0].in_transaction)
            self.assertTrue(self.occupied(guard, 'C:/concurrent-3.png'))

    def test_row_limit_falls_back_complete_query_without_rebuilding_each_time(self):
        self.register(*(f'C:/library/{i}.png' for i in range(10)))
        with patch.object(product, 'MAX_ROWS', 2), self.traced() as (guard, sql, _):
            self.assertTrue(self.occupied(guard, 'C:/library/9.png'))
            self.assertFalse(self.occupied(guard, 'C:/absent.png'))
            self.assertEqual(self.scans(sql), 1)
            self.assertEqual(sum('metadata_casefold_v1(path)' in s for s in sql), 2)

    def test_byte_limit_falls_back_and_new_revision_is_rechecked(self):
        self.register('C:/long-name.png')
        with patch.object(product, 'MAX_BYTES', 1), self.traced() as (guard, sql, _):
            self.assertTrue(self.occupied(guard, 'C:/long-name.png'))
            self.register('C:/new.png')
            self.assertTrue(self.occupied(guard, 'C:/new.png'))
            self.assertEqual(self.scans(sql), 2)

    def test_python_memory_limit_falls_back_including_unicode_overhead(self):
        self.register('C:/ñ/𐐀.png')
        with patch.object(product, 'MAX_KEY_MEMORY', 1), self.traced() as (guard, sql, _):
            self.assertTrue(self.occupied(guard, 'c:/Ñ/𐐨.png'))
            self.assertFalse(self.occupied(guard, 'C:/missing.png'))
            self.assertEqual(self.scans(sql), 1)
            self.assertEqual(sum('metadata_casefold_v1(path)' in s for s in sql), 2)

    def test_invalid_requests_fail_before_sql(self):
        with self.traced() as (guard, sql, _):
            for paths in ([], [None], [''], ['x\x00'], ['x'*4097], ['x']*33, 12):
                with self.assertRaises(ValueError): self.occupied(guard, paths)
            self.assertEqual(sql, [])

    def test_cancel_cache_hit_and_pre_cancel(self):
        event = threading.Event()
        with self.traced(event=event) as (guard, sql, _):
            self.assertFalse(self.occupied(guard, 'C:/x.png'))
            event.set()
            with self.assertRaisesRegex(LibraryError, 'cancelled'): self.occupied(guard, 'C:/x.png')
            self.assertEqual(self.scans(sql), 1)
        self.assertEqual(self.service.metrics_snapshot()['reads_cancelled'], 1)

    def test_cancel_during_scan_cleans_snapshot_and_metrics(self):
        event = threading.Event()
        self.register(*(f'C:/library/{i}.png' for i in range(100)))
        def trace(sql):
            if sql.startswith('SELECT substr'): event.set()
        with self.traced(trace=trace, event=event) as (guard, _, conns):
            with self.assertRaisesRegex(LibraryError, 'cancelled'): self.occupied(guard, 'C:/x.png')
            self.assertFalse(conns[0].in_transaction)
        self.assertEqual(self.service.metrics_snapshot()['reads_cancelled'], 1)

    def test_deadline_fails_cache_hit_too(self):
        with self.traced() as (guard, _, _):
            self.assertFalse(self.occupied(guard, 'C:/x.png'))
            with patch.object(product, 'BUDGET_SECONDS', 0):
                with self.assertRaisesRegex(LibraryError, 'budget_exceeded'): self.occupied(guard, 'C:/x.png')

    def test_context_failure_and_closed_guard(self):
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with self.traced() as (guard, _, conns): raise RuntimeError('fixture')
        with self.assertRaisesRegex(LibraryError, 'guard_closed'): self.occupied(guard, 'C:/x.png')
        with self.assertRaises(sqlite3.ProgrammingError): conns[0].execute('SELECT 1')

    def test_service_close_invalidates_guard(self):
        with self.traced() as (guard, _, _):
            self.assertFalse(self.occupied(guard, 'C:/x.png'))
            self.service.close()
            with self.assertRaisesRegex(LibraryError, 'service_closed'): self.occupied(guard, 'C:/x.png')

    def test_guard_cannot_cross_worker_threads(self):
        errors = []
        with self.traced() as (guard, sql, _):
            def other():
                try: self.occupied(guard, 'C:/x.png')
                except Exception as exc: errors.append(exc)
            thread = threading.Thread(target=other)
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(errors[0], LibraryError)
            self.assertEqual(sql, [])

    def test_database_replacement_is_rejected(self):
        with self.traced() as (guard, _, _):
            with patch.object(guard, '_file_identity', return_value=(-1, -1)):
                with self.assertRaisesRegex(LibraryError, 'database_replaced'): self.occupied(guard, 'C:/x.png')

    def test_invalid_stored_path_rejects_then_recovers_after_commit(self):
        self.register('C:/old.png')
        with self.traced() as (guard, _, conns):
            self.assertFalse(self.occupied(guard, 'C:/x.png'))
            with closing(sqlite3.connect(self.service.db_path, isolation_level=None)) as external:
                external.execute("UPDATE files SET path=?", ('x'*5000,))
            with self.assertRaisesRegex(ValueError, 'invalid_stored'): self.occupied(guard, 'C:/x.png')
            self.assertFalse(conns[0].in_transaction)
            with closing(sqlite3.connect(self.service.db_path, isolation_level=None)) as external:
                external.execute("UPDATE files SET path='C:/x.png'")
            self.assertTrue(self.occupied(guard, 'C:/x.png'))


if __name__ == '__main__': unittest.main()
