"""Qt offscreen: confirmación capturada, trabajo asíncrono y cierre con fixtures privadas."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
from concurrent.futures import Future

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QEvent, QCoreApplication
from PySide6.QtWidgets import QApplication, QMessageBox, QFileDialog, QPushButton, QTextEdit
from core.theme_manager import ThemeManager
from modules.metadata.module import MetadataModule, _LIVE_WORKERS
from modules.metadata.logic.worker import MetadataWorker
import scripts.metadata_files_selftest as fixtures

APP = QApplication.instance() or QApplication([])
APP.setStyle('Fusion')


def spin(condition, seconds=10):
    until = time.monotonic()+seconds
    while not condition():
        APP.processEvents()
        if time.monotonic() >= until: raise AssertionError('fixture Qt timeout')
        time.sleep(.005)
    APP.processEvents()


class Locale:
    def __init__(self, language='es'):
        self.values = json.loads((Path(__file__).resolve().parents[1]/'locales'/f'{language}.json').read_bytes())
    def tr(self, key, default=None): return self.values.get(key, default or key)


class UI(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Files()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.module = MetadataModule()
        self.theme = ThemeManager(str(self.fixture.root/'theme.json'))
        self.locale = Locale()
        self.module.on_load(dict(data_service=self.fixture.service, theme_manager=self.theme,
            locale_manager=self.locale, metadata_journal_root=self.fixture.root/'recovery'))
        self.addCleanup(self.close)

    def close(self):
        self.module.on_unload()
        spin(self.module.shutdown_ready)
        APP.processEvents()
        if self.module.view is not None: self.module.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        APP.processEvents()

    def view(self):
        with patch('core.components.standard_layout.LocaleManager', return_value=self.locale):
            return self.module.get_view()

    def load(self):
        self.view()
        self.module.load_image_set([str(self.fixture.source)])
        spin(lambda: not self.module._busy)
        self.assertIsNotNone(self.module._capture)
        self.module.set_mode(self.module.MODE_EDITOR)

    def edit_fields(self):
        self.module.txt_pos.setPlainText('Qt edited ñ')
        self.module.txt_neg.setPlainText('bad')

    def test_lazy_view_and_no_read_ddl(self):
        self.assertIsNone(self.module.view)
        self.assertTrue(self.module.shutdown_ready())
        self.view()
        self.assertFalse(self.module.btn_save.isEnabled())
        self.assertFalse(self.fixture.service.read('get_generation_history_status')['prepared'])

    def test_async_load_and_save_enable_after_native_finished(self):
        self.load()
        self.assertTrue(self.module.btn_save.isEnabled())
        self.assertEqual(self.module.txt_pos.toPlainText(), 'old prompt')
        self.assertFalse(self.module.image_label._pixmap.isNull())
        self.assertEqual(self.module._workers, set())

    def test_controls_have_real_localized_tooltips(self):
        view = self.view()
        for kind in (QPushButton, QTextEdit):
            for widget in view.findChildren(kind): self.assertTrue(widget.toolTip().strip(), widget.objectName())
        self.assertIn('respaldo', self.module.btn_save.toolTip())
        self.assertIn('AVIF', self.module.btn_export.toolTip())
        self.assertEqual(self.module.btn_history.text(), 'Historial de generación')

    def test_default_cancel_plain_concrete_copy_confirmation(self):
        self.load()
        self.edit_fields()
        def cancel(box):
            self.assertEqual(box.standardButton(box.defaultButton()), QMessageBox.Cancel)
            self.assertEqual(box.textFormat(), Qt.PlainText)
            self.assertIn(str(self.fixture.source), box.text())
            self.assertIn(str(self.fixture.key), box.text())
            return QMessageBox.Cancel
        with patch.object(QMessageBox, 'exec', cancel): self.module.action_save_current()
        self.assertFalse(self.module._busy)
        self.assertEqual(self.fixture.source.read_bytes(), self.fixture.original)

    def test_whitespace_prompts_show_clean_warning(self):
        self.load()
        self.module.txt_pos.setPlainText('  ')
        self.module.txt_neg.setPlainText('\n')
        def cancel(box):
            self.assertIn('Eliminar la metadata', box.text())
            return QMessageBox.Cancel
        with patch.object(QMessageBox, 'exec', cancel): self.module.action_save_current()

    def test_save_worker_updates_native_then_requires_reload(self):
        self.load()
        self.edit_fields()
        with patch.object(QMessageBox, 'exec', return_value=QMessageBox.Save): self.module.action_save_current()
        self.assertTrue(self.module._busy)
        self.assertFalse(self.module.btn_save.isEnabled())
        spin(lambda: not self.module._busy)
        self.assertEqual(self.fixture.state()['state']['file']['meta_positive'], 'Qt edited ñ')
        self.assertIsNone(self.module._capture)
        self.assertFalse(self.module.btn_save.isEnabled())
        self.module.display_current()
        spin(lambda: not self.module._busy)
        self.assertEqual(self.module.txt_pos.toPlainText(), 'Qt edited ñ')
        self.assertTrue(self.module.btn_save.isEnabled())

    def test_prompt_changed_in_modal_cancels_captured_save(self):
        self.load()
        self.edit_fields()
        def change(box):
            self.module.txt_pos.setPlainText('changed in modal')
            return QMessageBox.Save
        with patch.object(QMessageBox, 'exec', change): self.module.action_save_current()
        self.assertFalse(self.module._busy)
        self.assertEqual(self.fixture.source.read_bytes(), self.fixture.original)

    def test_mode_changed_in_modal_cancels_save(self):
        self.load()
        def change(box):
            self.module.set_mode(self.module.MODE_VIEWER)
            return QMessageBox.Save
        with patch.object(QMessageBox, 'exec', change): self.module.action_save_current()
        self.assertFalse(self.module._busy)
        self.assertEqual(self.fixture.source.read_bytes(), self.fixture.original)

    def test_close_in_modal_prevents_save(self):
        self.load()
        def close(box):
            self.module.on_unload()
            return QMessageBox.Save
        with patch.object(QMessageBox, 'exec', close): self.module.action_save_current()
        self.assertFalse(self.module._busy)
        self.assertEqual(self.fixture.source.read_bytes(), self.fixture.original)

    def test_export_same_path_cannot_bypass_original_warning(self):
        self.load()
        self.edit_fields()
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(self.fixture.source), '')):
            self.module.action_export_copy()
        spin(lambda: not self.module._busy)
        self.assertEqual(self.fixture.source.read_bytes(), self.fixture.original)
        self.assertIn('export_requires_new_path', self.module.txt_tech.toPlainText())

    def test_stale_export_modal_discards_action(self):
        self.load()
        def choose(*args):
            self.module.txt_neg.setPlainText('changed in chooser')
            return str(self.fixture.root/'new.png'), ''
        with patch.object(QFileDialog, 'getSaveFileName', side_effect=choose): self.module.action_export_copy()
        self.assertFalse(self.module._busy)
        self.assertFalse((self.fixture.root/'new.png').exists())

    def test_history_read_is_async_and_preserves_curation(self):
        result = self.fixture.edit()
        self.assertEqual(result['status'], 'complete')
        self.load()
        self.module._read_history()
        spin(lambda: not self.module._busy)
        self.assertIn('native old', self.module.txt_tech.toPlainText())
        self.assertEqual(self.fixture.db.get_file_rating(str(self.fixture.source)), 5)
        self.assertFalse(self.module.btn_history_next.isEnabled())

    def test_result_does_not_enable_controls_before_thread_exit(self):
        gate = threading.Event()
        emitted = threading.Event()
        real_run = MetadataWorker.run
        def delayed(worker):
            real_run(worker)
            emitted.set()
            gate.wait(5)
        self.view()
        with patch.object(MetadataWorker, 'run', delayed):
            self.module.load_image_set([str(self.fixture.source)])
            try:
                spin(emitted.is_set)
                APP.processEvents()
                self.assertTrue(self.module._busy)
                self.assertIsNone(self.module._capture)
                self.assertTrue(_LIVE_WORKERS)
                self.assertFalse(self.module.shutdown_ready())
            finally: gate.set()
            spin(lambda: not self.module._busy)

    def test_close_waits_for_admitted_commit_and_ignores_late_result(self):
        self.load()
        self.edit_fields()
        admitted = threading.Event()
        pending = Future()
        arguments = []
        submit = self.fixture.service.submit
        def delay(operation, *args, **kwargs):
            if operation == 'commit_metadata_edit':
                arguments.append((args, kwargs))
                admitted.set()
                return pending
            return submit(operation, *args, **kwargs)
        with patch.object(self.fixture.service, 'submit', side_effect=delay):
            with patch.object(QMessageBox, 'exec', return_value=QMessageBox.Save): self.module.action_save_current()
            try:
                spin(admitted.is_set)
                self.module.on_unload()
                self.assertFalse(self.module.shutdown_ready())
                self.assertFalse(pending.cancelled())
                args, kwargs = arguments[0]
                pending.set_result(submit('commit_metadata_edit', *args, **kwargs).result())
            finally:
                if not pending.done(): pending.set_exception(RuntimeError('fixture cleanup'))
            spin(self.module.shutdown_ready)
            APP.processEvents()
        self.assertEqual(self.fixture.state()['state']['file']['meta_positive'], 'Qt edited ñ')
        self.assertIsNone(self.module._pending_result)

    def test_partial_failure_reports_file_and_native_separately(self):
        self.load()
        self.edit_fields()
        submit = self.fixture.service.submit
        def fail(operation, *args, **kwargs):
            if operation == 'commit_metadata_edit':
                future = Future()
                future.set_exception(RuntimeError('fixture DB failure'))
                return future
            return submit(operation, *args, **kwargs)
        with patch.object(self.fixture.service, 'submit', side_effect=fail):
            with patch.object(QMessageBox, 'exec', return_value=QMessageBox.Save): self.module.action_save_current()
            spin(lambda: not self.module._busy)
        self.assertIn('necesita revisión', self.module.lbl_operation.text())
        self.assertIn('Archivo publicado: Sí', self.module.txt_tech.toPlainText())
        self.assertIn('confirmado: No', self.module.txt_tech.toPlainText())

    def test_pre_stopped_worker_does_not_touch_files(self):
        worker = MetadataWorker(self.fixture.service, self.fixture.root/'recovery', 'load', 1, path=str(self.fixture.source))
        worker.stop()
        with patch.object(worker.tasks, 'load', side_effect=AssertionError('must not load')):
            worker.run()
        worker.deleteLater()

    def test_cherry_original_save_disabled_history_and_export_available(self):
        (self.fixture.root/'catalog.db').write_bytes(b'portable')
        self.load()
        self.assertFalse(self.module.btn_save.isEnabled())
        self.assertTrue(self.module.btn_export.isEnabled())
        self.assertTrue(self.module.btn_history.isEnabled())
        self.assertIn('protegido', self.module.lbl_operation.text())


if __name__ == '__main__': unittest.main(verbosity=2)
