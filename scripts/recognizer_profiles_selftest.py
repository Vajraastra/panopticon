"""DB especializada temporal: concurrencia, caché, fallos y entrega Qt sin modelos."""
import os
import sys
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import warnings
from unittest.mock import patch
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
from PySide6.QtCore import QTimer, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from core.paths import ProjectPaths
from modules.character_recognizer.logic.profile_db import ProfileDB
from modules.character_recognizer.logic.profile_service import ProfileService, close_profile_services
from recognizer_data_selftest import RecognizerData
import curation_snapshot_selftest as helpers


@contextmanager
def connection(*args, **kwargs):
    conn = sqlite3.connect(*args, **kwargs)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class ProfileSQL(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'data' / 'profiles.db'
        self.service = ProfileService(self.path)
        self.addCleanup(lambda: self.service.close(wait=True))

    def snapshot(self):
        return self.service.submit('snapshot').result(3)

    def add(self, name='A', embedding=(1., 2., 3.)):
        return self.service.submit('add_reference', name, np.array(embedding)).result(3)

    def query(self, sql):
        with connection(self.path) as conn:
            return conn.execute(sql).fetchall()

    def test_constructor_no_disk_or_connection_owner_thread_only(self):
        self.assertFalse(self.path.parent.exists())
        owner, sql = [], []
        real_connect = sqlite3.connect
        def connect(*args, **kwargs):
            owner.append(threading.get_ident())
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(lambda statement: sql.append((threading.get_ident(), statement)))
            return conn
        with patch('modules.character_recognizer.logic.profile_service.sqlite3.connect', connect):
            self.snapshot()
            self.add()
            self.snapshot()
        self.assertEqual(len(owner), 1)
        self.assertNotEqual(owner[0], threading.get_ident())
        self.assertEqual({tid for tid, _ in sql}, set(owner))

    def test_concurrent_samples_no_lost_update_and_float32_storage(self):
        futures = []
        lock = threading.Lock()
        def producer(i):
            future = self.service.submit('add_reference', 'A', np.array([i + 1., 2.], dtype=np.float64))
            with lock:
                futures.append(future)
        threads = [threading.Thread(target=producer, args=(i,)) for i in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertTrue(all(f.result(3) is True for f in futures))
        self.assertEqual(self.query('SELECT samples_count, length(embedding) FROM characters'), [(40, 8)])
        np.testing.assert_allclose(self.snapshot().profiles[0][1], [20.5, 2.])

    def test_snapshot_cache_one_select_for_1000_reads_and_revisions(self):
        sql = []
        original = self.service._connect
        def connect():
            conn = original()
            conn.set_trace_callback(sql.append)
            return conn
        with patch.object(self.service, '_connect', connect):
            self.add()
            first = self.snapshot()
            sql.clear()
            for _ in range(1000):
                self.assertIs(self.snapshot(), first)
            self.assertFalse(any(s.startswith('SELECT') for s in sql), sql[:5])
            self.add()
            second = self.snapshot()
            self.assertGreater(second.revision, first.revision)
            self.assertEqual(sum(s.startswith('SELECT name, embedding') for s in sql), 1)

    def test_external_commit_invalidates_cache_and_snapshot_is_detached(self):
        self.add()
        first = self.snapshot()
        with connection(self.path) as conn:
            conn.execute('UPDATE characters SET name=?', ('External',))
        second = self.snapshot()
        self.assertGreater(second.revision, first.revision)
        self.assertEqual(first.profiles[0][0], 'A')
        self.assertEqual(second.profiles[0][0], 'External')
        vector = second.profiles[0][1]
        with self.assertRaises(ValueError):
            vector[0] = 0
        with self.assertRaises(ValueError):
            vector.setflags(write=True)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            vector.shape = (1, 3)
        self.assertEqual(second.profiles[0][1].shape, (3,))

    def test_invalid_inputs_fail_before_disk_and_capture_input(self):
        for vector in ([], [0., 0.], [np.nan], [np.inf], [[1., 2.]], ['x'], [1.] * 4097):
            with self.subTest(vector=str(vector)[:30]), self.assertRaises(ValueError):
                self.service.submit('add_reference', 'A', np.array(vector)).result(3)
        for name in ('', ' ', 'x' * 257, None):
            with self.assertRaises(ValueError):
                self.service.submit('add_reference', name, np.array([1.])).result(3)
        with self.assertRaises(ValueError):
            self.service.submit('unknown').result(3)
        self.assertFalse(self.path.exists())
        entered, release = threading.Event(), threading.Event()
        real = self.service._connect
        def blocked():
            entered.set()
            release.wait(3)
            return real()
        vector = np.array([1., 2.])
        with patch.object(self.service, '_connect', blocked):
            future = self.service.submit('add_reference', 'A', vector)
            self.assertTrue(entered.wait(1))
            vector[:] = 9
            release.set()
            self.assertTrue(future.result(3))
        np.testing.assert_array_equal(self.snapshot().profiles[0][1], [1., 2.])

    def test_dimension_mismatch_rollback_and_cache_unchanged(self):
        self.add()
        first = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'dimension_mismatch'):
            self.add(embedding=[1., 2.])
        self.assertIs(self.snapshot(), first)
        self.assertEqual(self.query('SELECT samples_count FROM characters'), [(1,)])

    def test_constraint_failure_rolls_back_and_does_not_publish_revision(self):
        self.add()
        with connection(self.path) as conn:
            conn.execute("CREATE TRIGGER fail_update AFTER UPDATE ON characters BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        first = self.snapshot()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'fixture'):
            self.add(embedding=[4., 5., 6.])
        self.assertIs(self.snapshot(), first)
        self.assertEqual(self.query('SELECT samples_count FROM characters'), [(1,)])

    def test_corrupt_blob_fails_visibly_without_replacing_cache_with_empty(self):
        self.add()
        old = self.snapshot()
        with connection(self.path) as conn:
            conn.execute('UPDATE characters SET embedding=?', (b'bad',))
        with self.assertRaisesRegex(ValueError, 'invalid_stored_embedding'):
            self.snapshot()
        self.assertEqual(len(old.profiles), 1)
        with self.assertRaises(ValueError):
            self.add()
        self.assertEqual(self.query('SELECT embedding, samples_count FROM characters'), [(b'bad', 1)])

    def test_legacy_schema_compatible_without_new_tables(self):
        self.path.parent.mkdir()
        with connection(self.path) as conn:
            conn.execute('CREATE TABLE characters(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE NOT NULL,embedding BLOB NOT NULL,samples_count INTEGER DEFAULT 1)')
            conn.execute('INSERT INTO characters(name,embedding,samples_count) VALUES(?,?,?)', ('Old', np.array([2., 4.], dtype=np.float32).tobytes(), 2))
        self.add('Old', [5., 7.])
        np.testing.assert_array_equal(self.snapshot().profiles[0][1], [3., 5.])
        self.assertEqual(self.query('SELECT samples_count FROM characters'), [(3,)])
        self.assertEqual(self.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"), [('characters',), ('sqlite_sequence',)])

    def test_external_writer_lock_errors_are_confirmed_and_retry_works(self):
        self.add()
        with connection(self.path, isolation_level=None) as conn:
            conn.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                self.add()
            conn.rollback()
        self.assertTrue(self.add())
        self.assertEqual(self.query('SELECT samples_count FROM characters'), [(2,)])

    def test_queue_full_cancel_and_close_drain_accepted(self):
        self.service = ProfileService(self.path, capacity=1)
        entered, release = threading.Event(), threading.Event()
        original = self.service._add_reference
        def blocked(*args):
            entered.set()
            release.wait(3)
            return original(*args)
        with patch.object(self.service, '_add_reference', blocked):
            first = self.service.submit('add_reference', 'A', np.array([1.]))
            self.assertTrue(entered.wait(1))
            second = self.service.submit('add_reference', 'B', np.array([1.]))
            with self.assertRaisesRegex(RuntimeError, 'queue_full'):
                self.service.submit('snapshot').result(3)
            self.assertTrue(second.cancel())
            start = time.monotonic()
            self.service.close()
            self.assertLess(time.monotonic() - start, .1)
            release.set()
            self.assertTrue(first.result(3))
        self.service.close(wait=True)
        self.assertFalse(self.service._thread.is_alive())
        self.assertEqual(self.query('SELECT name FROM characters'), [('A',)])
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            self.service.submit('snapshot').result(3)

    def test_fatal_init_resolves_all_queued_futures(self):
        entered, release = threading.Event(), threading.Event()
        def fail():
            entered.set()
            release.wait(3)
            raise KeyboardInterrupt('init_failure')
        with patch.object(self.service, '_connect', fail):
            first = self.service.submit('snapshot')
            self.assertTrue(entered.wait(1))
            second = self.service.submit('add_reference', 'A', np.array([1.]))
            release.set()
            for future in (first, second):
                with self.assertRaisesRegex(RuntimeError, 'profile_owner_failed: init_failure'):
                    future.result(3)

    def test_transient_init_error_can_retry_without_replacing_service(self):
        with patch.object(self.service, '_connect', side_effect=OSError('temporary')):
            with self.assertRaisesRegex(OSError, 'temporary'):
                self.snapshot()
        self.assertFalse(self.service._closed)
        self.assertEqual(self.snapshot().profiles, ())
        self.assertTrue(self.add())

    def test_two_independent_owners_cannot_lose_average_samples(self):
        other = ProfileService(self.path)
        self.addCleanup(lambda: other.close(wait=True))
        self.snapshot()
        other.submit('snapshot').result(3)
        futures = [service.submit('add_reference', 'A', np.array([float(i + 1), 2.]))
                   for i in range(40) for service in (self.service, other)]
        self.assertTrue(all(f.result(5) for f in futures))
        self.assertEqual(self.query('SELECT samples_count FROM characters'), [(80,)])
        np.testing.assert_allclose(self.snapshot().profiles[0][1], [20.5, 2.])

    def test_thread_start_failure_resolves_future_and_rejects_new_jobs(self):
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('no_thread')):
            with self.assertRaisesRegex(RuntimeError, 'no_thread'):
                self.snapshot()
        self.assertEqual(self.service._queue.qsize(), 0)
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            self.snapshot()

    def test_facades_share_one_owner_without_constructor_io(self):
        self.addCleanup(close_profile_services)
        with patch.object(ProjectPaths, 'root', return_value=Path(self.temp.name)):
            a, b = ProfileDB(), ProfileDB()
        self.assertIs(a.service, b.service)
        self.assertIsNone(a.service._thread)
        self.assertFalse(Path(a.db_path).exists())
        self.assertTrue(a.add_reference('A', np.array([1.])))
        self.assertEqual(b.get_all_profiles()[0][0], 'A')


class ProfileQt(RecognizerData):
    # load_tests sólo ejecuta casos nuevos; los 25 heredados corren en su propia suite.
    def test_initial_profile_read_blocked_qt_responsive_and_text_preserved(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = ProfileService._snapshot
        threads = []
        def blocked(service, conn):
            threads.append(threading.get_ident())
            entered.set()
            release.wait(3)
            return original(service, conn)
        with patch.object(ProfileService, '_snapshot', blocked):
            start = time.monotonic()
            view = self.view()
            self.assertLess(time.monotonic() - start, .5)
            self.assertTrue(entered.wait(1))
            view.combo_names.setEditText('Typing')
            fired = []
            QTimer.singleShot(0, lambda: fired.append(True))
            self.assertTrue(helpers.pump(lambda: bool(fired)))
            for _ in range(50):
                view.refresh_profiles()
            self.assertLessEqual(view.profile_db.service._queue.qsize(), 1)
            release.set()
            self.assertTrue(helpers.pump(lambda: not view._profile_read_pending))
        self.assertEqual(view.combo_names.currentText(), 'Typing')
        self.assertTrue(all(t != threading.get_ident() for t in threads))

    def test_profile_read_error_reload_keeps_names_and_typed_text(self):
        view = self.view()
        self.assertTrue(helpers.pump(lambda: not view._profile_read_pending))
        view.profile_db.add_reference('Known', np.array([1.]))
        view.refresh_profiles()
        self.assertTrue(helpers.pump(lambda: not view._profile_read_pending))
        view.combo_names.setEditText('Typing')
        with patch.object(ProfileService, '_snapshot', side_effect=OSError('fixture')):
            view.btn_retry_profiles.click()
            self.assertTrue(helpers.pump(lambda: not view._profile_read_pending))
        self.assertIn('fixture', view.lbl_profiles.text())
        self.assertEqual(view.combo_names.itemText(0), 'Known')
        self.assertEqual(view.combo_names.currentText(), 'Typing')
        view.btn_retry_profiles.click()
        self.assertTrue(helpers.pump(lambda: not view._profile_read_pending))
        self.assertEqual(view.lbl_profiles.text(), '')

    def manual(self):
        self.register(self.a, self.b)
        view = self.view()
        self.image(view, self.a, None, np.array([1., 2.], dtype=np.float32))
        self.ready(view)
        view.combo_names.setEditText('Manual')
        return view

    def test_reference_write_blocked_no_gui_wait_or_advance_before_commit(self):
        view = self.manual()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = ProfileService._add_reference
        def blocked(*args):
            entered.set()
            release.wait(3)
            return original(*args)
        with patch.object(ProfileService, '_add_reference', blocked):
            view.on_manual_save()
            self.assertTrue(helpers.pump(entered.is_set))
            self.assertEqual(view.worker.next_count, 0)
            self.assertFalse(view.btn_manual_save.isEnabled())
            self.assertIn('Learning', view.lbl_status.text())
            self.assertEqual(self.service.read('get_curation_snapshot', (self.a,))[0]['tags'], ('manual',))
            fired = []
            QTimer.singleShot(0, lambda: fired.append(True))
            self.assertTrue(helpers.pump(lambda: bool(fired)))
            view.on_manual_save()
            release.set()
            self.settled(view)
        self.assertEqual(view.worker.next_count, 1)
        with connection(view.profile_db.db_path) as conn:
            self.assertEqual(conn.execute('SELECT samples_count FROM characters').fetchall(), [(1,)])

    def test_reference_failure_partial_status_retry_only_unlearned_reference(self):
        view = self.manual()
        with patch.object(ProfileService, '_add_reference', side_effect=OSError('fixture')):
            view.on_manual_save()
            self.settled(view)
        self.assertEqual(view.worker.next_count, 0)
        self.assertIn('Tag saved, but reference failed', view.lbl_status.text())
        self.assertEqual(view.profile_db.get_all_profiles(), [])
        self.assertEqual(self.service.read('get_curation_snapshot', (self.a,))[0]['tags'], ('manual',))
        view.on_manual_save()
        self.settled(view)
        self.assertEqual(view.worker.next_count, 1)
        self.assertIn('Tag and reference saved', view.lbl_status.text())

    def test_new_image_during_reference_does_not_advance_new_worker(self):
        view = self.manual()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = ProfileService._add_reference
        def blocked(*args):
            entered.set()
            release.wait(3)
            return original(*args)
        with patch.object(ProfileService, '_add_reference', blocked):
            view.on_manual_save()
            self.assertTrue(helpers.pump(entered.is_set))
            from recognizer_data_selftest import PausedWorker
            newer = view.worker = PausedWorker((self.b,))
            self.image(view, self.b)
            release.set()
            self.settled(view)
        self.assertEqual(newer.next_count, 0)
        self.assertEqual(view.current_path, self.b)
        self.assertEqual(view.profile_db.get_all_profiles()[0][0], 'Manual')

    def test_destroy_owner_accepted_reference_commits_no_late_gui_delivery(self):
        view = self.manual()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = ProfileService._add_reference
        def blocked(*args):
            entered.set()
            release.wait(3)
            return original(*args)
        service = view.profile_db.service
        with patch.object(ProfileService, '_add_reference', blocked):
            view.on_manual_save()
            self.assertTrue(helpers.pump(entered.is_set))
            self.views.remove(view)
            view.shutdown()
            view.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            release.set()
            snapshot = service.submit('snapshot').result(3)
            QApplication.processEvents()
        self.assertEqual(snapshot.profiles[0][0], 'Manual')

    def test_worker_profile_failure_terminal_once_and_error_not_replaced_by_done(self):
        from modules.character_recognizer.logic.thread_worker import RecognitionWorker, AutoScanWorker
        class Engine:
            def initialize(self):
                pass
        with patch('modules.character_recognizer.logic.thread_worker.RecognitionEngine', Engine), \
                patch.object(ProfileDB, 'get_all_profiles', side_effect=OSError('profile_failure')):
            for cls in (RecognitionWorker, AutoScanWorker):
                worker = cls([self.a], mode='illustration')
                if cls is RecognitionWorker:
                    worker.request_next()
                errors, finished = [], []
                worker.error.connect(errors.append)
                worker.finished_signal.connect(lambda: finished.append(True))
                worker.start()
                self.assertTrue(worker.wait(3000))
                QApplication.processEvents()
                self.assertEqual(errors, ['profile_failure'])
                self.assertEqual(finished, [True])
        view = self.view()
        view.on_engine_error('profile_failure')
        view.on_finished()
        self.assertIn('profile_failure', view.lbl_status.text())

    def test_worker_sees_confirmed_profile_between_images_without_full_reload(self):
        from modules.character_recognizer.logic.thread_worker import RecognitionWorker
        class Engine:
            def initialize(self):
                pass
            def analyze_image(self, *args, **kwargs):
                return np.array([1., 2.]), None, .9
            def compare(self, *args):
                return .9
        with patch('modules.character_recognizer.logic.thread_worker.RecognitionEngine', Engine):
            worker = RecognitionWorker([self.a, self.b], mode='illustration')
            rows = []
            worker.image_processed.connect(lambda *args: rows.append(args))
            worker.request_next()
            worker.start()
            try:
                self.assertTrue(helpers.pump(lambda: len(rows) == 1))
                self.assertTrue(helpers.pump(lambda: worker.paused))
                worker.db.add_reference('Learned', np.array([1., 2.]))
                worker.request_next()
                self.assertTrue(helpers.pump(lambda: len(rows) == 2))
                self.assertEqual(rows[1][3], 'Learned')
            finally:
                worker.is_running = False
                self.assertTrue(worker.wait(3000))

    def test_headless_uses_one_snapshot_per_batch_with_fake_engine(self):
        from modules.character_recognizer.module import CharacterRecognizerModule
        db = ProfileDB()
        db.add_reference('Headless', np.array([1., 2.]))
        class Engine:
            def initialize(self):
                pass
            def analyze_image(self, *args):
                return np.array([1., 2.]), None, .9
            def compare(self, *args):
                return .9
        original = ProfileDB.get_all_profiles
        calls = []
        def snapshot(owner):
            calls.append(True)
            return original(owner)
        with patch('modules.character_recognizer.logic.recognition_engine.RecognitionEngine', Engine), \
                patch.object(ProfileDB, 'get_all_profiles', snapshot):
            result = CharacterRecognizerModule().run_headless([self.a, self.b], mode='illustration')
        self.assertEqual(result, {self.a: 'Headless', self.b: 'Headless'})
        self.assertEqual(len(calls), 1)

    def test_new_profile_texts_bilingual_and_qt_delivery_thread(self):
        import json
        root = Path(__file__).resolve().parents[1]
        for language in ('en', 'es'):
            texts = json.loads((root / 'locales' / (language + '.json')).read_text(encoding='utf-8'))
            for key in ('reload_profiles', 'reload_profiles_tip', 'profiles_loading', 'profiles_error',
                        'reference_saving', 'reference_error', 'reference_saved'):
                self.assertTrue(texts['tool.character_recognizer.' + key].format(reason='fixture', name='A'))
        view = self.view(mode='ccip')
        received = []
        view._profile_reader.result.connect(lambda *args: received.append(threading.get_ident()))
        self.assertTrue(helpers.pump(lambda: not view._profile_read_pending))
        self.assertEqual(received, [threading.get_ident()])
        self.assertTrue(view.btn_retry_profiles.toolTip())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.TestSuite(cls(name) for name in cls.__dict__ if name.startswith('test_'))
        for cls in (ProfileSQL, ProfileQt))


if __name__ == '__main__':
    unittest.main(verbosity=2)
