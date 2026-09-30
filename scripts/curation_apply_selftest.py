"""Elección GUI explícita y verificación SHA con archivos/base temporales."""
import hashlib
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from contextlib import closing
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from core.curation_apply import CurationApplyTasks, verify_file
from core.curation_sidecar import export_curation, read_curation_page, NAME
from core.library_repository import LibraryRepository
from core.event_bus import EventBus
import curation_snapshot_selftest as helpers
import curation_review_selftest as review
import librarian_data_selftest as prior


class ApplyQt(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.modules = []
        self.dialogs = []
        self.appliers = []
        self.releases = []
        self.bus = EventBus()
        self.folder = Path(self.temp.name) / 'colección ñ'
        self.folder.mkdir()
        self.addCleanup(self.shutdown)

    view = prior.LibrarianQt.view
    ready = prior.LibrarianQt.ready

    def shutdown(self):
        for release in self.releases:
            release.set()
        for dialog, reader in self.dialogs:
            if not reader._closed:
                dialog.close()
        for reader in self.appliers:
            if reader._thread is not None:
                reader._thread.join(3)
                self.assertFalse(reader._thread.is_alive())
        review.ReviewQt.shutdown(self)

    def selected(self, count=2):
        paths = [(self.folder / f'{i:04}.png').as_posix() for i in range(count)]
        data = b'portable fixture bytes'
        for path in paths:
            Path(path).write_bytes(data)
        self.service.submit('register_files_minimal', [dict(path=path, filename=Path(path).name,
            size=len(data), created=1, hash_original=hashlib.sha256(data).hexdigest()) for path in paths]).result(3)
        self.service.submit('add_watched_folder', self.folder.as_posix()).result(3)
        module = self.view()
        self.ready(module)
        module.load_folder_paths(self.folder.as_posix())
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        return module, paths

    def opened(self):
        module, paths = self.selected()
        self.service.submit('edit_curation', paths[0], add_tags=['exported'], rating=5).result(3)
        export_curation(self.service.db_path, self.folder)
        self.service.submit('edit_curation', paths[0], remove_tags=['exported'], rating=0).result(3)
        module.review_folder_curation()
        dialog = module._review_dialog
        self.dialogs.append((dialog, dialog.reader))
        self.appliers.append(dialog.apply_reader)
        self.assertTrue(helpers.pump(lambda: not dialog._busy))
        self.assertTrue(dialog.apply_button.isEnabled())
        return module, dialog, paths

    def apply(self, dialog):
        dialog.apply_button.click()
        self.assertTrue(helpers.pump(lambda: dialog._restoring is None and not dialog._busy))

    def test_explicit_choice_restores_only_one_copy_and_refreshes_confirmed_view(self):
        module, dialog, paths = self.opened()
        other = self.service.read('get_curation_restore_state', paths[1])
        bytes_before = [Path(path).read_bytes() for path in paths]
        artifact = (self.folder / NAME).read_bytes()
        self.apply(dialog)
        state = self.service.read('get_curation_restore_state', paths[0])['state']
        self.assertEqual(state['rating'], 5)
        self.assertEqual(state['tags'][0]['name'], 'exported')
        self.assertEqual(self.service.read('get_curation_restore_state', paths[1]), other)
        self.assertIn('Restored exported', dialog.restore_status.text())
        self.assertEqual(dialog._reports[0]['status'], 'same')
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertEqual([Path(path).read_bytes() for path in paths], bytes_before)
        self.assertEqual((self.folder / NAME).read_bytes(), artifact)

    def test_read_and_selection_never_apply_automatically(self):
        _, dialog, paths = self.opened()
        with patch.object(self.service, 'submit', side_effect=AssertionError('automatic write')):
            dialog.table.selectRow(1)
            dialog.restart_button.click()
            self.assertTrue(helpers.pump(lambda: not dialog._busy))
        self.assertEqual(self.service.read('get_curation_restore_state', paths[0])['state']['rating'], 0)

    def test_native_changes_since_display_are_not_silently_refreshed_into_approval(self):
        _, dialog, paths = self.opened()
        self.service.submit('edit_curation', paths[0], add_tags=['newer'], rating=2).result(3)
        current = self.service.read('get_curation_restore_state', paths[0])
        self.apply(dialog)
        self.assertIn('changed since', dialog.restore_status.text())
        self.assertEqual(self.service.read('get_curation_restore_state', paths[0]), current)
        self.assertIn('"rating": 0', dialog.central_text.toPlainText())

    def test_new_export_token_does_not_apply_a_different_export(self):
        _, dialog, paths = self.opened()
        export_curation(self.service.db_path, self.folder)
        self.apply(dialog)
        self.assertIn('export changed', dialog.restore_status.text())
        self.assertEqual(self.service.read('get_curation_restore_state', paths[0])['state']['rating'], 0)

    def test_changed_file_bytes_reject_before_admission(self):
        _, dialog, paths = self.opened()
        Path(paths[0]).write_bytes(b'different content')
        with patch.object(self.service, 'submit', side_effect=AssertionError('should not admit')):
            self.apply(dialog)
        self.assertIn('file bytes', dialog.restore_status.text())
        self.assertEqual(self.service.read('get_curation_restore_state', paths[0])['state']['rating'], 0)

    def test_missing_file_rejects_without_registration_or_writes(self):
        _, dialog, paths = self.opened()
        Path(paths[0]).unlink()
        with patch.object(self.service, 'submit', side_effect=AssertionError('write')):
            self.apply(dialog)
        self.assertIn('Could not restore', dialog.restore_status.text())
        self.assertEqual(self.service.read('count_files'), 2)

    def test_global_category_conflict_does_not_change_other_copy(self):
        _, dialog, paths = self.opened()
        self.service.submit('edit_curation', paths[1], add_tags=['exported'], rating=3).result(3)
        with closing(sqlite3.connect(self.service.db_path)) as conn, conn:
            conn.execute("UPDATE tags SET category='other' WHERE name='exported'")
        other = self.service.read('get_curation_restore_state', paths[1])
        self.apply(dialog)
        self.assertIn('shared tag', dialog.restore_status.text())
        self.assertEqual(self.service.read('get_curation_restore_state', paths[1]), other)
        self.assertEqual(self.service.read('get_curation_restore_state', paths[0])['state']['rating'], 0)

    def test_unknown_or_unregistered_identity_and_incomplete_preview_disable_action(self):
        _, dialog, _ = self.opened()
        report = dialog._reports[0]
        for status in ('identity_changed', 'identity_missing', 'unregistered'):
            report['status'] = status
            dialog._update_apply()
            self.assertFalse(dialog.apply_button.isEnabled())
        report['status'] = 'conflict'
        for side in ('sidecar', 'central'):
            report[side]['truncated'] = True
            dialog._update_apply()
            self.assertFalse(dialog.apply_button.isEnabled())
            report[side]['truncated'] = False

    def test_pending_verification_keeps_qt_responsive_and_ignores_double_click(self):
        _, dialog, _ = self.opened()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = CurationApplyTasks.read
        calls = []
        def pause(tasks, operation, **kwargs):
            self.assertNotEqual(threading.get_ident(), threading.main_thread().ident)
            calls.append(kwargs)
            entered.set()
            self.assertTrue(release.wait(3))
            return original(tasks, operation, **kwargs)
        with patch.object(CurationApplyTasks, 'read', pause):
            before = time.monotonic()
            dialog.apply_selected()
            self.assertLess(time.monotonic() - before, .1)
            self.assertTrue(entered.wait(1))
            dialog.apply_selected()
            self.assertFalse(dialog.restart_button.isEnabled())
            self.assertFalse(dialog.next_button.isEnabled())
            tick = []
            QTimer.singleShot(0, lambda: tick.append(True))
            self.assertTrue(helpers.pump(lambda: tick))
            release.set()
            self.assertTrue(helpers.pump(lambda: dialog._restoring is None and not dialog._busy))
        self.assertEqual(len(calls), 1)

    def test_close_before_admission_cancels_without_writing_or_gui_wait(self):
        module, dialog, paths = self.opened()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = CurationApplyTasks.read
        def pause(tasks, operation, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(tasks, operation, **kwargs)
        with patch.object(CurationApplyTasks, 'read', pause):
            dialog.apply_selected()
            self.assertTrue(entered.wait(1))
            before = time.monotonic()
            module.on_unload()
            self.assertLess(time.monotonic() - before, .1)
            self.assertTrue(self.appliers[-1]._cancel.is_set())
            release.set()
            self.appliers[-1]._thread.join(3)
            QApplication.processEvents()
        self.assertEqual(self.service.read('get_curation_restore_state', paths[0])['state']['rating'], 0)

    def test_close_after_admission_keeps_accepted_write_owned_by_service(self):
        module, dialog, paths = self.opened()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = LibraryRepository.restore_curation
        def pause(repo, *args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(repo, *args, **kwargs)
        with patch.object(LibraryRepository, 'restore_curation', pause):
            dialog.apply_selected()
            self.assertTrue(entered.wait(1))
            before = time.monotonic()
            module.on_unload()
            self.assertLess(time.monotonic() - before, .1)
            release.set()
            self.appliers[-1]._thread.join(3)
            QApplication.processEvents()
        self.assertEqual(self.service.read('get_curation_restore_state', paths[0])['state']['rating'], 5)

    def test_failed_page_cannot_reenable_action_by_selecting_retained_row(self):
        _, dialog, _ = self.opened()
        export_curation(self.service.db_path, self.folder)
        dialog.next_button.click()
        self.assertTrue(helpers.pump(lambda: not dialog._busy))
        dialog.table.selectRow(1)
        dialog.table.selectRow(0)
        self.assertFalse(dialog.apply_button.isEnabled())

    def test_apply_from_complete_traversal_is_enabled_for_last_page(self):
        _, dialog, _ = self.opened()
        dialog.next_button.click()
        self.assertTrue(helpers.pump(lambda: not dialog._busy))
        self.assertTrue(dialog._complete)
        self.assertTrue(dialog.apply_button.isEnabled())

    def test_apply_uses_captured_folder_while_librarian_browses_elsewhere(self):
        module, dialog, paths = self.opened()
        other = Path(self.temp.name) / 'other'
        other.mkdir()
        self.service.submit('add_watched_folder', other.as_posix()).result(3)
        module.load_folder_paths(other.as_posix())
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.apply(dialog)
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertEqual(self.service.read('get_curation_restore_state', paths[0])['state']['rating'], 5)
        self.assertEqual(module.current_folder_path, other.as_posix())
        self.assertFalse((other / NAME).exists())

    def test_sidecar_change_during_verification_is_rejected_before_admission(self):
        _, dialog, paths = self.opened()
        original = verify_file
        def changed(*args, **kwargs):
            value = original(*args, **kwargs)
            with closing(sqlite3.connect(self.folder / NAME)) as conn, conn:
                conn.execute('UPDATE curation_copies SET rating=4 WHERE id=1')
            return value
        with patch('core.curation_apply.verify_file', changed), patch.object(
                self.service, 'submit', side_effect=AssertionError('write')):
            self.apply(dialog)
        self.assertIn('export changed', dialog.restore_status.text())
        self.assertEqual(self.service.read('get_curation_restore_state', paths[0])['state']['rating'], 0)


class Worker(helpers.Fixture):
    def test_pre_cancel_and_invalid_approval_do_no_filesystem_or_queue_work(self):
        event = threading.Event()
        event.set()
        tasks = CurationApplyTasks(self.service)
        params = dict(folder='missing', copy_id=1, relative_path='image.png', export_id='x',
                      expected_fingerprint='a'*64)
        with patch('core.curation_apply.read_curation_page', side_effect=AssertionError('IO')):
            with self.assertRaisesRegex(RuntimeError, 'curation_cancelled'):
                tasks.read('apply_curation', cancel_event=event, **params)
            params['expected_fingerprint'] = 'bad'
            with self.assertRaisesRegex(ValueError, 'invalid_restore_fingerprint'):
                tasks.read('apply_curation', **params)

    def test_missing_copy_or_wrong_relative_path_is_not_reassigned(self):
        tasks = CurationApplyTasks(self.service)
        params = dict(folder='fixture', copy_id=1, relative_path='image.png', export_id='x',
                      expected_fingerprint='a'*64)
        for copies in ([], [dict(id=2)], [dict(id=1, relative_path='other.png')]):
            with patch('core.curation_apply.read_curation_page', return_value=dict(copies=copies)):
                with self.assertRaisesRegex(ValueError, 'sidecar_changed'):
                    tasks.read('apply_curation', **params)

    def test_verify_rejects_changed_file_and_symbolic_link(self):
        path = Path(self.temp.name) / 'fixture.bin'
        path.write_bytes(b'fixture')
        digest = hashlib.sha256(b'fixture').hexdigest()
        self.assertIsNotNone(verify_file(path, digest.upper()))
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaisesRegex(ValueError, 'curation_file_symlink'):
                verify_file(path, digest)
        with patch('core.curation_apply._identity', side_effect=[1, 2]):
            with self.assertRaisesRegex(ValueError, 'curation_file_changed'):
                verify_file(path, digest)


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
