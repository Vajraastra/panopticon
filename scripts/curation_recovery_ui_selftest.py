"""Recovery GUI con archivos temporales y kernel Windows; nunca biblioteca real."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton
from core.components.curation_recovery import CurationRecoveryDialog
from core.curation_recovery_tasks import CurationRecoveryTasks
from core.curation_export_lock import export_guard
from core.curation_recovery import inspect_recovery, LOCK, PREFIX
from core.curation_sidecar import export_curation, NAME
from core.event_bus import EventBus
import curation_snapshot_selftest as helpers
import curation_recovery_selftest as recovery
import librarian_data_selftest as librarian

ROOT = Path(__file__).resolve().parents[1]


class RecoveryUI(helpers.Fixture):
    stale = recovery.Recovery.stale
    assert_intact = recovery.Recovery.assert_intact
    view = librarian.LibrarianQt.view
    ready = librarian.LibrarianQt.ready

    def setUp(self):
        super().setUp()
        self.modules, self.dialogs, self.releases = [], [], []
        self.bus = EventBus()
        self.addCleanup(self.shutdown)
        self.folder = Path(self.temp.name) / 'colección ñ & artista'
        self.folder.mkdir()
        self.path = self.folder / 'image.png'
        self.path.write_bytes(b'unchanged image')
        self.register(self.path.as_posix())
        export_curation(self.service.db_path, self.folder)
        self.published = (self.folder / NAME).read_bytes()
        self.image = self.path.read_bytes()
        self.central_path = self.path.as_posix()
        self.central = self.service.read('get_curation_restore_state', self.central_path)

    def shutdown(self):
        for event in self.releases:
            event.set()
        for dialog in self.dialogs:
            if not dialog._closed:
                dialog.close()
            thread = dialog.reader._thread
            if thread is not None:
                thread.join(3)
                self.assertFalse(thread.is_alive())
        librarian.LibrarianQt.shutdown(self)

    def dialog(self, *, locale='es', theme=None, folder=None):
        values = json.loads((ROOT / f'locales/{locale}.json').read_bytes())
        dialog = CurationRecoveryDialog(str(folder or self.folder), lambda k, d: values.get(k, d), theme)
        self.dialogs.append(dialog)
        dialog.show()
        return dialog

    def wait(self, dialog):
        self.assertTrue(helpers.pump(lambda: not dialog._busy))

    def test_inspection_shows_all_five_resources_and_never_moves(self):
        self.stale(journals=True)
        before = {p.name: p.read_bytes() for p in self.folder.iterdir()}
        expected = inspect_recovery(self.folder)
        dialog = self.dialog()
        self.wait(dialog)
        self.assertEqual(dialog.table.rowCount(), 5)
        self.assertEqual([dialog.table.item(i, 0).text() for i in range(5)], [r['name'] for r in expected['files']])
        self.assertEqual([dialog.table.item(i, 1).text() for i in range(5)], [str(r['size']) for r in expected['files']])
        self.assertIn(expected['fingerprint'], dialog.identity.text())
        self.assertIn(expected['token'], dialog.identity.text())
        self.assertEqual(dialog.heading.text(), str(self.folder))
        self.assertTrue(dialog.quarantine_button.isEnabled())
        self.assertEqual({p.name: p.read_bytes() for p in self.folder.iterdir()}, before)
        self.assert_intact()

    def test_explicit_quarantine_keeps_bytes_and_unclaimed_files(self):
        self.stale(journals=True)
        unrelated = self.folder / (PREFIX + 'unclaimed.tmp')
        unrelated.write_bytes(b'unclaimed')
        cherry = self.folder / 'catalog.db'
        cherry.write_bytes(b'foreign Cherry catalog')
        dialog = self.dialog()
        self.wait(dialog)
        records = inspect_recovery(self.folder)['files']
        before = {r['name']: (self.folder / r['name']).read_bytes() for r in records}
        dialog.quarantine()
        self.wait(dialog)
        target, = list(self.folder.glob('.panopticon.curation.recovery-*'))
        self.assertIn(str(target), dialog.status.text())
        self.assertTrue((target / 'recovery.json').is_file())
        for name, raw in before.items():
            self.assertEqual((target / name).read_bytes(), raw)
            self.assertFalse((self.folder / name).exists())
        self.assertFalse(dialog.quarantine_button.isEnabled())
        self.assertIsNone(dialog._fingerprint)
        self.assertEqual(cherry.read_bytes(), b'foreign Cherry catalog')
        self.assertEqual(unrelated.read_bytes(), b'unclaimed')
        self.assert_intact()
        dialog.inspect()
        self.wait(dialog)
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertFalse(dialog.quarantine_button.isEnabled())

    def test_clean_folder_does_not_approve_unclaimed_temporary(self):
        path = self.folder / (PREFIX + 'orphan.tmp')
        path.write_bytes(b'unknown')
        dialog = self.dialog()
        self.wait(dialog)
        self.assertFalse(dialog.quarantine_button.isEnabled())
        self.assertIn('No se encontró', dialog.status.text())
        dialog.quarantine()
        self.assertEqual(path.read_bytes(), b'unknown')
        self.assertEqual(list(self.folder.glob('.panopticon.curation.recovery-*')), [])

    def test_stale_fingerprint_disables_until_new_inspection(self):
        path = self.stale()
        dialog = self.dialog()
        self.wait(dialog)
        previous = dialog._fingerprint
        path.write_bytes(b'changed since review')
        dialog.quarantine()
        self.wait(dialog)
        self.assertIn('cambiaron', dialog.status.text())
        self.assertFalse(dialog.quarantine_button.isEnabled())
        self.assertIsNone(dialog._fingerprint)
        self.assertTrue((self.folder / LOCK).exists())
        self.assertEqual(list(self.folder.glob('.panopticon.curation.recovery-*')), [])
        dialog.inspect()
        self.wait(dialog)
        self.assertNotEqual(dialog._fingerprint, previous)
        self.assertTrue(dialog.quarantine_button.isEnabled())

    def test_active_export_rejects_inspection_without_waiting(self):
        self.stale()
        with export_guard(self.folder):
            before = time.monotonic()
            dialog = self.dialog()
            self.wait(dialog)
            self.assertLess(time.monotonic() - before, .5)
            self.assertIn('activa', dialog.status.text())
            self.assertFalse(dialog.quarantine_button.isEnabled())
        self.assertTrue((self.folder / LOCK).is_file())

    def test_active_export_after_review_rejects_quarantine(self):
        self.stale()
        dialog = self.dialog()
        self.wait(dialog)
        with export_guard(self.folder):
            dialog.quarantine()
            self.wait(dialog)
            self.assertIn('activa', dialog.status.text())
            self.assertFalse(dialog.quarantine_button.isEnabled())
        self.assertTrue((self.folder / LOCK).is_file())

    def test_ambiguous_legacy_lock_preserved_with_manual_review_message(self):
        (self.folder / LOCK).write_bytes(b'')
        dialog = self.dialog()
        self.wait(dialog)
        self.assertIn('revisión manual', dialog.status.text())
        self.assertFalse(dialog.quarantine_button.isEnabled())
        self.assertEqual((self.folder / LOCK).read_bytes(), b'')

    def test_partial_move_keeps_plan_and_does_not_claim_success(self):
        self.stale(journals=True)
        dialog = self.dialog()
        self.wait(dialog)
        original = Path.rename
        calls = []
        def rename(path, target):
            calls.append(path.name)
            if len(calls) == 2:
                raise OSError('injected rename failure')
            return original(path, target)
        with patch.object(Path, 'rename', rename):
            dialog.quarantine()
            self.wait(dialog)
        self.assertIn('incompleta', dialog.status.text())
        self.assertNotIn('Recursos conservados en', dialog.status.text())
        self.assertFalse(dialog.quarantine_button.isEnabled())
        target, = list(self.folder.glob('.panopticon.curation.recovery-*'))
        self.assertIn(target.name, dialog.status.text())
        self.assertTrue((target / 'recovery.json').exists())
        self.assertTrue((self.folder / LOCK).exists())
        dialog.inspect()
        self.wait(dialog)
        self.assertEqual(dialog.table.rowCount(), 4)
        dialog.quarantine()
        self.wait(dialog)
        self.assertEqual(len(list(self.folder.glob('.panopticon.curation.recovery-*'))), 2)
        self.assert_intact()

    def hold(self, target):
        import core.curation_recovery_tasks as tasks
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = getattr(tasks, target)
        calls = []
        def held(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), threading.main_thread().ident)
            calls.append((args, kwargs))
            entered.set()
            self.assertTrue(release.wait(3))
            return original(*args, **kwargs)
        return entered, release, calls, patch.object(tasks, target, held)

    def test_inspection_does_not_block_qt_or_double_start(self):
        self.stale()
        entered, release, calls, hook = self.hold('inspect_recovery')
        with hook:
            before = time.monotonic()
            dialog = self.dialog()
            self.assertLess(time.monotonic() - before, .3)
            self.assertTrue(entered.wait(1))
            dialog.inspect()
            dialog.quarantine()
            ticks = []
            QTimer.singleShot(0, lambda: ticks.append(True))
            self.assertTrue(helpers.pump(lambda: ticks))
            release.set()
            self.wait(dialog)
        self.assertEqual(len(calls), 1)

    def test_quarantine_uses_shown_fingerprint_and_ignores_double_start(self):
        self.stale()
        dialog = self.dialog()
        self.wait(dialog)
        shown = dialog._fingerprint
        entered, release, calls, hook = self.hold('quarantine_recovery')
        with hook:
            dialog.quarantine()
            self.assertTrue(entered.wait(1))
            dialog.quarantine()
            dialog.inspect()
            ticks = []
            QTimer.singleShot(0, lambda: ticks.append(True))
            self.assertTrue(helpers.pump(lambda: ticks))
            release.set()
            self.wait(dialog)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ((str(self.folder),), {'expected_fingerprint': shown}))

    def test_close_before_task_starts_cancels_without_io(self):
        self.stale()
        dialog = self.dialog()
        self.wait(dialog)
        original = CurationRecoveryTasks.read
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def held(tasks, operation, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(tasks, operation, **kwargs)
        with patch.object(CurationRecoveryTasks, 'read', held):
            dialog.quarantine()
            self.assertTrue(entered.wait(1))
            dialog.close()
            release.set()
            dialog.reader._thread.join(3)
        self.assertTrue((self.folder / LOCK).exists())
        self.assertEqual(list(self.folder.glob('.panopticon.curation.recovery-*')), [])

    def test_close_after_task_starts_continues_without_late_delivery(self):
        self.stale()
        dialog = self.dialog()
        self.wait(dialog)
        delivered = []
        dialog.reader.result.connect(lambda *args: delivered.append(args))
        entered, release, _, hook = self.hold('quarantine_recovery')
        with hook:
            dialog.quarantine()
            self.assertTrue(entered.wait(1))
            before = time.monotonic()
            dialog.close()
            self.assertLess(time.monotonic() - before, .3)
            release.set()
            dialog.reader._thread.join(3)
            QApplication.processEvents()
        self.assertFalse((self.folder / LOCK).exists())
        self.assertEqual(len(list(self.folder.glob('.panopticon.curation.recovery-*'))), 1)
        self.assertEqual(delivered, [])
        self.assert_intact()

    def test_folder_missing_is_reported_without_quarantine(self):
        dialog = self.dialog(folder=self.folder / 'missing')
        self.wait(dialog)
        self.assertIn('no está disponible', dialog.status.text())
        self.assertFalse(dialog.quarantine_button.isEnabled())

    def test_folder_choice_not_current_library_view_and_only_one_dialog(self):
        self.stale()
        module = self.view()
        self.ready(module)
        self.assertIsNone(module.current_folder_path)
        with patch.object(QFileDialog, 'getExistingDirectory', return_value=str(self.folder)) as choice:
            module.recover_folder_curation()
            dialog = module._recovery_dialog
            self.dialogs.append(dialog)
            self.wait(dialog)
            module.current_folder_path = 'C:/different/view'
            module.recover_folder_curation()
            self.assertIs(module._recovery_dialog, dialog)
            self.assertEqual(choice.call_count, 1)
            self.assertEqual(dialog.folder, str(self.folder))
        self.assert_intact()

    def test_cancel_folder_choice_does_not_start_worker(self):
        module = self.view()
        self.ready(module)
        with patch.object(QFileDialog, 'getExistingDirectory', return_value=''), \
                patch.object(CurationRecoveryTasks, 'read', side_effect=AssertionError('unexpected I/O')):
            module.recover_folder_curation()
            self.assertIsNone(module._recovery_dialog)

    def test_module_unload_closes_recovery_and_drops_results(self):
        module = self.view()
        self.ready(module)
        entered, release, _, hook = self.hold('inspect_recovery')
        with hook, patch.object(QFileDialog, 'getExistingDirectory', return_value=str(self.folder)):
            module.recover_folder_curation()
            dialog = module._recovery_dialog
            self.dialogs.append(dialog)
            self.assertTrue(entered.wait(1))
            module.on_unload()
            self.assertTrue(dialog._closed)
            release.set()
            dialog.reader._thread.join(3)
        self.assert_intact()

    def test_bilingual_controls_tooltips_and_error_keys(self):
        values = {lang: json.loads((ROOT / f'locales/{lang}.json').read_bytes()) for lang in ('en', 'es')}
        keys = {k for k in values['en'] if k.startswith('lib.recovery.')}
        self.assertEqual(keys, {k for k in values['es'] if k.startswith('lib.recovery.')})
        for lang in ('en', 'es'):
            with self.subTest(lang=lang):
                dialog = self.dialog(locale=lang)
                self.wait(dialog)
                for button in dialog.findChildren(QPushButton):
                    self.assertTrue(button.toolTip())
                    self.assertNotEqual(button.text(), button.toolTip())
                self.assertTrue(dialog.table.toolTip())
                self.assertEqual(dialog.windowTitle(), values[lang]['lib.recovery.title'])
                for reason in ('sidecar_export_busy', 'invalid_export_guard', 'unverifiable_export_lock',
                               'unsafe_recovery_resource', 'recovery_identity_changed', 'recovery_changed'):
                    self.assertIn('lib.recovery.error.' + reason, keys)

    def test_worker_cancellation_and_invalid_dispatch_are_pre_io(self):
        tasks = CurationRecoveryTasks()
        cancel = threading.Event()
        cancel.set()
        with patch('core.curation_recovery_tasks.inspect_recovery', side_effect=AssertionError('I/O')):
            with self.assertRaisesRegex(ValueError, 'recovery_cancelled'):
                tasks.read('inspect_recovery', folder=str(self.folder), cancel_event=cancel)
            with self.assertRaisesRegex(ValueError, 'unknown_recovery_task'):
                tasks.read('bogus', folder=str(self.folder))
            with self.assertRaisesRegex(ValueError, 'invalid_export_folder'):
                tasks.read('inspect_recovery', folder='')


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
