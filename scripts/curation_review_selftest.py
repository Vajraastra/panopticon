"""Revisión RO en Qt con bases/carpeta temporales; sin aplicación real ni imágenes."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from contextlib import closing
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from core.curation_review import CurationReviewTasks, _preview, TEXT_LIMIT
from core.curation_sidecar import export_curation, NAME
from core.components.curation_review import CurationReviewDialog
from core.event_bus import EventBus
from core.theme_manager import ThemeManager
import curation_snapshot_selftest as helpers
import librarian_data_selftest as prior
import librarian_export_selftest as exported


class ReviewQt(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.modules = []
        self.releases = []
        self.dialogs = []
        self.bus = EventBus()
        self.folder = Path(self.temp.name) / 'colección ñ'
        self.folder.mkdir()
        self.addCleanup(self.shutdown)

    view = prior.LibrarianQt.view
    ready = prior.LibrarianQt.ready
    selected = exported.ExportQt.selected

    def shutdown(self):
        for release in self.releases:
            release.set()
        for dialog, reader in self.dialogs:
            if not reader._closed:
                dialog.close()
            if reader._thread is not None:
                reader._thread.join(3)
                self.assertFalse(reader._thread.is_alive())
        prior.LibrarianQt.shutdown(self)

    def open_review(self, count=203):
        module, paths = self.selected(count)
        export_curation(self.service.db_path, self.folder)
        module.review_folder_curation()
        dialog = module._review_dialog
        self.dialogs.append((dialog, dialog.reader))
        self.assertTrue(helpers.pump(lambda: not dialog._busy))
        return module, dialog, paths

    def next(self, dialog):
        dialog.next_button.click()
        self.assertTrue(helpers.pump(lambda: not dialog._busy))

    def test_sequential_pages_keep_only_current_rows_and_verify_terminal_count(self):
        module, dialog, _ = self.open_review()
        self.assertEqual(dialog.table.rowCount(), 100)
        self.assertEqual(dialog._seen, 100)
        self.next(dialog)
        self.assertEqual(dialog._seen, 200)
        self.assertEqual(dialog.table.item(0, 0).text(), '0100.png')
        self.assertEqual(len(dialog._reports), 100)
        self.next(dialog)
        self.assertEqual(dialog.table.rowCount(), 3)
        self.assertNotIn('complete', dialog.status.text())
        self.next(dialog)
        self.assertIn('complete', dialog.status.text())
        self.assertIn('203', dialog.status.text())
        self.assertFalse(dialog.next_button.isEnabled())
        dialog.restart_button.click()
        self.assertTrue(helpers.pump(lambda: not dialog._busy))
        self.assertEqual(dialog._seen, 100)
        self.assertEqual(dialog.table.rowCount(), 100)

    def test_conflict_retains_rating_zero_empty_tags_and_exported_values(self):
        module, paths = self.selected(2)
        self.service.submit('edit_curation', paths[0], add_tags=['exported'], rating=5).result(3)
        export_curation(self.service.db_path, self.folder)
        self.service.submit('edit_curation', paths[0], remove_tags=['exported'], rating=0).result(3)
        module.review_folder_curation()
        dialog = module._review_dialog
        self.dialogs.append((dialog, dialog.reader))
        self.assertTrue(helpers.pump(lambda: not dialog._busy))
        self.assertEqual([r['status'] for r in dialog._reports], ['conflict', 'same'])
        self.assertEqual(json.loads(dialog.central_text.toPlainText())['rating'], 0)
        self.assertEqual(json.loads(dialog.central_text.toPlainText())['tags'], [])
        self.assertEqual(json.loads(dialog.sidecar_text.toPlainText())['rating'], 5)
        self.assertIn('exported', dialog.sidecar_text.toPlainText())
        self.assertIn('conflict', dialog.table.item(0, 1).text())

    def test_changed_export_stops_traversal_preserves_page_and_restart_recovers(self):
        _, dialog, _ = self.open_review()
        old = dialog._token
        export_curation(self.service.db_path, self.folder)
        self.next(dialog)
        self.assertIn('export changed', dialog.status.text())
        self.assertEqual(dialog._seen, 100)
        self.assertEqual(dialog.table.rowCount(), 100)
        dialog.restart_button.click()
        self.assertTrue(helpers.pump(lambda: not dialog._busy))
        self.assertNotEqual(dialog._token, old)
        self.assertEqual(dialog._seen, 100)

    def test_missing_records_do_not_announce_complete(self):
        _, dialog, _ = self.open_review(103)
        with closing(sqlite3.connect(self.folder / NAME)) as conn, conn:
            conn.execute('DELETE FROM curation_copies WHERE id>100')
        self.next(dialog)
        self.assertIn('does not match', dialog.status.text())
        self.assertNotIn('complete:', dialog.status.text())
        self.assertEqual(dialog.table.rowCount(), 100)

    def test_total_changed_without_new_token_is_rejected(self):
        _, dialog, _ = self.open_review()
        with closing(sqlite3.connect(self.folder / NAME)) as conn, conn:
            conn.execute("UPDATE meta SET value=204 WHERE key='copy_count'")
        self.next(dialog)
        self.assertIn('export changed', dialog.status.text())
        self.assertEqual(dialog._seen, 100)

    def test_no_database_or_sidecar_writes_during_review(self):
        _, dialog, paths = self.open_review(2)
        snapshot = self.service.read('get_curation_snapshot', paths)
        digest = hashlib.sha256((self.folder / NAME).read_bytes()).digest()
        with patch.object(self.service, 'submit', side_effect=AssertionError('write')):
            self.next(dialog)
            dialog.restart_button.click()
            self.assertTrue(helpers.pump(lambda: not dialog._busy))
        self.assertEqual(snapshot, self.service.read('get_curation_snapshot', paths))
        self.assertEqual(digest, hashlib.sha256((self.folder / NAME).read_bytes()).digest())

    def test_worker_keeps_qt_responsive_and_close_cancels_without_wait(self):
        module, _ = self.selected(2)
        export_curation(self.service.db_path, self.folder)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = CurationReviewTasks.read
        def paused(tasks, operation, **kwargs):
            self.assertNotEqual(threading.get_ident(), threading.main_thread().ident)
            entered.set()
            self.assertTrue(release.wait(3))
            return original(tasks, operation, **kwargs)
        with patch.object(CurationReviewTasks, 'read', paused):
            module.review_folder_curation()
            dialog = module._review_dialog
            reader = dialog.reader
            self.dialogs.append((dialog, reader))
            self.assertTrue(entered.wait(1))
            tick = []
            QTimer.singleShot(0, lambda: tick.append(True))
            self.assertTrue(helpers.pump(lambda: tick))
            before = time.monotonic()
            module.on_unload()
            self.assertLess(time.monotonic() - before, .1)
            self.assertTrue(reader._cancel.is_set())
            self.assertIsNone(module._review_dialog)
            release.set()
            reader._thread.join(3)
            self.assertFalse(reader._thread.is_alive())
            QApplication.processEvents()

    def test_one_window_captured_folder_and_reopen_after_close(self):
        module, dialog, _ = self.open_review(2)
        module.review_folder_curation()
        self.assertIs(module._review_dialog, dialog)
        other = Path(self.temp.name) / 'other'
        other.mkdir()
        module.load_folder_paths(other.as_posix())
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertEqual(dialog.folder, self.folder.as_posix())
        dialog.close()
        self.assertIsNone(module._review_dialog)
        module.review_folder_curation()
        new = module._review_dialog
        self.dialogs.append((new, new.reader))
        self.assertTrue(helpers.pump(lambda: not new._busy))
        self.assertIn('Comparison stopped', new.status.text())
        self.assertFalse((other / NAME).exists())

    def test_empty_operational_folder_reviews_history_without_allowing_registration(self):
        module, dialog, paths = self.open_review(2)
        dialog.close()
        self.service.submit('remove_files', paths).result(3)
        module.load_folder_paths(self.folder.as_posix())
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        # CF9 conserva historial exportable aunque ya no haya copias operativas.
        self.assertTrue(module.btn_export_curation.isEnabled())
        self.assertTrue(module.btn_review_curation.isEnabled())
        module.review_folder_curation()
        dialog = module._review_dialog
        self.dialogs.append((dialog, dialog.reader))
        self.assertTrue(helpers.pump(lambda: not dialog._busy))
        self.assertEqual([r['status'] for r in dialog._reports], ['offline_history', 'offline_history'])
        self.assertFalse(dialog.register_button.isEnabled())
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertIn('No registered copy', dialog.central_text.toPlainText())

    def test_filters_export_and_folder_write_disable_review(self):
        module, _ = self.selected(2)
        for attribute, value in (('active_tags', ['alpha']), ('_external_paths', ('fixture',)),
                                  ('_exporting', self.folder), ('_folder_write', (1, 'add', 'fixture'))):
            old = getattr(module, attribute)
            setattr(module, attribute, value)
            module._update_export_controls()
            self.assertFalse(module.btn_review_curation.isEnabled())
            module.review_folder_curation()
            self.assertIsNone(module._review_dialog)
            setattr(module, attribute, old)

    def test_tooltips_plain_text_bilingual_strings_and_semantic_colors(self):
        _, dialog, _ = self.open_review(2)
        for control in (dialog.table, dialog.sidecar_text, dialog.central_text,
                        dialog.restart_button, dialog.next_button, dialog.apply_button,
                        dialog.register_button, dialog.close_button):
            self.assertTrue(control.toolTip())
        for control in (dialog.restart_button, dialog.sidecar_text):
            self.assertIn(ThemeManager().get_color('bg_input'), control.styleSheet())
        root = Path(__file__).resolve().parents[1]
        keysets = []
        for language in ('en', 'es'):
            values = json.loads((root / 'locales' / (language + '.json')).read_text(encoding='utf-8'))
            keysets.append({key for key in values if key.startswith('lib.review.')})
            self.assertTrue({'lib.review.register', 'lib.review.adopt', 'lib.review.register_tip',
                             'lib.review.state.central_identity_missing'} <= keysets[-1])
            for key, value in values.items():
                if key.startswith('lib.review.'):
                    self.assertTrue(value.format(count=1, seen=1, total=2, reason='fixture', path='fixture'))
        self.assertEqual(keysets[0], keysets[1])


class WorkerContract(unittest.TestCase):
    def test_overflow_adapts_page_on_worker_and_single_copy_error_is_visible(self):
        calls = []
        def compare(db, folder, **page):
            calls.append(page['limit'])
            if page['limit'] > 25:
                raise ValueError('sidecar_payload_too_large')
            return dict(export_id='token', total=0, after=0, reports=[])
        with patch('core.curation_review.compare_curation_page', compare):
            result = CurationReviewTasks('fixture').read('compare_curation', folder='fixture')
        self.assertEqual(calls, [100, 50, 25])
        self.assertEqual(result['reports'], [])
        with patch('core.curation_review.compare_curation_page', side_effect=ValueError('sidecar_payload_too_large')):
            with self.assertRaisesRegex(ValueError, 'sidecar_payload_too_large'):
                CurationReviewTasks('fixture').read('compare_curation', folder='fixture')

    def test_pre_cancelled_reader_does_no_io(self):
        event = threading.Event()
        event.set()
        with patch('core.curation_review.compare_curation_page', side_effect=AssertionError('IO')):
            with self.assertRaisesRegex(RuntimeError, 'curation_cancelled'):
                CurationReviewTasks('fixture').read('compare_curation', folder='fixture', cancel_event=event)

    def test_preview_is_bounded_without_silently_reporting_complete_data(self):
        state = dict(hash_original='a' * 64, hash_mod=None, rating=0,
                     tags=[dict(name=str(i) + 'x' * 4090, source='manual', category='general') for i in range(100)])
        value = _preview(state)
        self.assertLessEqual(len(value['text']), TEXT_LIMIT)
        self.assertTrue(value['truncated'])
        self.assertEqual(value['tags'], 100)
        preview = json.loads(value['text'])
        self.assertEqual(preview['rating'], 0)
        self.assertLess(len(preview['tags']), 100)


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
