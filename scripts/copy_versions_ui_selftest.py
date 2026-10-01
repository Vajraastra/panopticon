"""Catálogo offline real Qt/servicio, archivos y DB temporales; sin colección viva."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QPushButton, QComboBox
from core.components.copy_versions import CopyVersionsDialog
from core.copy_version_tasks import CopyVersionTasks
from core.library_repository import LibraryRepository
from core.locale_manager import LocaleManager
from core.theme_manager import ThemeManager
import curation_snapshot_selftest as helpers
import librarian_data_selftest as prior
import librarian_export_selftest as exported


class CatalogQt(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.dialogs = []
        self.releases = []
        self.addCleanup(self.shutdown)

    def shutdown(self):
        for release in self.releases:
            release.set()
        for dialog in self.dialogs:
            if not dialog._closed:
                dialog.close()
            for reader in (dialog.reader, dialog.details_reader, dialog.action_reader):
                if reader._thread:
                    reader._thread.join(3)
                    self.assertFalse(reader._thread.is_alive())
        QApplication.processEvents()

    def copy(self, name='a.png', data=b'first', offline=True):
        path = Path(self.temp.name) / name
        path.write_bytes(data)
        self.service.submit('register_files_minimal', [dict(path=path.as_posix(), filename=name,
            size=len(data), created=1, hash_original=hashlib.sha256(data).hexdigest())]).result(3)
        self.service.submit('edit_curation', path.as_posix(), add_tags=['curated-' + name], rating=5).result(3)
        key = self.service.read('get_file_id', path.as_posix())
        if offline:
            state = self.service.read('get_copy_version_state', key)
            self.service.submit('archive_copy', key, expected_fingerprint=state['fingerprint']).result(3)
        return path, key

    def open(self):
        dialog = CopyVersionsDialog(self.service, LocaleManager().tr, ThemeManager())
        self.dialogs.append(dialog)
        self.page_ready(dialog)
        return dialog

    def page_ready(self, dialog):
        self.assertTrue(helpers.pump(lambda: not dialog._busy and not dialog.details_reader._active))

    def acted(self, dialog):
        self.assertTrue(helpers.pump(lambda: not dialog._acting and not dialog._busy and
                                   not dialog.details_reader._active))

    def test_default_offline_complete_review_without_automatic_writes(self):
        _, offline = self.copy()
        self.copy('active.png', offline=False)
        with patch.object(self.service, 'submit', side_effect=AssertionError('automatic write')):
            dialog = self.open()
        self.assertEqual([r['id'] for r in dialog._copies], [offline])
        self.assertEqual(json.loads(dialog.details.toPlainText()), dialog._reviewed['state'])
        self.assertFalse(dialog.archive_button.isEnabled())
        self.assertTrue(dialog.online_button.isEnabled())

    def test_same_hash_reactivates_same_curated_id_without_changing_file(self):
        path, key = self.copy()
        before = path.read_bytes()
        dialog = self.open()
        saved = []
        dialog.copies_changed.connect(lambda: saved.append(True))
        dialog.online_button.click()
        self.acted(dialog)
        self.assertEqual(self.service.read('get_file_id', path.as_posix()), key)
        self.assertEqual(self.service.read('get_curation_snapshot', [path.as_posix()])[0]['rating'], 5)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(saved, [True])
        self.assertEqual(dialog.table.rowCount(), 0)

    def test_changed_hash_creates_new_clean_id_and_keeps_old_curation(self):
        path, key = self.copy()
        dialog = self.open()
        path.write_bytes(b'replacement')
        dialog.online_button.click()
        self.acted(dialog)
        new = self.service.read('get_curation_snapshot', [path.as_posix()])[0]
        self.assertNotEqual(new['id'], key)
        self.assertEqual((new['rating'], new['tags']), (0, ()))
        old = self.service.read('get_copy_version_state', key)['state']
        self.assertEqual((old['file']['is_offline'], old['file']['rating']), (1, 5))
        self.assertEqual(dialog._selected_id, key)
        self.assertEqual(path.read_bytes(), b'replacement')

    def test_all_scope_can_archive_online_copy_without_file_io(self):
        path, key = self.copy(offline=False)
        dialog = self.open()
        self.assertEqual(dialog.table.rowCount(), 0)
        dialog.scope.setCurrentIndex(1)
        self.page_ready(dialog)
        with patch('core.copy_version_tasks.observe', side_effect=AssertionError('file IO')):
            dialog.archive_button.click()
            self.acted(dialog)
        self.assertEqual(self.service.read('count_files'), 0)
        self.assertEqual(path.read_bytes(), b'first')
        self.assertEqual(dialog._reviewed['state']['file']['id'], key)
        self.assertTrue(dialog._reviewed['state']['file']['is_offline'])

    def test_approval_state_change_rejected_before_hash_and_no_false_success(self):
        _, key = self.copy()
        dialog = self.open()
        saved = []
        dialog.copies_changed.connect(lambda: saved.append(True))
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('UPDATE files SET rating=3 WHERE id=?', (key,))
            conn.commit()
        with patch('core.copy_version_tasks.observe', side_effect=AssertionError('stale hash IO')):
            dialog.online_button.click()
            self.acted(dialog)
        self.assertIn(dialog.reason(ValueError('copy_version_changed_since_review')), dialog.action_status.text())
        self.assertEqual(saved, [])
        self.assertTrue(dialog._reviewed['state']['file']['is_offline'])
        self.assertEqual(dialog._reviewed['state']['file']['rating'], 3)

    def test_hash_worker_responsive_and_double_action_ignored(self):
        self.copy()
        dialog = self.open()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        from core.copy_version_tasks import observe
        def held(*args):
            self.assertNotEqual(threading.get_ident(), threading.main_thread().ident)
            entered.set()
            self.assertTrue(release.wait(3))
            return observe(*args)
        with patch('core.copy_version_tasks.observe', held):
            started = time.monotonic()
            dialog.online_button.click()
            self.assertLess(time.monotonic() - started, .1)
            self.assertTrue(entered.wait(1))
            dialog.act('make_copy_online')
            dialog.restart()
            self.assertEqual(dialog.action_reader._ticket, 1)
            self.assertFalse(dialog.scope.isEnabled())
            ticks = []
            QTimer.singleShot(0, lambda: ticks.append(True))
            self.assertTrue(helpers.pump(lambda: ticks))
            release.set()
            self.acted(dialog)

    def test_close_before_admission_cancels_without_commit_or_late_signal(self):
        _, key = self.copy()
        dialog = self.open()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        from core.copy_version_tasks import observe
        def held(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return observe(*args)
        saved = []
        dialog.copies_changed.connect(lambda: saved.append(True))
        with patch('core.copy_version_tasks.observe', held):
            dialog.online_button.click()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            dialog.close()
            self.assertLess(time.monotonic() - started, .1)
            release.set()
            dialog.action_reader._thread.join(3)
            self.assertFalse(dialog.action_reader._thread.is_alive())
            QApplication.processEvents()
        self.assertTrue(self.service.read('get_copy_version_state', key)['state']['file']['is_offline'])
        self.assertEqual(saved, [])

    def test_accepted_commit_finishes_after_close_without_late_widget_delivery(self):
        _, key = self.copy(offline=False)
        dialog = self.open()
        dialog.scope.setCurrentIndex(1)
        self.page_ready(dialog)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        archive = LibraryRepository.archive_copy
        def held(repo, *args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return archive(repo, *args, **kwargs)
        saved = []
        dialog.copies_changed.connect(lambda: saved.append(True))
        with patch.object(LibraryRepository, 'archive_copy', held):
            dialog.archive_button.click()
            self.assertTrue(entered.wait(1))
            dialog.close()
            release.set()
            dialog.action_reader._thread.join(3)
            self.assertFalse(dialog.action_reader._thread.is_alive())
            QApplication.processEvents()
        self.assertTrue(self.service.read('get_copy_version_state', key)['state']['file']['is_offline'])
        self.assertEqual(saved, [])

    def test_reject_escape_closes_all_readers(self):
        self.copy()
        dialog = self.open()
        dialog.reject()
        self.assertTrue(dialog._closed)
        self.assertTrue(all(r._closed for r in (dialog.reader, dialog.details_reader, dialog.action_reader)))

    def test_missing_file_error_keeps_history_and_allows_reviewed_retry(self):
        path, key = self.copy()
        dialog = self.open()
        path.unlink()
        dialog.online_button.click()
        self.acted(dialog)
        self.assertTrue(dialog.action_status.text())
        self.assertTrue(dialog._reviewed['state']['file']['is_offline'])
        path.write_bytes(b'first')
        dialog.online_button.click()
        self.acted(dialog)
        self.assertEqual(self.service.read('get_file_id', path.as_posix()), key)

    def test_excessive_state_disables_both_actions_without_truncation(self):
        _, key = self.copy()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('UPDATE files SET meta_positive=? WHERE id=?', ('x' * 65537, key))
            conn.commit()
        dialog = self.open()
        self.assertIsNone(dialog._reviewed)
        self.assertFalse(dialog.online_button.isEnabled())
        self.assertFalse(dialog.archive_button.isEnabled())
        self.assertIn(dialog.reason(ValueError('copy_version_payload_too_large')), dialog.details.toPlainText())

    def test_occupied_route_rejects_old_version_without_erasing_either(self):
        path, key = self.copy()
        before = self.service.read('get_copy_version_state', key)['fingerprint']
        path.write_bytes(b'new')
        CopyVersionTasks(self.service).read('make_copy_online', copy_id=key, expected_fingerprint=before)
        dialog = self.open()
        dialog.online_button.click()
        self.acted(dialog)
        self.assertIn(dialog.reason(ValueError('copy_version_path_occupied')), dialog.action_status.text())
        self.assertEqual(self.service.read('get_copy_version_state', key)['fingerprint'], before)
        self.assertEqual(self.service.read('count_files'), 1)

    def test_pages_bounded_and_signed_legacy_id_review(self):
        paths = [f'C:/offline/{i}.png' for i in range(203)]
        self.register(*paths)
        self.service.submit('remove_files', paths).result(3)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute("UPDATE files SET id=-9 WHERE path='C:/offline/0.png'")
            conn.commit()
        dialog = self.open()
        self.assertEqual(dialog._selected_id, -9)
        self.assertEqual(dialog.table.rowCount(), 100)
        self.assertTrue(dialog.online_button.isEnabled())
        dialog.next_button.click()
        self.page_ready(dialog)
        self.assertEqual(len(dialog._copies), 100)
        self.assertNotEqual(dialog._selected_id, -9)
        dialog.next_button.click()
        self.page_ready(dialog)
        self.assertEqual(dialog.table.rowCount(), 3)
        self.assertFalse(dialog.next_button.isEnabled())

    def test_rapid_selection_discards_old_complete_state(self):
        self.copy()
        _, second = self.copy('b.png')
        dialog = self.open()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        read = self.service.read
        def held(operation, *args, **kwargs):
            self.assertNotEqual(threading.get_ident(), threading.main_thread().ident)
            if operation == 'get_copy_version_state' and args[0] != second:
                entered.set()
                self.assertTrue(release.wait(3))
            return read(operation, *args, **kwargs)
        with patch.object(self.service, 'read', held):
            dialog._select()
            self.assertTrue(entered.wait(1))
            dialog.table.selectRow(1)
            self.assertFalse(dialog.online_button.isEnabled())
            release.set()
            self.page_ready(dialog)
        self.assertEqual(dialog._reviewed['state']['file']['id'], second)
        self.assertEqual(json.loads(dialog.details.toPlainText())['file']['id'], second)

    def test_page_error_never_keeps_previous_approval(self):
        self.copy()
        dialog = self.open()
        read = self.service.read
        def failed(operation, *args, **kwargs):
            self.assertNotEqual(threading.get_ident(), threading.main_thread().ident)
            if operation == 'get_copy_versions_page':
                raise ValueError('fixture-read-error')
            return read(operation, *args, **kwargs)
        with patch.object(self.service, 'read', failed):
            dialog.restart_button.click()
            self.page_ready(dialog)
        self.assertIsNone(dialog._reviewed)
        self.assertFalse(dialog.online_button.isEnabled())
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertIn('fixture-read-error', dialog.status.text())

    def test_tooltips_semantic_theme_and_bilingual_messages(self):
        self.copy()
        dialog = self.open()
        for button in dialog.findChildren(QPushButton):
            self.assertTrue(button.toolTip())
            self.assertIn('border:', button.styleSheet())
        self.assertTrue(dialog.scope.toolTip())
        self.assertTrue(dialog.table.toolTip())
        self.assertTrue(dialog.details.toolTip())
        for language in ('en', 'es'):
            values = json.loads((Path(__file__).resolve().parents[1] / 'locales' / (language + '.json')).read_text(encoding='utf-8'))
            keys = [key for key in values if key.startswith('lib.versions.')]
            self.assertEqual(len(keys), 48)
            for key in keys:
                self.assertTrue(values[key].format(id=1, count=100, reason='fixture'))


class IntegrationQt(helpers.Fixture):
    view = prior.LibrarianQt.view
    ready = prior.LibrarianQt.ready
    selected = exported.ExportQt.selected

    def setUp(self):
        super().setUp()
        from core.event_bus import EventBus
        self.bus = EventBus()
        self.modules = []
        self.releases = []
        self.folder = Path(self.temp.name) / 'collection'
        self.folder.mkdir()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        for module in self.modules:
            dialog = module._versions_dialog
            if dialog is not None:
                dialog.close()
                for reader in (dialog.reader, dialog.details_reader, dialog.action_reader):
                    if reader._thread:
                        reader._thread.join(3)
                        self.assertFalse(reader._thread.is_alive())
        prior.LibrarianQt.shutdown(self)

    def test_module_single_dialog_independent_navigation_and_reopen(self):
        module, _ = self.selected(1)
        module.review_copy_versions()
        first = module._versions_dialog
        module.review_copy_versions()
        self.assertIs(module._versions_dialog, first)
        self.assertTrue(helpers.pump(lambda: not first._busy))
        module.load_folder_paths('C:/another')
        self.assertIs(module._versions_dialog, first)
        first.reject()
        self.assertTrue(helpers.pump(lambda: module._versions_dialog is None))
        self.assertTrue(first._closed)
        module.review_copy_versions()
        self.assertIsNot(module._versions_dialog, first)

    def test_folder_with_only_offline_versions_can_export_them(self):
        from core.curation_sidecar import read_curation_page
        module, paths = self.selected(1)
        self.service.submit('remove_files', paths).result(3)
        module.refresh_current_view()
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertEqual(module._selection_count, 0)
        self.assertTrue(module.btn_export_curation.isEnabled())
        module.export_folder_curation()
        self.assertTrue(helpers.pump(lambda: module._exporting is None))
        value = read_curation_page(self.folder)
        self.assertEqual(value['total'], 1)
        self.assertTrue(value['copies'][0]['is_offline'])

    def test_committed_transition_refreshes_librarian_active_count(self):
        module, paths = self.selected(1)
        module.review_copy_versions()
        dialog = module._versions_dialog
        self.assertTrue(helpers.pump(lambda: not dialog._busy))
        dialog.scope.setCurrentIndex(1)
        self.assertTrue(helpers.pump(lambda: not dialog._busy and dialog._reviewed is not None))
        dialog.archive_button.click()
        self.assertTrue(helpers.pump(lambda: not dialog._acting and not dialog._busy and
                                   module._overview_ready and module._browse_ready and module._selection_count == 0))
        self.assertEqual(self.service.read('count_files'), 0)


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    # Avoid inheriting the export suite: only this unit's test classes are loaded.
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (CatalogQt, IntegrationQt))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
