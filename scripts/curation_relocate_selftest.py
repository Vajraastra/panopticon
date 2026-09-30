"""Registro tras mudanza/adopción GUI con archivos y DB temporales, Windows/offscreen."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from core.curation_apply import CurationApplyTasks, verify_file
from core.curation_sidecar import export_curation, NAME
from core.event_bus import EventBus
from core.library_repository import LibraryRepository
import curation_snapshot_selftest as helpers
import curation_apply_selftest as previous


class RelocationQt(helpers.Fixture):
    view = previous.ApplyQt.view
    ready = previous.ApplyQt.ready
    selected = previous.ApplyQt.selected
    shutdown = previous.ApplyQt.shutdown

    def setUp(self):
        super().setUp()
        self.modules, self.dialogs, self.appliers, self.releases = [], [], [], []
        self.bus = EventBus()
        self.folder = Path(self.temp.name) / 'colección ñ'
        self.folder.mkdir()
        self.addCleanup(self.shutdown)

    def sql(self, query, args=()):
        with closing(sqlite3.connect(self.service.db_path)) as conn, conn:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute(query, args)

    def state(self, path):
        return self.service.read('get_curation_restore_state', path)

    def dialog(self, module):
        module.review_folder_curation()
        dialog = module._review_dialog
        self.dialogs.append((dialog, dialog.reader))
        self.appliers.append(dialog.apply_reader)
        self.assertTrue(helpers.pump(lambda: not dialog._busy))
        return dialog

    def prepared(self, mode='moved', unknown=False):
        module, old_paths = self.selected()
        self.service.submit('edit_curation', old_paths[0], add_tags=['exported'], rating=5).result(3)
        self.sql("UPDATE files SET meta_model='preserved model',meta_positive='preserved prompt' WHERE path=?", (old_paths[0],))
        self.sql("UPDATE file_tags SET source='manual'")
        if unknown:
            self.sql('UPDATE files SET hash_original=NULL,hash_mod=NULL')
        export_curation(self.service.db_path, self.folder)
        self.old = self.state(old_paths[0])
        self.other = self.state(old_paths[1])
        if mode == 'moved':
            new_folder = Path(self.temp.name) / 'mudada ñ'
            self.folder.rename(new_folder)
            self.folder = new_folder
            self.service.submit('add_watched_folder', self.folder.as_posix()).result(3)
            paths = [(self.folder / Path(path).name).as_posix() for path in old_paths]
            module.load_folder_paths(self.folder.as_posix())
            self.assertTrue(helpers.pump(lambda: module._browse_ready))
        else:
            paths = old_paths
            if mode == 'absent':
                # Simular pérdida real del registro; retirar del inventario ahora lo archiva.
                self.sql('DELETE FROM files_fts WHERE rowid=?', (self.old['state']['id'],))
                self.sql('DELETE FROM files WHERE id=?', (self.old['state']['id'],))
            else:
                self.sql('UPDATE files SET hash_original=NULL,hash_mod=NULL WHERE path=?', (paths[0],))
                self.service.submit('edit_curation', paths[0], remove_tags=['exported'], rating=0).result(3)
        dialog = self.dialog(module)
        return module, dialog, old_paths, paths

    def apply_new(self, dialog):
        self.assertTrue(dialog.register_button.isEnabled())
        dialog.register_button.click()
        self.assertTrue(helpers.pump(lambda: dialog._restoring is None and not dialog._busy))

    def test_moved_folder_registers_only_selected_copy_and_keeps_old_records(self):
        module, dialog, old_paths, paths = self.prepared()
        self.assertEqual(dialog._reports[0]['status'], 'unregistered')
        self.assertFalse(dialog.apply_button.isEnabled())
        before = [Path(path).read_bytes() for path in paths]
        artifact = (self.folder / NAME).read_bytes()
        self.apply_new(dialog)
        restored = self.state(paths[0])['state']
        self.assertEqual(restored['tags'], self.old['state']['tags'])
        self.assertEqual(restored['rating'], 5)
        self.assertNotEqual(restored['id'], self.old['state']['id'])
        self.assertEqual(self.state(old_paths[0]), self.old)
        self.assertEqual(self.state(old_paths[1]), self.other)
        with self.assertRaisesRegex(Exception, 'file_not_registered'):
            self.state(paths[1])
        self.assertEqual(self.service.read('count_files'), 3)
        self.assertEqual([Path(path).read_bytes() for path in paths], before)
        self.assertEqual((self.folder / NAME).read_bytes(), artifact)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            row = conn.execute('SELECT file_size,created_date,meta_model,meta_positive FROM files WHERE path=?', (paths[0],)).fetchone()
            self.assertEqual(row[0], len(before[0]))
            self.assertEqual(row[1], getattr(Path(paths[0]).stat(), 'st_birthtime', Path(paths[0]).stat().st_ctime))
            self.assertEqual(row[2:], (None, None))
        self.assertTrue(helpers.pump(lambda: module._browse_ready and module._selection_count == 1))
        self.assertEqual(dialog._reports[0]['status'], 'same')
        self.assertIn('Restored exported', dialog.restore_status.text())

    def test_null_identity_adoption_keeps_reviewed_id_and_generation_metadata(self):
        _, dialog, _, paths = self.prepared('adopt')
        self.assertEqual(dialog._reports[0]['status'], 'central_identity_missing')
        self.assertIn('Complete identity', dialog.register_button.text())
        self.assertFalse(dialog.apply_button.isEnabled())
        before = self.state(paths[0])
        self.apply_new(dialog)
        after = self.state(paths[0])['state']
        self.assertEqual(after['id'], before['state']['id'])
        self.assertEqual(after, self.old['state'])
        self.assertEqual(self.state(paths[1]), self.other)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT meta_model,meta_positive FROM files WHERE path=?',
                (paths[0],)).fetchone(), ('preserved model', 'preserved prompt'))

    def test_route_registered_after_absence_review_is_not_overwritten(self):
        _, dialog, _, paths = self.prepared('absent')
        self.service.submit('register_files_minimal', [dict(path=paths[0], filename=Path(paths[0]).name,
            size=1, created=1, hash_original=self.old['state']['hash_original'])]).result(3)
        self.service.submit('edit_curation', paths[0], add_tags=['newer'], rating=2).result(3)
        before = self.state(paths[0])
        self.apply_new(dialog)
        self.assertIn('registered after', dialog.restore_status.text())
        self.assertEqual(self.state(paths[0]), before)
        self.assertIsNone(dialog._reports[0]['fingerprint'])

    def test_adoption_never_refreshes_changes_since_shown_review(self):
        _, dialog, _, paths = self.prepared('adopt')
        fingerprint = dialog._reports[0]['fingerprint']
        self.service.submit('edit_curation', paths[0], add_tags=['newer'], rating=2).result(3)
        before = self.state(paths[0])
        self.apply_new(dialog)
        self.assertIn('changed since', dialog.restore_status.text())
        self.assertEqual(self.state(paths[0]), before)
        self.assertEqual(dialog._reports[0]['fingerprint'], fingerprint)

    def test_any_known_different_hash_blocks_registration_and_adoption(self):
        _, dialog, _, paths = self.prepared('adopt')
        for original, modified in ((self.old['state']['hash_original'], 'c'*64), (None, 'c'*64)):
            self.sql('UPDATE files SET hash_original=?,hash_mod=? WHERE path=?', (original, modified, paths[0]))
            dialog.restart()
            self.assertTrue(helpers.pump(lambda: not dialog._busy))
            self.assertEqual(dialog._reports[0]['status'], 'identity_changed')
            self.assertFalse(dialog.register_button.isEnabled())
            self.assertFalse(dialog.apply_button.isEnabled())

    def test_unknown_export_and_incomplete_preview_never_allow_new_action(self):
        _, dialog, _, _ = self.prepared(unknown=True)
        self.assertFalse(dialog.register_button.isEnabled())
        report = dialog._reports[0]
        self.assertFalse(report['known_identity'])
        report['known_identity'] = True
        for side in ('sidecar', 'central'):
            report[side]['truncated'] = True
            dialog._update_apply()
            self.assertFalse(dialog.register_button.isEnabled())
            report[side]['truncated'] = False

    def test_changed_bytes_never_admit_registration(self):
        _, dialog, _, paths = self.prepared()
        Path(paths[0]).write_bytes(b'changed')
        with patch.object(self.service, 'submit', side_effect=AssertionError('admitted')):
            self.apply_new(dialog)
        self.assertIn('file bytes', dialog.restore_status.text())
        self.assertEqual(self.service.read('count_files'), 2)

    def test_missing_file_never_admits_registration(self):
        _, dialog, _, paths = self.prepared()
        Path(paths[0]).unlink()
        with patch.object(self.service, 'submit', side_effect=AssertionError('admitted')):
            self.apply_new(dialog)
        self.assertIn('Could not restore', dialog.restore_status.text())
        self.assertEqual(self.service.read('count_files'), 2)

    def test_changed_export_rejects_registration(self):
        _, dialog, _, _ = self.prepared('absent')
        export_curation(self.service.db_path, self.folder)
        self.apply_new(dialog)
        self.assertIn('export changed', dialog.restore_status.text())
        self.assertEqual(self.service.read('count_files'), 1)

    def test_category_conflict_rolls_back_new_file_and_preserves_other_copy(self):
        _, dialog, old_paths, _ = self.prepared()
        self.sql("UPDATE tags SET category='different' WHERE name='exported'")
        before = self.state(old_paths[0])
        self.apply_new(dialog)
        self.assertIn('shared tag', dialog.restore_status.text())
        self.assertEqual(self.service.read('count_files'), 2)
        self.assertEqual(self.state(old_paths[0]), before)

    def test_fts_failure_rolls_back_registration_without_false_success(self):
        _, dialog, _, _ = self.prepared()
        with patch.object(LibraryRepository, '_fts_sync_file', side_effect=RuntimeError('forced_fts')):
            self.apply_new(dialog)
        self.assertIn('Could not restore', dialog.restore_status.text())
        self.assertEqual(self.service.read('count_files'), 2)
        self.assertEqual(dialog._reports[0]['status'], 'unregistered')

    def test_read_select_and_restart_never_register_automatically(self):
        _, dialog, _, _ = self.prepared()
        with patch.object(self.service, 'submit', side_effect=AssertionError('automatic write')):
            dialog.table.selectRow(1)
            dialog.restart()
            self.assertTrue(helpers.pump(lambda: not dialog._busy))
        self.assertEqual(self.service.read('count_files'), 2)

    def test_hash_worker_keeps_qt_responsive_and_blocks_duplicate_action(self):
        _, dialog, _, _ = self.prepared()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = CurationApplyTasks.read
        calls = []
        def paused(tasks, operation, **kwargs):
            self.assertNotEqual(threading.get_ident(), threading.main_thread().ident)
            calls.append(kwargs['mode'])
            entered.set()
            self.assertTrue(release.wait(3))
            return original(tasks, operation, **kwargs)
        with patch.object(CurationApplyTasks, 'read', paused):
            start = time.monotonic()
            dialog.register_selected()
            self.assertLess(time.monotonic() - start, .1)
            self.assertTrue(helpers.pump(entered.is_set))
            dialog.register_selected()
            self.assertFalse(dialog.apply_button.isEnabled())
            self.assertFalse(dialog.register_button.isEnabled())
            pulses = []
            QTimer.singleShot(0, lambda: pulses.append(True))
            self.assertTrue(helpers.pump(lambda: pulses))
            release.set()
            self.assertTrue(helpers.pump(lambda: dialog._restoring is None and not dialog._busy))
        self.assertEqual(calls, ['register'])

    def test_close_before_admission_cancels_registration(self):
        _, dialog, _, _ = self.prepared()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = CurationApplyTasks.read
        def paused(tasks, operation, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(tasks, operation, **kwargs)
        with patch.object(CurationApplyTasks, 'read', paused):
            dialog.register_selected()
            self.assertTrue(helpers.pump(entered.is_set))
            reader = dialog.apply_reader
            start = time.monotonic()
            dialog.close()
            self.assertLess(time.monotonic() - start, .1)
            release.set()
            reader._thread.join(3)
            self.assertFalse(reader._thread.is_alive())
        self.assertEqual(self.service.read('count_files'), 2)

    def test_close_after_admission_keeps_accepted_registration_with_owner(self):
        _, dialog, _, paths = self.prepared()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = LibraryRepository.register_curation_copy
        def paused(repo, *args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(repo, *args, **kwargs)
        with patch.object(LibraryRepository, 'register_curation_copy', paused):
            dialog.register_selected()
            self.assertTrue(helpers.pump(entered.is_set))
            reader = dialog.apply_reader
            dialog.close()
            release.set()
            reader._thread.join(3)
            self.assertFalse(reader._thread.is_alive())
        self.assertEqual(self.state(paths[0])['state']['rating'], 5)

    def test_failed_page_retained_selection_cannot_enable_registration(self):
        _, dialog, _, _ = self.prepared()
        with patch('core.curation_review.compare_curation_page', side_effect=ValueError('sidecar_changed')):
            dialog.next_button.click()
            self.assertTrue(helpers.pump(lambda: not dialog._busy))
        dialog.table.selectRow(1)
        self.assertFalse(dialog.register_button.isEnabled())
        self.assertFalse(dialog.apply_button.isEnabled())

    def test_source_stat_change_after_hash_rejects_before_admission(self):
        _, dialog, _, paths = self.prepared()
        def changed(path, digest, event):
            before = verify_file(path, digest, event)
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
            return before
        with patch('core.curation_apply.verify_file', side_effect=changed), patch.object(
                self.service, 'submit', side_effect=AssertionError('admitted')):
            self.apply_new(dialog)
        self.assertIn('file changed', dialog.restore_status.text())
        self.assertEqual(self.service.read('count_files'), 2)

    def test_invalid_mode_or_register_fingerprint_fail_before_io(self):
        tasks = CurationApplyTasks(self.service)
        params = dict(folder=self.folder, copy_id=1, relative_path='a.png', export_id='token', expected_fingerprint=None)
        with patch('core.curation_apply.read_curation_page', side_effect=AssertionError('IO')):
            with self.assertRaisesRegex(ValueError, 'invalid_curation_apply'):
                tasks.read('apply_curation', **params, mode='automatic')
            with self.assertRaisesRegex(ValueError, 'invalid_restore_fingerprint'):
                tasks.read('apply_curation', **dict(params, expected_fingerprint='a'*64), mode='register')
            with self.assertRaisesRegex(ValueError, 'invalid_restore_fingerprint'):
                tasks.read('apply_curation', **params, mode='adopt')


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
