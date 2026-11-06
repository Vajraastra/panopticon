"""Curación del reconocedor con DB/Qt temporales; sin modelos, GPU ni biblioteca real."""
import os
import sys
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
import curation_snapshot_selftest as helpers
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QPushButton, QComboBox, QListWidget
from core.library_repository import LibraryRepository
from core.library_service import LibraryError
from core.paths import ProjectPaths
from modules.character_recognizer.ui.recognition_view import CharacterRecognitionView


class PausedWorker:
    def __init__(self, paths=()):
        self.paths = list(paths)
        self.next_count = self.back_count = 0
        self.is_running = True

    def pause(self):
        pass

    def request_next(self):
        self.next_count += 1

    def go_back(self):
        self.back_count += 1

    def isRunning(self):
        return False


class RecognizerData(helpers.Fixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.views = []
        self.addCleanup(self.close_views)
        self.pather = patch.object(ProjectPaths, 'root', return_value=Path(self.temp.name))
        self.pather.start()
        self.addCleanup(self.pather.stop)
        self.a = str(Path(self.temp.name) / 'a.png')
        self.b = str(Path(self.temp.name) / 'b.png')
        from PIL import Image
        for path in (self.a, self.b):
            Image.new('RGB', (12, 10), 'red').save(path)

    def close_views(self):
        for view in self.views:
            view.shutdown()
            thread = view._reader._thread
            if thread:
                thread.join(3)
                self.assertFalse(thread.is_alive())
            for worker in (view.worker, view.auto_worker):
                if worker and hasattr(worker, 'wait'):
                    self.assertTrue(worker.wait(3000))
            view.close()
            view.deleteLater()
        QApplication.processEvents()
        from modules.character_recognizer.logic.profile_service import close_profile_services
        close_profile_services()

    def view(self, paths=None, *, mode='illustration'):
        view = CharacterRecognitionView(dict(data_service=self.service))
        # These fixtures use legacy profile vectors and ArcFace engine doubles.
        view.mode_picker.setCurrentIndex(view.mode_picker.findData(mode))
        view.worker = PausedWorker(paths or (self.a, self.b))
        from modules.character_recognizer.logic.copy_bindings import normalized
        view._fixture_ids={normalized(path):self.service.read('get_file_id',path) for path in view.worker.paths}
        self.views.append(view)
        return view

    def image(self, view, path, name='Alpha', embedding=None):
        from modules.character_recognizer.logic.copy_bindings import normalized
        view.on_image_processed(path, np.zeros((10, 12, 3), dtype=np.uint8),
            embedding, name, None, .9,dict(path=normalized(path),id=view._fixture_ids.get(normalized(path))))

    def ready(self, view):
        self.assertTrue(helpers.pump(lambda: view._curation_ready))

    def settled(self, view):
        self.assertTrue(helpers.pump(lambda: view._pending_edit is None and view._curation_ready))

    def tags(self, view):
        return [view.list_tags.item(i).text() for i in range(view.list_tags.count())]

    def blocked(self, method):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = getattr(LibraryRepository, method)
        def block(repo, *args, **kwargs):
            entered.set()
            release.wait(3)
            return original(repo, *args, **kwargs)
        return entered, release, patch.object(LibraryRepository, method, block)

    def test_read_revision_is_three_selects_off_gui_and_delivery_on_qt(self):
        self.register(self.a)
        self.service.submit('edit_curation', self.a, add_tags=['beta'], rating=3).result(3)
        original = LibraryRepository.get_curation_snapshot
        sql, threads, received, connections = [], [], [], []
        def read(repo, *args, **kwargs):
            threads.append(threading.get_ident())
            connections.append(repo.conn)
            repo.conn.set_trace_callback(sql.append)
            return original(repo, *args, **kwargs)
        service_read = self.service.read
        closed = []
        def after_read(*args, **kwargs):
            rows = service_read(*args, **kwargs)
            import sqlite3
            with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed'):
                connections[-1].execute('SELECT 1')
            closed.append(threading.get_ident())
            return rows
        view = self.view()
        view._reader.result.connect(lambda *args: received.append(threading.get_ident()))
        with patch.object(LibraryRepository, 'get_curation_snapshot', read), \
                patch.object(self.service, 'read', after_read):
            self.image(view, self.a)
            self.ready(view)
        self.assertEqual(self.tags(view), ['beta'])
        self.assertEqual(sum(s.startswith('SELECT') for s in sql), 3)
        self.assertNotEqual(threads, [threading.get_ident()])
        self.assertEqual(received, [threading.get_ident()])
        self.assertEqual(closed, threads)

    def test_blocked_read_responsive_latest_path_only(self):
        self.register(self.a, self.b)
        self.service.submit('edit_curation', self.b, add_tags=['new']).result(3)
        view = self.view()
        entered, release, blocker = self.blocked('get_curation_snapshot')
        with blocker:
            start = time.monotonic()
            self.image(view, self.a)
            self.assertLess(time.monotonic() - start, .3)
            self.assertTrue(entered.wait(1))
            fired = []
            QTimer.singleShot(0, lambda: fired.append(True))
            self.assertTrue(helpers.pump(lambda: bool(fired)))
            self.assertFalse(view.btn_confirm.isEnabled())
            self.image(view, self.b)
            release.set()
            self.ready(view)
        self.assertEqual(view.current_path, self.b)
        self.assertEqual(self.tags(view), ['new'])

    def test_read_error_visible_and_reload_recovers(self):
        self.register(self.a)
        view = self.view()
        with patch.object(LibraryRepository, 'get_curation_snapshot', side_effect=RuntimeError()):
            self.image(view, self.a)
            self.assertTrue(helpers.pump(lambda: 'RuntimeError' in ' '.join(self.tags(view))))
            self.assertFalse(view.btn_confirm.isEnabled())
            view.on_confirm()
            self.assertIsNone(view._pending_edit)
        view.btn_retry_tags.click()
        self.ready(view)
        self.assertEqual(self.tags(view), ['(No tags)'])

    def test_missing_is_read_only_and_never_registers(self):
        view = self.view()
        self.image(view, self.a)
        self.assertTrue(helpers.pump(lambda: 'Index this image' in ' '.join(self.tags(view))))
        view.on_confirm()
        view.on_manual_save()
        self.assertEqual(self.service.read('count_files'), 0)
        self.assertFalse(view._curation_ready)

    def test_incomplete_snapshot_is_error_not_empty_tags(self):
        view = self.view()
        with patch.object(LibraryRepository, 'get_curation_snapshot', return_value=[]):
            self.image(view, self.a)
            self.assertTrue(helpers.pump(lambda: 'Incomplete' in ' '.join(self.tags(view))))
        self.assertFalse(view._curation_ready)

    def test_confirm_waits_for_commit_blocks_double_start_and_navigation(self):
        self.register(self.a)
        view = self.view()
        self.image(view, self.a)
        self.ready(view)
        worker = view.worker
        entered, release, blocker = self.blocked('edit_curation')
        with blocker:
            start = time.monotonic()
            view.on_confirm()
            self.assertLess(time.monotonic() - start, .1)
            self.assertTrue(entered.wait(1))
            ticket = view._pending_edit['ticket']
            view.on_confirm()
            view.on_skip()
            view.on_return()
            view.on_manual_save()
            self.assertEqual(view._pending_edit['ticket'], ticket)
            self.assertEqual((worker.next_count, worker.back_count), (0, 0))
            self.assertFalse(view.btn_confirm.isEnabled())
            fired = []
            QTimer.singleShot(0, lambda: fired.append(True))
            self.assertTrue(helpers.pump(lambda: bool(fired)))
            self.assertEqual(self.service.read('get_curation_snapshot', (self.a,))[0]['tags'], ())
            release.set()
            self.settled(view)
        self.assertEqual(worker.next_count, 1)
        self.assertEqual(self.tags(view), ['alpha'])

    def test_failed_write_no_advance_no_success_then_retry(self):
        self.register(self.a)
        view = self.view()
        self.image(view, self.a)
        self.ready(view)
        with patch.object(LibraryRepository, 'edit_curation', side_effect=RuntimeError()):
            view.on_confirm()
            self.settled(view)
        self.assertIn('Cannot save tags', view.lbl_status.text())
        self.assertNotIn('Tagged:', view.lbl_status.text())
        self.assertEqual(view.worker.next_count, 0)
        self.assertEqual(self.tags(view), ['(No tags)'])
        view.on_confirm()
        self.settled(view)
        self.assertEqual(view.worker.next_count, 1)

    def test_false_commit_is_error(self):
        self.register(self.a)
        view = self.view()
        self.image(view, self.a)
        self.ready(view)
        with patch.object(LibraryRepository, 'edit_curation', return_value=False):
            view.on_confirm()
            self.settled(view)
        self.assertIn('operation_failed', view.lbl_status.text())
        self.assertEqual(view.worker.next_count, 0)

    def test_rollback_after_fts_write_never_announces_success(self):
        self.register(self.a)
        self.service.submit('edit_curation', self.a, add_tags=['old'], rating=2).result(3)
        view = self.view()
        self.image(view, self.a)
        self.ready(view)
        sync = LibraryRepository._fts_sync_file
        def fail(repo, *args):
            sync(repo, *args)
            raise OSError('after FTS')
        with patch.object(LibraryRepository, '_fts_sync_file', fail):
            view.on_confirm()
            self.settled(view)
        self.assertIn('after FTS', view.lbl_status.text())
        self.assertEqual(view.worker.next_count, 0)
        self.assertEqual(self.tags(view), ['old'])
        row = self.service.read('get_curation_snapshot', (self.a,))[0]
        self.assertEqual((row['tags'], row['rating']), (('old',), 2))

    def test_bulk_replacing_input_keeps_accepted_item_and_abandons_old_tail(self):
        self.register(self.a, self.b)
        view = self.view()
        self.bulk(view, (self.a, self.b))
        entered, release, blocker = self.blocked('edit_curation')
        with blocker, patch.object(view, '_start_recognition'):
            view.on_tag_all()
            self.assertTrue(entered.wait(1))
            # Simulate a new input through the public API while the first write runs.
            view.worker = None
            view.load_images((self.b,))
            release.set()
            self.assertTrue(helpers.pump(lambda: view._pending_edit is None))
        rows = self.service.read('get_curation_snapshot', (self.a, self.b))
        self.assertEqual([r['tags'] for r in rows], [('bulk',), ()])
        self.assertEqual(view._bulk_remaining, ())
        self.assertEqual(view._queued_paths, [self.b])
        self.assertNotIn('All tagged', view.lbl_auto_status.text())

    def test_destroyed_owner_does_not_cancel_accepted_curation(self):
        self.register(self.a)
        view = self.view()
        self.image(view, self.a)
        self.ready(view)
        entered, release, blocker = self.blocked('edit_curation')
        with blocker:
            view.on_confirm()
            self.assertTrue(entered.wait(1))
            self.views.remove(view)
            from PySide6.QtCore import QCoreApplication, QEvent
            view.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            release.set()
            self.service.submit('edit_curation', self.a, rating=1).result(3)
            QApplication.processEvents()
        self.assertEqual(self.service.read('get_curation_snapshot', (self.a,))[0]['tags'], ('alpha',))

    def test_queue_full_error_visible_no_gui_future_wait(self):
        self.register(self.a)
        view = self.view()
        self.image(view, self.a)
        self.ready(view)
        future = Future()
        future.set_exception(LibraryError('queue_full', 'Busy fixture'))
        with patch.object(self.service, 'submit', return_value=future):
            view.on_confirm()
            self.settled(view)
        self.assertIn('Busy fixture', view.lbl_status.text())
        self.assertEqual(view.worker.next_count, 0)

    def test_new_image_cannot_redirect_old_confirm_or_advance_new_worker(self):
        self.register(self.a, self.b)
        view = self.view()
        self.image(view, self.a)
        self.ready(view)
        old = view.worker
        entered, release, blocker = self.blocked('edit_curation')
        with blocker:
            view.on_confirm()
            self.assertTrue(entered.wait(1))
            view.worker = PausedWorker((self.b,))
            self.image(view, self.b, 'Beta')
            release.set()
            self.settled(view)
        self.assertEqual(self.service.read('get_curation_snapshot', (self.a,))[0]['tags'], ('alpha',))
        self.assertEqual(self.service.read('get_curation_snapshot', (self.b,))[0]['tags'], ())
        self.assertEqual((old.next_count, view.worker.next_count), (0, 0))
        self.assertEqual(view.current_path, self.b)

    def test_remove_failure_preserves_tag_and_then_confirms_removal(self):
        self.register(self.a)
        self.service.submit('edit_curation', self.a, add_tags=['old'], rating=4).result(3)
        view = self.view()
        self.image(view, self.a)
        self.ready(view)
        view.list_tags.setCurrentRow(0)
        with patch.object(LibraryRepository, 'edit_curation', side_effect=OSError('disk full')):
            view.on_remove_tag()
            self.settled(view)
        self.assertIn('disk full', view.lbl_status.text())
        self.assertEqual(self.tags(view), ['old'])
        view.list_tags.setCurrentRow(0)
        view.on_remove_tag()
        self.settled(view)
        self.assertEqual(self.tags(view), ['(No tags)'])
        self.assertEqual(view.worker.next_count, 0)
        self.assertEqual(self.service.read('get_curation_snapshot', (self.a,))[0]['rating'], 4)

    def test_original_bytes_unchanged_and_other_curation_preserved(self):
        self.register(self.a)
        before = Path(self.a).read_bytes()
        self.service.submit('edit_curation', self.a, add_tags=['other'], rating=5).result(3)
        view = self.view()
        self.image(view, self.a)
        self.ready(view)
        view.on_confirm()
        self.settled(view)
        self.assertEqual(Path(self.a).read_bytes(), before)
        row = self.service.read('get_curation_snapshot', (self.a,))[0]
        self.assertEqual((row['tags'], row['rating']), (('alpha', 'other'), 5))

    def test_manual_learns_captured_embedding_only_after_commit(self):
        self.register(self.a)
        view = self.view()
        emb = np.array([1., 2., 3.], dtype=np.float32)
        self.image(view, self.a, None, emb)
        self.ready(view)
        view.combo_names.setEditText('Manual')
        with patch.object(LibraryRepository, 'edit_curation', side_effect=OSError('blocked')):
            view.on_manual_save()
            self.settled(view)
        self.assertEqual(view.profile_db.get_all_profiles(), [])
        entered, release, blocker = self.blocked('edit_curation')
        with blocker:
            view.on_manual_save()
            self.assertTrue(entered.wait(1))
            emb[:] = 0
            self.assertEqual(view.profile_db.get_all_profiles(), [])
            release.set()
            self.settled(view)
        name, stored = view.profile_db.get_all_profiles()[0]
        self.assertEqual(name, 'Manual')
        np.testing.assert_array_equal(stored, [1., 2., 3.])
        self.assertEqual(view.worker.next_count, 1)

    def bulk(self, view, paths, name='Bulk'):
        from modules.character_recognizer.logic.copy_bindings import capture
        view._auto_bindings=capture(self.service,list(paths))
        view._auto_input = tuple(paths)
        view._auto_cancelled = False
        view.on_auto_suggestion(name, .9, len(paths))

    def test_bulk_single_pending_bounded_writer_and_captured_deduplicated_paths(self):
        paths = [f'C:/fixture/{i}.png' for i in range(130)]
        self.register(*paths)
        view = self.view(paths)
        self.bulk(view, paths + [paths[0]])
        submit = self.service.submit
        active, maximum, calls = 0, 0, []
        def tracked(op, *args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            calls.append(args[0])
            future = submit(op, *args, **kwargs)
            def done(_):
                nonlocal active
                active -= 1
            future.add_done_callback(done)
            return future
        with patch.object(self.service, 'submit', tracked):
            view.on_tag_all()
            view.worker.paths[:] = ['C:/unrelated.png']
            view.on_tag_all()
            self.assertTrue(helpers.pump(lambda: view._pending_edit is None, 5))
        self.assertEqual(calls, paths)
        self.assertEqual(maximum, 1)
        self.assertEqual(view._bulk_done, 130)
        self.assertIn('All tagged', view.lbl_auto_status.text())
        self.assertEqual(self.service.read('get_curation_snapshot', (paths[-1],))[0]['tags'], ('bulk',))

    def test_bulk_error_preserves_confirmed_prefix_and_retries_only_tail(self):
        self.register(self.a, self.b)
        view = self.view()
        self.bulk(view, (self.a, self.b))
        edit = LibraryRepository.edit_curation
        calls = []
        def fail_second(repo, path, **kwargs):
            calls.append(path)
            if path == self.b:
                raise OSError('full fixture')
            return edit(repo, path, **kwargs)
        with patch.object(LibraryRepository, 'edit_curation', fail_second):
            view.on_tag_all()
            self.assertTrue(helpers.pump(lambda: view._pending_edit is None))
        self.assertEqual(view._bulk_done, 1)
        self.assertEqual(view._bulk_remaining, (self.b,))
        self.assertIn('1 confirmed', view.lbl_auto_status.text())
        self.assertNotIn('All tagged', view.lbl_auto_status.text())
        with patch.object(LibraryRepository, 'edit_curation', lambda repo, path, **kw: (calls.append(path), edit(repo, path, **kw))[1]):
            view.on_tag_all()
            self.assertTrue(helpers.pump(lambda: view._pending_edit is None))
        self.assertEqual(calls, [self.a, self.b, self.b])
        self.assertEqual(view._bulk_done, 2)

    def test_bulk_missing_image_stops_without_registration_or_false_success(self):
        self.register(self.a)
        view = self.view()
        self.bulk(view, (self.a, self.b))
        view.on_tag_all()
        self.assertTrue(helpers.pump(lambda: view._pending_edit is None))
        self.assertEqual(view._bulk_done, 1)
        self.assertIn('file_not_registered', view.lbl_auto_status.text())
        self.assertEqual(self.service.read('count_files'), 1)

    def test_unload_returns_immediately_accepted_write_survives_no_next_or_ui(self):
        self.register(self.a)
        view = self.view()
        self.image(view, self.a)
        self.ready(view)
        entered, release, blocker = self.blocked('edit_curation')
        with blocker:
            view.on_confirm()
            self.assertTrue(entered.wait(1))
            start = time.monotonic()
            view.shutdown()
            self.assertLess(time.monotonic() - start, .1)
            status = view.lbl_status.text()
            release.set()
            self.service.submit('edit_curation', self.a, rating=1).result(3)
            QApplication.processEvents()
        self.assertEqual(view.lbl_status.text(), status)
        self.assertEqual(view.worker.next_count, 0)
        self.assertEqual(self.service.read('get_curation_snapshot', (self.a,))[0]['tags'], ('alpha',))

    def test_bulk_unload_stops_tail_but_keeps_accepted_item(self):
        self.register(self.a, self.b)
        view = self.view()
        self.bulk(view, (self.a, self.b))
        entered, release, blocker = self.blocked('edit_curation')
        with blocker:
            view.on_tag_all()
            self.assertTrue(entered.wait(1))
            view.shutdown()
            release.set()
            self.service.submit('edit_curation', self.a, rating=1).result(3)
            QApplication.processEvents()
        rows = self.service.read('get_curation_snapshot', (self.a, self.b))
        self.assertEqual([r['tags'] for r in rows], [('bulk',), ()])

    def test_bulk_cancel_keeps_accepted_item_stops_tail_and_reports_partial(self):
        self.register(self.a, self.b)
        view = self.view()
        self.bulk(view, (self.a, self.b))
        entered, release, blocker = self.blocked('edit_curation')
        with blocker:
            view.on_tag_all()
            self.assertTrue(entered.wait(1))
            self.assertTrue(view.btn_auto_cancel.isEnabled())
            view.btn_auto_cancel.click()
            release.set()
            self.assertTrue(helpers.pump(lambda: view._pending_edit is None))
        self.assertEqual(view._bulk_done, 1)
        self.assertEqual(view._bulk_remaining, ())
        self.assertIn('Stopped after 1', view.lbl_auto_status.text())
        self.assertNotIn('All tagged', view.lbl_auto_status.text())
        rows = self.service.read('get_curation_snapshot', (self.a, self.b))
        self.assertEqual([r['tags'] for r in rows], [('bulk',), ()])

    def test_cancelled_auto_scan_late_suggestion_is_ignored(self):
        view = self.view()
        view._auto_input = (self.a,)
        view._auto_cancelled = False
        view.on_auto_cancel()
        view.on_auto_suggestion('late', .9, 1)
        self.assertEqual(view._auto_paths, ())
        self.assertTrue(view.frame_suggestion.isHidden())

    def test_real_qthread_replacement_never_waits_on_gui_latest_input_wins(self):
        from modules.character_recognizer.logic.thread_worker import RecognitionWorker
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class Engine:
            def initialize(self):
                entered.set()
                release.wait(3)
            def analyze_image(self, *args, **kwargs):
                return None, None, 0
        view = self.view()
        view.worker = None
        with patch('modules.character_recognizer.logic.thread_worker.RecognitionEngine', Engine):
            view.load_images((self.a,))
            old = view.worker
            self.assertIsInstance(old, RecognitionWorker)
            self.assertTrue(entered.wait(1))
            start = time.monotonic()
            view.load_images((self.b,))
            view.load_images((self.b, self.a))
            self.assertLess(time.monotonic() - start, .1)
            self.assertIs(view.worker, old)
            fired = []
            QTimer.singleShot(0, lambda: fired.append(True))
            self.assertTrue(helpers.pump(lambda: bool(fired)))
            release.set()
            self.assertTrue(helpers.pump(lambda: view.worker is not None and view.worker is not old))
            self.assertEqual(view.worker.paths, [self.b, self.a])
            view.shutdown()
            self.assertTrue(view.worker.wait(3000))

    def test_tooltips_semantic_colors_and_module_unload(self):
        from modules.character_recognizer.module import CharacterRecognizerModule
        view = self.view()
        controls = []
        for cls in (QPushButton, QComboBox, QListWidget):
            controls.extend(view.findChildren(cls))
        self.assertTrue(all(w.toolTip().strip() for w in controls))
        self.assertTrue(all('color: white' not in w.styleSheet() and 'color: black' not in w.styleSheet() for w in controls))
        module = CharacterRecognizerModule()
        module.view = module.content_view = view
        module.on_unload()
        self.assertTrue(view._closed)
        view.load_images((self.a,))
        self.assertIsNone(view._queued_paths)


if __name__ == '__main__':
    unittest.main(verbosity=2)
