"""Terminal/cancelación/progreso QThread reales; DB e imágenes de juguete."""
from dataclasses import asdict
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer, QObject, Slot
from PySide6.QtWidgets import QApplication
from modules.duplicate_finder.logic.review_worker import ExactReviewWorker
from modules.duplicate_finder.logic.exact_review import ExactDuplicateReview, ReviewLimits
from core.duplicate_policy import DuplicatePolicy
import curation_snapshot_selftest as helpers
import duplicate_exact_review_selftest as prior


class Receiver(QObject):
    def __init__(self):
        super().__init__()
        self.values = []
        self.threads = []

    @Slot(dict)
    def receive(self, result):
        self.threads.append(threading.get_ident())
        self.values.append(result)


class WorkerQt(prior.Review):
    def setUp(self):
        super().setUp()
        self.workers = []
        self.releases = []
        self.addCleanup(self.shutdown)

    def shutdown(self):
        for event in self.releases:
            event.set()
        for worker, _ in self.workers:
            worker.stop()
            self.assertTrue(worker.wait(3000))  # Sólo fixture después de liberar I/O.
        QApplication.processEvents()

    def worker(self, **kwargs):
        worker = ExactReviewWorker(str(self.root), self.service, **kwargs)
        receiver = Receiver()
        worker.finished_signal.connect(receiver.receive)
        self.workers.append((worker, receiver))
        return worker, receiver

    def completed(self, worker, receiver):
        self.assertTrue(helpers.pump(lambda: not worker.isRunning() and receiver.values))
        self.assertEqual(len(receiver.values), 1)
        self.assertEqual(receiver.threads, [threading.get_ident()])
        return receiver.values[0]

    def test_result_and_native_finished_are_distinct_and_delivered_once(self):
        raw, curated = self.pair()
        worker, receiver = self.worker(token=19)
        native = []
        worker.finished.connect(lambda: native.append(True))
        worker.start()
        value = self.completed(worker, receiver)
        self.assertTrue(helpers.pump(lambda: native))
        self.assertEqual(native, [True])
        self.assertEqual((value['token'], value['folder'], value['mode']), (19, str(self.root), 'hash'))
        self.assertEqual(self.report(value)['keep'], (curated.as_posix(),))
        self.assertTrue(raw.is_file())

    def test_unexpected_scan_error_still_emits_one_failed_terminal(self):
        worker, receiver = self.worker()
        with patch.object(ExactDuplicateReview, 'scan', side_effect=RuntimeError('fixture-scan-error')):
            worker.start()
            value = self.completed(worker, receiver)
        self.assertEqual((value['error'], value['complete'], value['cancelled']), ('fixture-scan-error', False, False))
        self.assertEqual((value['groups'], value['proofs']), ({}, {}))

    def test_inventory_limit_emits_error_instead_of_partial_discard(self):
        self.pair()
        worker, receiver = self.worker(limits=ReviewLimits(files=1))
        worker.start()
        value = self.completed(worker, receiver)
        self.assertEqual(value['error'], 'duplicate_review_inventory_limit')
        self.assertEqual(value['reports'], {})

    def test_pre_start_stop_is_cancelled_without_inventory(self):
        worker, receiver = self.worker()
        worker.stop()
        with patch('os.walk', side_effect=AssertionError('inventory')):
            worker.start()
            value = self.completed(worker, receiver)
        self.assertTrue(value['cancelled'])
        self.assertIsNone(value['error'])

    def test_stop_responsive_and_qt_heartbeat_while_hash_is_busy(self):
        self.pair()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        from modules.duplicate_finder.logic.exact_review import _probe
        def blocked(path, event):
            entered.set()
            self.assertTrue(release.wait(3))
            return _probe(path, event)
        worker, receiver = self.worker()
        with patch('modules.duplicate_finder.logic.exact_review._probe', blocked):
            worker.start()
            self.assertTrue(entered.wait(1))
            ticks = []
            QTimer.singleShot(0, lambda: ticks.append(True))
            self.assertTrue(helpers.pump(lambda: ticks))
            started = time.monotonic()
            worker.stop()
            self.assertLess(time.monotonic() - started, .1)
            self.assertTrue(worker.isRunning())
            release.set()
            value = self.completed(worker, receiver)
        self.assertTrue(value['cancelled'])
        self.assertEqual(value['reports'], {})

    def test_cancelled_central_read_cannot_publish_older_results(self):
        self.pair()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        read = self.service.read
        def blocked(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), threading.main_thread().ident)
            entered.set()
            self.assertTrue(release.wait(3))
            return read(*args, **kwargs)
        worker, receiver = self.worker()
        with patch.object(self.service, 'read', blocked):
            worker.start()
            self.assertTrue(entered.wait(1))
            worker.stop()
            release.set()
            value = self.completed(worker, receiver)
        self.assertTrue(value['cancelled'])
        self.assertFalse(value['complete'])

    def test_weight_and_limits_capture_cannot_be_redirected_by_caller(self):
        self.pair()
        policy = DuplicatePolicy(rating_weight=33)
        limits = ReviewLimits(files=10)
        worker, receiver = self.worker(policy=policy, limits=limits)
        object.__setattr__(policy, 'rating_weight', 1000)
        object.__setattr__(limits, 'files', 1)
        worker.start()
        value = self.completed(worker, receiver)
        self.assertEqual(value['policy']['rating_weight'], 33)
        self.assertEqual(worker.limits.files, 10)
        self.assertTrue(value['complete'])

    def test_native_phases_show_inventory_hash_and_curation(self):
        self.pair()
        worker, receiver = self.worker()
        phases = []
        worker.phase.connect(phases.append)
        worker.start()
        self.completed(worker, receiver)
        self.assertEqual(phases[0], 'inventory')
        self.assertIn('hash', phases)
        self.assertIn('curation', phases)

    def test_progress_does_not_flood_qt_and_always_delivers_terminal_hash_count(self):
        worker, receiver = self.worker()
        progress = []
        worker.progress.connect(lambda *args: progress.append(args))
        def simulated(scan, folder, callback, cancel):
            for value in range(1, 10001):
                callback(value, 10000, 'fixture.png')
            return dict(groups={}, reports={}, proofs={}, issues=[], issue_count=0, complete=True)
        with patch.object(ExactDuplicateReview, 'scan', simulated):
            worker.start()
            self.completed(worker, receiver)
        self.assertLess(len(progress), 100)
        self.assertEqual(progress[-1][:2], (10000, 10000))

    def test_invalid_token_configuration_rejected_before_start(self):
        for value in (True, 0, -1, 2**63):
            with self.assertRaises(ValueError):
                ExactReviewWorker(str(self.root), self.service, token=value)
        with self.assertRaises(ValueError):
            ExactReviewWorker(str(self.root), self.service, policy={})


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    # Load only new Qt cases, without inheriting the scanner suite again.
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('WorkerQt.' + name, sys.modules[__name__])
        for name in WorkerQt.__dict__ if name.startswith('test_'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
