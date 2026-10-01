"""Physical WAL and live occupancy on private SQLite; no checkpoint tuning."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from queue import Queue
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.library_metrics import LibraryMetrics
from core.library_repository import LibraryRepository
from core.library_service import LibraryService
from core.library_wal import WalSampler


def wait(predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.003)
    raise AssertionError('private fixture did not reach expected state')


class Scalars(unittest.TestCase):
    def test_initial_and_detached_snapshot_have_only_scalars(self):
        metrics = LibraryMetrics(2)
        first = metrics.snapshot()
        self.assertEqual((first['wal_status'], first['wal_bytes'], first['wal_sample_age_seconds']),
                         ('unsampled', None, None))
        self.assertFalse(first['wal_observer_active'])
        self.assertTrue(all(type(value) in (str, int, float, bool, type(None)) for value in first.values()))
        first['wal_status'] = 'fake'
        self.assertEqual(metrics.snapshot()['wal_status'], 'unsampled')

    def test_queue_busy_means_continuous_backlog_not_oldest_pending_item(self):
        metrics, queue = LibraryMetrics(2), Queue(2)
        with patch('core.library_metrics.time.perf_counter', return_value=10) as clock:
            metrics.enqueue(queue, 'first')
            clock.return_value = 12
            metrics.enqueue(queue, 'second')
            clock.return_value = 20
            queue.get_nowait()
            started = metrics.start_write('first', 10)
            snapshot = metrics.snapshot()
            self.assertEqual(snapshot['queue_busy_seconds'], 10)
            self.assertEqual(snapshot['write_active_seconds'], 0)
            clock.return_value = 21
            self.assertEqual(metrics.snapshot()['write_active_seconds'], 1)
            metrics.finish_write('committed', started)
            self.assertEqual(metrics.snapshot()['write_active_seconds'], 0)
            queue.get_nowait()
            metrics.discard(cancelled=True)
            self.assertEqual(metrics.snapshot()['queue_busy_seconds'], 0)
            clock.return_value = 30
            metrics.enqueue(queue, 'third')
            clock.return_value = 32
            self.assertEqual(metrics.snapshot()['queue_busy_seconds'], 2)

    def test_reads_busy_covers_overlapping_activity_not_oldest_active_read(self):
        metrics = LibraryMetrics(2)
        with patch('core.library_metrics.time.perf_counter', return_value=10) as clock:
            first = metrics.start_read()
            clock.return_value = 12
            second = metrics.start_read()
            clock.return_value = 20
            metrics.finish_read('completed', first)
            self.assertEqual(metrics.snapshot()['reads_busy_seconds'], 10)
            metrics.finish_read('cancelled', second)
            self.assertEqual(metrics.snapshot()['reads_busy_seconds'], 0)
            self.assertEqual(metrics.snapshot()['reads_active'], 0)

    def test_wal_sampling_is_throttled_and_error_recovers_without_stale_bytes(self):
        metrics = LibraryMetrics(2)
        sampler = WalSampler('C:/private/native.db', metrics)
        with patch('core.library_wal.time.perf_counter', return_value=10) as clock, patch(
                'core.library_wal.os.stat', return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_size=42)) as probe:
            sampler.poll()
            for _ in range(1000):
                sampler.poll()
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(metrics.snapshot()['wal_bytes'], 42)
            clock.return_value = 11
            probe.side_effect = PermissionError('private-stat-failure')
            sampler.poll()
            value = metrics.snapshot()
            self.assertEqual((value['wal_status'], value['wal_bytes'], value['wal_sample_errors']), ('error', None, 1))
            clock.return_value = 12
            probe.side_effect = None
            probe.return_value = SimpleNamespace(st_mode=stat.S_IFREG, st_size=84)
            sampler.poll()
            value = metrics.snapshot()
            self.assertEqual((value['wal_status'], value['wal_bytes'], value['wal_samples']), ('present', 84, 3))
            self.assertEqual(value['wal_sample_age_seconds'], 0)
            clock.return_value = 15
            self.assertEqual(metrics.snapshot()['wal_sample_age_seconds'], 3)
        sampler.stop()
        self.assertFalse(metrics.snapshot()['wal_observer_active'])

    def test_absent_invalid_and_unexpected_probe_error_do_not_raise(self):
        for probe_result in (FileNotFoundError(), PermissionError(), RuntimeError('unexpected'),
                             SimpleNamespace(st_mode=stat.S_IFDIR, st_size=42),
                             SimpleNamespace(st_mode=stat.S_IFREG, st_size=-1)):
            with self.subTest(probe_result=type(probe_result).__name__):
                metrics = LibraryMetrics(2)
                sampler = WalSampler('C:/private/native.db', metrics)
                with patch('core.library_wal.os.stat') as probe:
                    if isinstance(probe_result, Exception):
                        probe.side_effect = probe_result
                    else:
                        probe.return_value = probe_result
                    sampler.poll()
                value = metrics.snapshot()
                expected = 'absent' if isinstance(probe_result, FileNotFoundError) else 'error'
                self.assertEqual(value['wal_status'], expected)
                self.assertEqual(value['wal_bytes'], 0 if expected == 'absent' else None)

    def test_snapshots_have_fixed_size_and_no_io(self):
        metrics = LibraryMetrics(2)
        count = len(metrics.snapshot())
        with patch('core.library_wal.os.stat', side_effect=AssertionError('filesystem')), patch(
                'sqlite3.connect', side_effect=AssertionError('SQL')):
            for _ in range(1000):
                started = metrics.start_read()
                metrics.finish_read('completed', started)
                self.assertEqual(len(metrics.snapshot()), count)


class Service(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LibraryService(Path(self.temp.name)/'private.db')
        self.addCleanup(self.service.close)

    def test_initial_owner_sample_and_actual_shutdown_state(self):
        value = self.service.metrics_snapshot()
        self.assertGreaterEqual(value['wal_samples'], 1)
        self.assertTrue(value['wal_observer_active'])
        self.assertIsInstance(value['wal_bytes'], int)
        self.service.close()
        value = self.service.metrics_snapshot()
        self.assertFalse(value['wal_observer_active'])
        self.assertFalse(value['accepting'])
        self.assertTrue(self.service.shutdown_ready())

    def test_sample_is_owner_only_outside_transaction_and_has_no_checkpoint_sql(self):
        observed, sql, connection = [], [], []
        original = self.service._wal.poll
        def tracked():
            observed.append(threading.get_ident())
            if connection:
                self.assertFalse(connection[0].in_transaction)
            return original()
        register = LibraryRepository.register_files_minimal
        def traced(repo, records):
            connection[:] = [repo.conn]
            repo.conn.set_trace_callback(sql.append)
            return register(repo, records)
        with patch.object(self.service._wal, 'poll', tracked), patch.object(
                LibraryRepository, 'register_files_minimal', traced):
            for index in range(3):
                self.assertTrue(self.service.submit('register_files_minimal', [
                    dict(path=f'C:/private/{index}.png', filename=f'{index}.png', size=1, created=1)]).result(3))
            wait(lambda: len(observed) >= 3)
        self.assertEqual(set(observed), {self.service._thread.ident})
        self.assertFalse(any('CHECKPOINT' in statement.upper() for statement in sql))

    def test_pinned_reader_does_not_get_closed_or_checkpointed_by_sampling(self):
        self.assertTrue(self.service.submit('register_files_minimal', [
            dict(path='C:/private/base.png', filename='base.png', size=1, created=1)]).result(3))
        with closing(sqlite3.connect(self.service.db_path, isolation_level=None)) as reader:
            reader.execute('BEGIN')
            self.assertEqual(reader.execute('SELECT count(*) FROM files').fetchone()[0], 1)
            before = self.service.metrics_snapshot()['wal_samples']
            for index in range(10):
                records = [dict(path=f'C:/private/{index}-{row}.png', filename=f'{index}-{row}.png',
                                size=1, created=1) for row in range(100)]
                self.assertTrue(self.service.submit('register_files_minimal', records).result(3))
            wait(lambda: self.service.metrics_snapshot()['wal_samples'] > before)
            size = os.stat(self.service.db_path + '-wal').st_size
            snapshot = self.service.metrics_snapshot()
            self.assertEqual(snapshot['wal_bytes'], size)
            self.assertGreater(size, 0)
            self.assertEqual(reader.execute('SELECT count(*) FROM files').fetchone()[0], 1)
            self.assertEqual(self.service.read('count_files'), 1001)
            reader.rollback()

    def test_slow_sample_does_not_hold_metrics_lock_or_block_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        sampler = self.service._wal
        actual = os.stat
        def slow(path, *args, **kwargs):
            if path == self.service.db_path+'-wal' and threading.get_ident() == self.service._thread.ident:
                entered.set()
                if not release.wait(4):
                    raise RuntimeError('private-stat-timeout')
            return actual(path, *args, **kwargs)
        with patch('core.library_wal.os.stat', slow):
            sampler._next = 0
            try:
                self.assertTrue(entered.wait(3))
                with ThreadPoolExecutor(1) as pool:
                    values = pool.submit(lambda: [self.service.metrics_snapshot() for _ in range(1000)]).result(1)
                    self.assertEqual(len(values), 1000)
                    self.assertTrue(all(value['wal_sample_age_seconds'] >= 0 for value in values))
            finally:
                release.set()

    def test_slow_write_and_read_report_live_occupancy_without_sampling_sql(self):
        entered, release = threading.Event(), threading.Event()
        register = LibraryRepository.register_files_minimal
        def held(repo, records):
            entered.set()
            if not release.wait(4):
                raise RuntimeError('private-writer-timeout')
            return register(repo, records)
        with patch.object(LibraryRepository, 'register_files_minimal', held):
            first = self.service.submit('register_files_minimal', [])
            try:
                self.assertTrue(entered.wait(3))
                second = self.service.submit('register_files_minimal', [])
                value = self.service.metrics_snapshot()
                self.assertGreater(value['write_active_seconds'], 0)
                self.assertGreater(value['queue_busy_seconds'], 0)
                self.assertGreaterEqual(value['wal_sample_age_seconds'], 0)
            finally:
                release.set()
            self.assertTrue(first.result(3))
            self.assertTrue(second.result(3))
        value = self.service.metrics_snapshot()
        self.assertEqual((value['write_active_seconds'], value['queue_busy_seconds']), (0, 0))
        entered.clear()
        release.clear()
        def read_held(repo):
            entered.set()
            if not release.wait(4):
                raise RuntimeError('private-reader-timeout')
            return 0
        with patch.object(LibraryRepository, 'count_files', read_held), ThreadPoolExecutor(1) as pool:
            reading = pool.submit(self.service.read, 'count_files')
            try:
                self.assertTrue(entered.wait(3))
                value = self.service.metrics_snapshot()
                self.assertEqual(value['reads_active'], 1)
                self.assertGreater(value['reads_busy_seconds'], 0)
            finally:
                release.set()
            self.assertEqual(reading.result(3), 0)
        self.assertEqual(self.service.metrics_snapshot()['reads_busy_seconds'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
