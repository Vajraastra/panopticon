"""Bounded FIFO admission/cancellation on private Windows Native databases."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.database_manager import DatabaseManager
from core.inventory import InventoryResult, InventoryStatus
from core.library_batch import BatchCancelled, MAX_BATCH_ROWS, commit_batch
from core.library_repository import LibraryRepository
from core.library_service import LibraryError, LibraryQueueFull, LibraryService
from modules.librarian.logic.hash_backfill import HashBackfillWorker
from modules.librarian.logic.indexer import IndexerWorker


def wait(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.002)
    raise AssertionError('private fixture did not reach expected state')


class Batches(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LibraryService(Path(self.temp.name) / 'private.db', capacity=1)
        self.addCleanup(self.service.close)
        self.db = DatabaseManager(service=self.service)
        self.cancel = threading.Event()
        self.record = dict(path=(Path(self.temp.name) / 'one.png').as_posix(),
                           filename='one.png', size=1, created=1)

    @contextmanager
    def held(self):
        entered, release = threading.Event(), threading.Event()
        original = LibraryRepository.register_files_minimal

        def block(repo, records):
            if not records:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('private_writer_hold_timeout')
            return original(repo, records)

        with patch.object(LibraryRepository, 'register_files_minimal', block):
            first = self.service.submit('register_files_minimal', [])
            try:
                self.assertTrue(entered.wait(3))
                yield release
            finally:
                release.set()
                first.result(3)

    def test_full_queue_waits_then_confirms_once(self):
        with self.held() as release, ThreadPoolExecutor(1) as pool:
            queued = self.service.submit('register_files_minimal', [self.record])
            with patch.object(self.service, 'submit', wraps=self.service.submit) as submitted:
                job = pool.submit(commit_batch, self.service, 'register_files_minimal',
                                  [dict(self.record, size=7)], self.cancel)
                try:
                    wait(lambda: self.service.metrics_snapshot()['rejected_full'] >= 1)
                    self.assertFalse(job.done())
                    self.assertEqual(self.service.metrics_snapshot()['accepted'], 2)
                    release.set()
                    self.assertTrue(queued.result(3))
                    self.assertTrue(job.result(3))
                    self.assertGreaterEqual(submitted.call_count, 2)
                finally:
                    release.set()
            self.assertEqual(self.service.metrics_snapshot()['accepted'], 3)
            self.assertEqual(self.service.read('count_files'), 1)

    def test_stop_while_full_never_accepts_the_waiting_batch(self):
        with self.held() as release, ThreadPoolExecutor(1) as pool:
            queued = self.service.submit('register_files_minimal', [self.record])
            job = pool.submit(commit_batch, self.service, 'register_files_minimal',
                              [dict(self.record, filename='not-accepted.png')], self.cancel)
            try:
                wait(lambda: self.service.metrics_snapshot()['rejected_full'] >= 1)
                self.cancel.set()
                with self.assertRaises(BatchCancelled):
                    job.result(3)
                self.assertEqual(self.service.metrics_snapshot()['accepted'], 2)
            finally:
                release.set()
                queued.result(3)

    def test_stop_cancels_accepted_but_not_started_batch(self):
        with self.held() as release, ThreadPoolExecutor(1) as pool:
            job = pool.submit(commit_batch, self.service, 'register_files_minimal',
                              [self.record], self.cancel)
            try:
                wait(lambda: self.service.metrics_snapshot()['queued'] == 1)
                self.cancel.set()
                with self.assertRaises(BatchCancelled):
                    job.result(3)
            finally:
                release.set()
        wait(lambda: self.service.metrics_snapshot()['cancelled'] == 1)
        self.assertEqual(self.service.read('count_files'), 0)

    def test_stop_waits_for_running_commit_and_returns_confirmation(self):
        entered, release = threading.Event(), threading.Event()
        original = LibraryRepository.register_files_minimal

        def slow(repo, records):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('private_writer_hold_timeout')
            return original(repo, records)

        with patch.object(LibraryRepository, 'register_files_minimal', slow), ThreadPoolExecutor(1) as pool:
            job = pool.submit(commit_batch, self.service, 'register_files_minimal', [self.record], self.cancel)
            try:
                self.assertTrue(entered.wait(3))
                self.cancel.set()
                self.assertFalse(job.done())
                release.set()
                self.assertTrue(job.result(3))
            finally:
                release.set()
        self.assertEqual(self.service.read('count_files'), 1)

    def test_accepted_failure_named_queue_full_is_never_retried(self):
        with patch.object(LibraryRepository, 'register_files_minimal', side_effect=ValueError('queue_full')) as command:
            with self.assertRaises(LibraryError) as caught:
                commit_batch(self.service, 'register_files_minimal', [self.record], self.cancel)
            self.assertNotIsInstance(caught.exception, LibraryQueueFull)
            self.assertEqual(command.call_count, 1)
        self.assertEqual(self.service.read('count_files'), 0)

    def test_closed_payload_and_sql_failures_are_not_retried(self):
        with patch.object(self.service, 'submit', wraps=self.service.submit) as submit:
            with self.assertRaises(LibraryError):
                commit_batch(self.service, 'register_files_minimal', [dict(self.record, filename='x'*1100000)], self.cancel)
            self.assertEqual(submit.call_count, 1)
        with patch.object(LibraryRepository, 'register_files_minimal', side_effect=RuntimeError('private-failure')):
            with patch.object(self.service, 'submit', wraps=self.service.submit) as submit:
                with self.assertRaises(LibraryError):
                    commit_batch(self.service, 'register_files_minimal', [self.record], self.cancel)
                self.assertEqual(submit.call_count, 1)
        self.service.close()
        with patch.object(self.service, 'submit', wraps=self.service.submit) as submit:
            with self.assertRaisesRegex(LibraryError, 'service_closed'):
                commit_batch(self.service, 'register_files_minimal', [self.record], self.cancel)
            self.assertEqual(submit.call_count, 1)

    def test_limits_and_precancel_do_not_submit(self):
        with patch.object(self.service, 'submit', side_effect=AssertionError('unexpected admission')):
            for operation, records in [('edit_curation', []), ('register_files_minimal', [self.record]*101),
                                       ('register_files_minimal', iter([self.record]))]:
                with self.assertRaises(ValueError):
                    commit_batch(self.service, operation, records, self.cancel)
            self.assertTrue(commit_batch(self.service, 'register_files_minimal', [], self.cancel))
            self.cancel.set()
            with self.assertRaises(BatchCancelled):
                commit_batch(self.service, 'register_files_minimal', [self.record], self.cancel)

    def test_completion_racing_wait_timeout_returns_the_settled_result(self):
        class Race:
            def cancel(self):
                return False
            def done(self):
                return True
            def result(self, timeout=None):
                if timeout is not None:
                    raise TimeoutError()
                return True
        fake = SimpleNamespace(submit=lambda *args: Race())
        self.assertTrue(commit_batch(fake, 'register_files_minimal', [self.record], self.cancel))

    def test_legacy_atomic_facade_is_not_silently_split_or_capped(self):
        records = [dict(self.record, path=(Path(self.temp.name)/f'{index}.png').as_posix(),
                        filename=f'{index}.png') for index in range(205)]
        with patch.object(self.service, 'submit', wraps=self.service.submit) as submit:
            self.assertTrue(self.db.register_files_minimal(records))
            self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.db.count_files(), 205)

    def test_interactive_command_stays_fifo_between_producer_batches(self):
        entered, release = threading.Event(), threading.Event()
        original = LibraryRepository.register_files_minimal
        order = []

        def register(repo, records):
            order.append(records[0]['filename'])
            if len(order) == 1:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('private_writer_hold_timeout')
            return original(repo, records)

        original_edit = LibraryRepository.edit_curation
        def edit(repo, *args, **kwargs):
            order.append('interactive')
            return original_edit(repo, *args, **kwargs)

        def producer():
            for name in ('first.png', 'second.png'):
                record = dict(self.record, filename=name)
                self.assertTrue(commit_batch(self.service, 'register_files_minimal', [record], self.cancel))

        with patch.object(LibraryRepository, 'register_files_minimal', register), patch.object(
                LibraryRepository, 'edit_curation', edit), ThreadPoolExecutor(1) as pool:
            job = pool.submit(producer)
            try:
                self.assertTrue(entered.wait(3))
                rating = self.service.submit('edit_curation', self.record['path'], rating=5)
                release.set()
                self.assertTrue(rating.result(3))
                job.result(3)
            finally:
                release.set()
        self.assertEqual(order, ['first.png', 'interactive', 'second.png'])
        self.assertEqual(self.service.read('get_file_rating', self.record['path']), 5)

    def hash_records(self, count):
        records = [dict(self.record, path=(Path(self.temp.name) / f'{index}.png').as_posix(),
                        filename=f'{index}.png') for index in range(count)]
        for start in range(0, len(records), 100):
            self.assertTrue(self.db.register_files_minimal(records[start:start+100]))
        return records

    def test_backfill_splits_hash_read_page_into_confirmed_hundred_row_writes(self):
        self.hash_records(205)
        worker = HashBackfillWorker(self.db, batch_size=205)
        sizes, finished = [], []
        original = self.db.set_hash_original_bulk
        def record(values, **kwargs):
            sizes.append(len(values))
            return original(values, **kwargs)
        worker.finished_signal.connect(lambda done, failed: finished.append((done, failed)))
        with patch('modules.librarian.logic.hash_backfill.sha256_file', return_value='a'*64), patch.object(
                self.db, 'set_hash_original_bulk', record):
            worker.run()
        self.assertEqual(sizes, [100, 100, 5])
        self.assertEqual(finished, [(205, 0)])
        self.assertEqual(self.db.count_files_missing_hash(), 0)

    def test_backfill_stop_after_first_commit_keeps_confirmed_count_and_remaining_hashes(self):
        self.hash_records(205)
        worker = HashBackfillWorker(self.db, batch_size=205)
        finished = []
        worker.finished_signal.connect(lambda done, failed: finished.append((done, failed)))
        worker.count_signal.connect(lambda *args: worker.stop())
        with patch('modules.librarian.logic.hash_backfill.sha256_file', return_value='a'*64):
            worker.run()
        self.assertEqual(worker.outcome, InventoryStatus.CANCELLED)
        self.assertEqual(finished, [(100, 0)])
        self.assertEqual(self.db.count_files_missing_hash(), 105)

    def test_backfill_later_failure_preserves_confirmed_count(self):
        self.hash_records(205)
        worker = HashBackfillWorker(self.db, batch_size=205)
        original, calls, finished = self.db.set_hash_original_bulk, [], []
        def second(values, **kwargs):
            calls.append(len(values))
            return original(values, **kwargs) if len(calls) == 1 else False
        worker.finished_signal.connect(lambda done, failed: finished.append((done, failed)))
        with patch('modules.librarian.logic.hash_backfill.sha256_file', return_value='a'*64), patch.object(
                self.db, 'set_hash_original_bulk', second):
            worker.run()
        self.assertEqual(calls, [100, 100])
        self.assertEqual(finished, [(100, 105)])
        self.assertEqual(worker.outcome, InventoryStatus.ERROR)
        self.assertEqual(self.db.count_files_missing_hash(), 105)

    def test_indexer_caps_configured_batches_without_approving_a_prefix(self):
        records = [dict(self.record, path=(Path(self.temp.name)/f'{index}.png').as_posix(),
                        filename=f'{index}.png') for index in range(205)]
        worker = IndexerWorker(self.db, [self.temp.name])
        worker.batch_size = 1000
        sizes = []
        original = self.db.register_files_minimal
        def record(values, **kwargs):
            sizes.append(len(values))
            return original(values, **kwargs)
        with patch.object(worker, '_discover_tree', return_value=InventoryResult(files=records)), patch.object(
                self.db, 'register_files_minimal', record):
            worker.run()
        self.assertEqual(sizes, [100, 100, 5])
        self.assertEqual(worker.outcome, InventoryStatus.COMPLETE)
        self.assertEqual(self.db.count_files(), 205)


if __name__ == '__main__':
    unittest.main(verbosity=2)
