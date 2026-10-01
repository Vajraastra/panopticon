"""Lecturas acotadas y entrega Qt, con conexiones y DB temporales propias."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication
from core import library_service as service_module
from core.library_async import LibraryReader
from core.library_payload import MAX_FIELD_BYTES
from core.library_repository import LibraryRepository
from core.library_service import LibraryError, LibraryService


def pump(predicate, seconds=3):
    deadline = time.monotonic()+seconds
    while time.monotonic()<deadline:
        QApplication.processEvents()
        if predicate(): return True
        time.sleep(.002)
    return False


class ReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='native-read-budget-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.service = LibraryService(self.root/'native.db')
        self.addCleanup(self.service.close)
        self.releases = []
        self.readers = []
        self.addCleanup(self.cleanup_readers)

    def cleanup_readers(self):
        for event in self.releases: event.set()
        for reader in self.readers: reader.close()
        self.assertTrue(pump(lambda: all(r.shutdown_ready() for r in self.readers)))
        QApplication.processEvents()

    def reader(self):
        reader = LibraryReader(self.service)
        self.readers.append(reader)
        results = []
        reader.result.connect(lambda ticket,value,error: results.append((ticket,value,error,QThread.currentThread())))
        return reader, results

    def assert_closed(self, conn):
        with self.assertRaises(sqlite3.ProgrammingError): conn.execute('SELECT 1')

    def expensive(self, repo):
        return repo.conn.execute('WITH RECURSIVE x(n) AS (VALUES(0) UNION ALL '
            'SELECT n+1 FROM x WHERE n<1000000) SELECT sum(n) FROM x').fetchone()[0]

    def test_every_read_has_default_budget(self):
        with patch.object(service_module, 'DEFAULT_READ_BUDGET_SECONDS', 0), \
             patch.object(service_module, 'open_reader', side_effect=AssertionError('SQL admitted')):
            with self.assertRaisesRegex(LibraryError, 'read_budget_exceeded'):
                self.service.read('count_files')
        self.assertEqual(self.service.metrics_snapshot()['reads_failed'], 1)
        self.assertEqual(self.service.metrics_snapshot()['reads_active'], 0)

    def test_existing_discovery_timeout_reason_is_preserved(self):
        with patch.dict(service_module.READ_BUDGET_SECONDS, get_duplicate_candidates_page=0):
            with self.assertRaisesRegex(LibraryError, 'duplicate_discovery_budget_exceeded'):
                self.service.read('get_duplicate_candidates_page', [])

    def test_sql_deadline_interrupts_and_releases_reader(self):
        connections = []
        original = service_module.open_reader
        def reader(path):
            conn = original(path)
            connections.append(conn)
            return conn
        with patch.object(service_module, 'DEFAULT_READ_BUDGET_SECONDS', .005), \
             patch.object(service_module, 'open_reader', reader), \
             patch.object(LibraryRepository, 'count_files', lambda repo: self.expensive(repo)):
            with self.assertRaisesRegex(LibraryError, 'read_budget_exceeded'):
                self.service.read('count_files')
        self.assertEqual(len(connections), 1)
        self.assert_closed(connections[0])
        self.assertTrue(self.service.submit('register_files_minimal', []).result(3))
        self.assertEqual(self.service.read('count_files'), 0)

    def test_cancellation_before_admission_opens_no_reader_or_payload_copy(self):
        event = threading.Event()
        event.set()
        with patch.object(service_module, 'open_reader', side_effect=AssertionError('SQL')), \
             patch.object(service_module, 'snapshot_read', side_effect=AssertionError('copy')):
            with self.assertRaisesRegex(LibraryError, 'cancelled'):
                self.service.read('count_files', object(), cancel_event=event)
        self.assertEqual(self.service.metrics_snapshot()['reads_cancelled'], 1)

    def test_cancel_during_python_result_work_discards_result_and_closes(self):
        event = threading.Event()
        connections = []
        def work(repo):
            connections.append(repo.conn)
            event.set()
            return ['would be stale']
        with patch.object(LibraryRepository, 'count_files', work):
            with self.assertRaisesRegex(LibraryError, 'cancelled'):
                self.service.read('count_files', cancel_event=event)
        self.assert_closed(connections[0])
        self.assertEqual(self.service.metrics_snapshot()['reads_cancelled'], 1)

    def test_deadline_during_python_result_work_discards_late_result(self):
        connections = []
        def work(repo):
            connections.append(repo.conn)
            time.sleep(.03)
            return ['would be late']
        with patch.object(service_module, 'DEFAULT_READ_BUDGET_SECONDS', .01), \
             patch.object(LibraryRepository, 'count_files', work):
            with self.assertRaisesRegex(LibraryError, 'read_budget_exceeded'):
                self.service.read('count_files')
        self.assert_closed(connections[0])

    def test_large_read_request_is_rejected_before_sql(self):
        with patch.object(service_module, 'open_reader', side_effect=AssertionError('SQL')):
            with self.assertRaisesRegex(LibraryError, 'read_payload_too_large'):
                self.service.read('count_files', 'x'*MAX_FIELD_BYTES)
        value = self.service.metrics_snapshot()
        self.assertEqual((value['reads_rejected_payload'],value['reads_failed'],value['reads_active']), (1,1,0))

    def test_custom_hooks_are_rejected_without_execution(self):
        class Unsafe:
            def __deepcopy__(self, memo): raise AssertionError('hook')
        with patch.object(service_module, 'open_reader', side_effect=AssertionError('SQL')):
            with self.assertRaisesRegex(LibraryError, 'invalid_read_payload'):
                self.service.read('count_files', Unsafe())

    def test_cyclic_request_rejected_and_next_read_works(self):
        value = []
        value.append(value)
        with self.assertRaisesRegex(LibraryError, 'invalid_read_payload'):
            self.service.read('count_files', value)
        self.assertEqual(self.service.read('count_files'), 0)

    def test_read_receives_detached_producer_payload(self):
        source = ['original']
        def work(repo, values):
            source.append('late')
            return values
        with patch.object(LibraryRepository, 'count_files', work):
            self.assertEqual(self.service.read('count_files', source), ['original'])

    def test_closed_service_rejects_before_read_snapshot(self):
        self.service.close()
        with patch.object(service_module, 'snapshot_read', side_effect=AssertionError('copy')):
            with self.assertRaisesRegex(LibraryError, 'service_closed'):
                self.service.read('count_files', object())

    def test_cancel_wins_over_elapsed_budget(self):
        event = threading.Event()
        event.set()
        with patch.object(service_module, 'DEFAULT_READ_BUDGET_SECONDS', 0):
            with self.assertRaisesRegex(LibraryError, 'cancelled'):
                self.service.read('count_files', cancel_event=event)

    def test_short_operation_finishes_without_partial_or_changed_state(self):
        schema = self.service.read('count_files')
        self.assertEqual(schema, 0)
        with closing(sqlite3.connect(self.root/'native.db')) as conn:
            before = conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()
        self.assertEqual(self.service.read('get_all_tags'), [])
        with closing(sqlite3.connect(self.root/'native.db')) as conn:
            self.assertEqual(conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall(), before)

    def test_qt_invalid_payload_uses_queued_delivery_without_sql_or_thread(self):
        reader, results = self.reader()
        with patch.object(self.service, 'read', side_effect=AssertionError('SQL')), \
             patch('core.library_async.threading.Thread', side_effect=AssertionError('thread')):
            ticket = reader.request('count_files', 'x'*MAX_FIELD_BYTES)
            self.assertEqual(results, [])
            self.assertTrue(pump(lambda: bool(results)))
        self.assertEqual(results[0][0], ticket)
        self.assertIsNone(results[0][1])
        self.assertEqual(results[0][2].reason, 'read_payload_too_large')
        self.assertEqual(results[0][3], self.app.thread())
        self.assertTrue(reader.shutdown_ready())

    def test_qt_pending_invalid_request_cannot_start_an_extra_reader(self):
        reader, results = self.reader()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []
        def work(operation, *args, cancel_event=None, **kwargs):
            calls.append(operation)
            entered.set()
            if not release.wait(3): raise AssertionError('private timeout')
            return 'old'
        with patch.object(self.service, 'read', work):
            reader.request('count_files')
            self.assertTrue(entered.wait(3))
            ticket = reader.request('count_files', 'x'*MAX_FIELD_BYTES)
            QApplication.processEvents()
            self.assertEqual(results, [])
            self.assertEqual(calls, ['count_files'])
            release.set()
            self.assertTrue(pump(lambda: bool(results)))
        self.assertEqual([(r[0],r[2].reason) for r in results], [(ticket,'read_payload_too_large')])

    def test_qt_latest_valid_request_replaces_pending_invalid_request(self):
        reader, results = self.reader()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []
        def work(operation, *args, cancel_event=None, **kwargs):
            calls.append(args)
            if not args:
                entered.set()
                if not release.wait(3): raise AssertionError('private timeout')
            return args
        with patch.object(self.service, 'read', work):
            reader.request('count_files')
            self.assertTrue(entered.wait(3))
            reader.request('count_files', object())
            source = ['latest']
            ticket = reader.request('count_files', source)
            source.append('changed')
            release.set()
            self.assertTrue(pump(lambda: bool(results)))
        self.assertEqual(calls, [(), (['latest'],)])
        self.assertEqual([(r[0],r[1],r[2]) for r in results], [(ticket,(['latest'],),None)])

    def test_qt_close_drops_queued_preflight_error(self):
        reader, results = self.reader()
        reader.request('count_files', object())
        reader.close()
        QApplication.processEvents()
        self.assertEqual(results, [])
        self.assertTrue(reader.shutdown_ready())

    def test_qt_reader_continues_after_payload_rejection(self):
        reader, results = self.reader()
        reader.request('count_files', object())
        self.assertTrue(pump(lambda: bool(results)))
        ticket = reader.request('count_files')
        self.assertTrue(pump(lambda: len(results)==2))
        self.assertEqual((results[1][0],results[1][1],results[1][2]), (ticket,0,None))


if __name__ == '__main__': unittest.main(verbosity=2)
