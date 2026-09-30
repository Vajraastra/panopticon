"""Métricas de servicio con DB temporal; sin biblioteca ni WAL real."""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from core.library_repository import LibraryRepository
from core.library_service import LibraryError, LibraryService


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = LibraryService(self.root / 'fixture.db', capacity=1)
        self.addCleanup(self.service.close)
        self.releases = []
        self.addCleanup(lambda: [event.set() for event in self.releases])

    def hold(self, operation='register_files_minimal'):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = getattr(LibraryRepository, operation)
        def held(repo, *args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('fixture timeout')
            return original(repo, *args, **kwargs)
        return entered, release, patch.object(LibraryRepository, operation, held)

    def settled(self):
        self.service._queue.join()
        return self.service.metrics_snapshot()

    def balanced(self, value):
        self.assertEqual(value['accepted'], value['queued'] + bool(value['active']) +
                         value['committed'] + value['failed'] + value['cancelled'])
        self.assertGreaterEqual(value['queued'], 0)
        self.assertGreaterEqual(value['reads_active'], 0)

    def test_empty_detached_snapshot_has_no_sql_or_filesystem_io(self):
        with patch('core.library_service.sqlite3.connect', side_effect=AssertionError('SQL')), \
             patch('core.library_service.open_reader', side_effect=AssertionError('reader')), \
             patch('os.stat', side_effect=AssertionError('stat')):
            value = self.service.metrics_snapshot()
        self.assertEqual((value['capacity'], value['accepted'], value['queued'], value['active']), (1, 0, 0, None))
        value['accepted'] = 999
        self.assertEqual(self.service.metrics_snapshot()['accepted'], 0)

    def test_queue_full_active_and_waiting_are_distinguished_and_balanced(self):
        entered, release, hook = self.hold()
        with hook:
            first = self.service.submit('register_files_minimal', [])
            self.assertTrue(entered.wait(1))
            second = self.service.submit('register_files_minimal', [])
            with self.assertRaisesRegex(LibraryError, 'queue_full'):
                self.service.submit('register_files_minimal', []).result(1)
            value = self.service.metrics_snapshot()
            self.assertEqual((value['queued'], value['active'], value['accepted'], value['rejected_full']),
                             (1, 'register_files_minimal', 2, 1))
            self.balanced(value)
            release.set()
            self.assertTrue(first.result(3))
            self.assertTrue(second.result(3))
        value = self.settled()
        self.assertEqual((value['committed'], value['high_water'], value['queued'], value['active']), (2, 1, 0, None))
        self.assertEqual(value['queue_wait_count'], 2)
        self.assertGreater(value['queue_wait_max_seconds'], 0)
        self.balanced(value)

    def test_sql_failure_counts_once_and_next_command_commits(self):
        with patch.object(LibraryRepository, 'register_files_minimal', side_effect=ValueError('fixture_failure')):
            with self.assertRaisesRegex(LibraryError, 'fixture_failure'):
                self.service.submit('register_files_minimal', []).result(3)
        self.assertTrue(self.service.submit('register_files_minimal', []).result(3))
        value = self.settled()
        self.assertEqual((value['accepted'], value['failed'], value['committed']), (2, 1, 1))
        self.assertEqual((value['begin_count'], value['commit_count'], value['write_count']), (2, 1, 2))
        self.balanced(value)

    def test_cancelled_queued_write_never_runs_sql_and_counts_once(self):
        entered, release, hook = self.hold()
        with hook:
            first = self.service.submit('register_files_minimal', [])
            self.assertTrue(entered.wait(1))
            second = self.service.submit('register_files_minimal', [])
            self.assertTrue(second.cancel())
            release.set()
            self.assertTrue(first.result(3))
        value = self.settled()
        self.assertEqual((value['accepted'], value['committed'], value['cancelled']), (2, 1, 1))
        self.assertEqual(value['begin_count'], 1)
        self.balanced(value)

    def test_close_drains_work_and_rejects_without_inflating_acceptance(self):
        self.assertTrue(self.service.submit('register_files_minimal', []).result(3))
        self.service.close()
        with self.assertRaisesRegex(LibraryError, 'service_closed'):
            self.service.submit('register_files_minimal', []).result(1)
        with self.assertRaisesRegex(LibraryError, 'service_closed'):
            self.service.read('count_files')
        self.service.close()
        value = self.service.metrics_snapshot()
        self.assertFalse(value['accepting'])
        self.assertEqual((value['accepted'], value['committed'], value['rejected_closed'], value['reads_rejected_closed']), (1, 1, 1, 1))
        self.balanced(value)

    def test_read_success_error_and_cancellation_have_separate_counters(self):
        self.assertEqual(self.service.read('count_files'), 0)
        with self.assertRaises(ValueError):
            self.service.read('get_library_page', limit=0)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(LibraryError, 'cancelled'):
            self.service.read('count_files', cancel_event=cancelled)
        value = self.service.metrics_snapshot()
        self.assertEqual((value['reads_completed'], value['reads_failed'], value['reads_cancelled'], value['reads_active']), (1, 1, 1, 0))
        self.assertEqual(value['read_count'], 3)
        self.assertGreaterEqual(value['read_seconds'], value['read_max_seconds'])

    def test_active_read_visible_while_sql_and_owner_remain_independent(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def hold(repo):
            entered.set()
            self.assertTrue(release.wait(3))
            return 0
        with patch.object(LibraryRepository, 'count_files', hold), ThreadPoolExecutor(max_workers=1) as pool:
            read = pool.submit(self.service.read, 'count_files')
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.service.metrics_snapshot()['reads_active'], 1)
                self.assertTrue(self.service.submit('register_files_minimal', []).result(3))
            finally:
                release.set()
            self.assertEqual(read.result(3), 0)
        self.assertEqual(self.service.metrics_snapshot()['reads_active'], 0)

    def test_noop_commits_are_commands_not_data_revisions_or_changed_rows(self):
        for _ in range(2):
            self.assertTrue(self.service.submit('register_files_minimal', []).result(3))
        value = self.settled()
        self.assertEqual(value['committed'], 2)
        self.assertNotIn('revision', value)
        self.assertNotIn('changed_rows', value)
        self.assertEqual(self.service.read('count_files'), 0)

    def test_fatal_writer_resolves_queue_and_balances_accounting(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def broken(repo, *args):
            entered.set()
            self.assertTrue(release.wait(3))
            repo.conn.close()
            return True
        with patch.object(LibraryRepository, 'register_files_minimal', broken):
            first = self.service.submit('register_files_minimal', [])
            self.assertTrue(entered.wait(1))
            second = self.service.submit('register_files_minimal', [])
            release.set()
            for future in (first, second):
                with self.assertRaisesRegex(LibraryError, 'writer_stopped'):
                    future.result(3)
        self.service.close()
        value = self.service.metrics_snapshot()
        self.assertEqual((value['accepted'], value['failed'], value['queued'], value['active']), (2, 2, 0, None))
        self.assertFalse(value['accepting'])
        self.balanced(value)

    def test_invalid_capacity_cannot_create_unbounded_queue_or_start_thread(self):
        with patch('core.library_service.threading.Thread', side_effect=AssertionError('thread')):
            for capacity in (0, -1, True, '64', 1.5):
                with self.assertRaisesRegex(ValueError, 'invalid_queue_capacity'):
                    LibraryService(self.root / 'invalid.db', capacity=capacity)
        self.assertFalse((self.root / 'invalid.db').exists())

    def test_many_readers_have_no_lost_updates_or_unbounded_history(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(lambda _: self.service.read('count_files'), range(100))), [0] * 100)
        value = self.service.metrics_snapshot()
        self.assertEqual((value['reads_completed'], value['reads_active'], value['read_count']), (100, 0, 100))
        self.assertTrue(all(isinstance(item, (int, float, bool, str, type(None))) for item in value.values()))
        self.assertNotIn(str(self.root), str(value))

    def test_future_callbacks_observe_final_accounting_without_deadlock(self):
        captured, done = [], threading.Event()
        entered, release, hook = self.hold()
        with hook:
            future = self.service.submit('register_files_minimal', [])
            self.assertTrue(entered.wait(1))
            future.add_done_callback(lambda result: (captured.append(self.service.metrics_snapshot()), done.set()))
            release.set()
            self.assertTrue(future.result(3))
            self.assertTrue(done.wait(1))
        self.assertEqual(captured[0]['committed'], 1)
        self.assertIsNone(captured[0]['active'])

    def test_begin_lock_failure_is_timed_separately_from_commit_and_recovers(self):
        def timeout(repo, rows):
            repo.conn.execute('PRAGMA busy_timeout=25')
            return True
        with patch.object(LibraryRepository, 'register_files_minimal', timeout):
            self.assertTrue(self.service.submit('register_files_minimal', []).result(3))
        before = self.settled()
        blocker = sqlite3.connect(self.service.db_path, isolation_level=None)
        try:
            blocker.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(LibraryError, 'locked'):
                self.service.submit('register_files_minimal', []).result(1)
        finally:
            blocker.rollback()
            blocker.close()
        value = self.settled()
        self.assertEqual(value['begin_count'] - before['begin_count'], 1)
        self.assertEqual(value['commit_count'], before['commit_count'])
        self.assertGreaterEqual(value['begin_seconds'] - before['begin_seconds'], .02)
        self.assertEqual(value['failed'], 1)
        self.assertTrue(self.service.submit('register_files_minimal', []).result(3))
        self.balanced(self.settled())

    def test_sqlite_progress_interruption_is_counted_as_cancelled_read(self):
        from core import library_service
        event = threading.Event()
        real_open = library_service.open_reader
        def reader(path):
            conn = real_open(path)
            conn.set_trace_callback(lambda sql: event.set() if sql.startswith('WITH RECURSIVE') else None)
            return conn
        def expensive(repo):
            return repo.conn.execute('WITH RECURSIVE x(n) AS (VALUES(0) UNION ALL '
                'SELECT n+1 FROM x WHERE n<1000000) SELECT sum(n) FROM x').fetchone()[0]
        with patch.object(library_service, 'open_reader', reader), \
             patch.object(LibraryRepository, 'count_files', expensive):
            with self.assertRaisesRegex(LibraryError, 'cancelled'):
                self.service.read('count_files', cancel_event=event)
        value = self.service.metrics_snapshot()
        self.assertEqual((value['reads_cancelled'], value['reads_failed'], value['reads_active']), (1, 0, 0))

    def test_snapshot_remains_consistent_under_concurrent_producers_and_readers(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            def produce(_):
                future = self.service.submit('register_files_minimal', [])
                try:
                    future.result(3)
                except LibraryError as error:
                    self.assertEqual(error.reason, 'queue_full')
                self.balanced(self.service.metrics_snapshot())
                return self.service.read('count_files')
            self.assertEqual(list(pool.map(produce, range(100))), [0] * 100)
        value = self.settled()
        self.assertEqual(value['accepted'] + value['rejected_full'], 100)
        self.assertEqual(value['committed'], value['accepted'])
        self.balanced(value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
