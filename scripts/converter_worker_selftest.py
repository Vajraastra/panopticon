"""Lifecycle and failure regressions for the complete conversion/verification job."""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from core.metadata.batch_verifier import BatchVerificationReport
from modules.format_converter import module as m


class WorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication([])
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = m.FormatConverterModule()
        self.module.on_load({})
        self.module.get_view()
        self.module.files_to_convert = [Path(self.tmp.name) / 'input.png']
        self.report = m.BatchConversionReport(converted_count=1, results=[object()])
        self.info = self.enterContext(patch.object(QMessageBox, 'information'))
        self.error = self.enterContext(patch.object(QMessageBox, 'critical'))
        self.folder = self.enterContext(patch.object(
            QFileDialog, 'getExistingDirectory', return_value=self.tmp.name))

    def tearDown(self):
        self.module.on_unload()
        self.app.processEvents()
        self.module.view.close()
        self.module.view.deleteLater()
        self.module.deleteLater()
        self.tmp.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            self.app.processEvents()
            if time.monotonic() > deadline:
                self.fail('Qt operation timed out')
            time.sleep(0.005)

    def test_busy_progress_snapshot_and_single_start(self):
        entered, release = threading.Event(), threading.Event()
        observed = []
        def verify(report, progress_callback):
            observed.append(QThread.currentThread() != self.app.thread())
            progress_callback(0, 1)
            entered.set()
            if not release.wait(5):
                raise TimeoutError('fixture not released')
            progress_callback(1, 1)
            return BatchVerificationReport(total_files=1, ok_count=1)
        with patch.object(m, 'convert_batch', return_value=self.report) as convert, \
             patch.object(m, 'verify_batch_conversion', side_effect=verify):
            self.module.start_conversion()
            self.module.chk_verify.setChecked(False)  # job already captured its options
            try:
                self.wait_for(entered.is_set)
                self.wait_for(lambda: '0/1' in self.module.lbl_current_file.text())
                self.assertFalse(self.module.btn_convert.isEnabled())
                self.assertFalse(self.module.drop_zone.isEnabled())
                self.assertFalse(self.module.combo_format.isEnabled())
                self.module.start_conversion()
                self.module._refresh_queue()
                self.assertFalse(self.module.btn_convert.isEnabled())
                self.module.view.hide()
                self.module.view.show()
                self.assertTrue(self.module.worker_thread.isRunning())
                self.info.assert_not_called()
            finally:
                release.set()
            self.wait_for(lambda: self.info.called)
            self.assertEqual(observed, [True])
            convert.assert_called_once()
            self.folder.assert_called_once()
            self.assertIn('1/1', self.module.txt_log.toPlainText())
            self.assertFalse(self.module.worker_thread.isRunning())
            self.assertTrue(self.module.combo_format.isEnabled())

    def test_verification_exception_keeps_conversion_result(self):
        with patch.object(m, 'convert_batch', return_value=self.report), \
             patch.object(m, 'verify_batch_conversion', side_effect=OSError('probe failure')):
            self.module.start_conversion()
            self.wait_for(lambda: self.info.called)
        self.assertIn('Verification error: probe failure', self.module.txt_log.toPlainText())
        self.assertFalse(self.module._busy)
        self.error.assert_not_called()

    def test_conversion_exception_releases_controls_and_can_retry(self):
        with patch.object(m, 'convert_batch', side_effect=OSError('convert failure')):
            self.module.start_conversion()
            self.wait_for(lambda: self.error.called)
        self.assertFalse(self.module._busy)
        self.assertFalse(self.module.worker_thread.isRunning())
        self.module.chk_verify.setChecked(False)
        with patch.object(m, 'convert_batch', return_value=self.report):
            self.module.start_conversion()
            self.wait_for(lambda: self.info.called)

    def test_verification_disabled_and_empty_batch(self):
        for enabled, results in ((False, [object()]), (True, [])):
            self.info.reset_mock()
            self.module.chk_verify.setChecked(enabled)
            report = m.BatchConversionReport(results=results)
            with patch.object(m, 'convert_batch', return_value=report), \
                 patch.object(m, 'verify_batch_conversion') as verify:
                self.module.start_conversion()
                self.wait_for(lambda: self.info.called)
                verify.assert_not_called()

    def test_unload_interrupts_verification_without_gui_callbacks(self):
        entered = threading.Event()
        def verify(report, progress_callback):
            entered.set()
            for _ in range(500):
                time.sleep(0.01)
                progress_callback(1, 2)
            self.fail('Interruption never requested')
        with patch.object(m, 'convert_batch', return_value=self.report), \
             patch.object(m, 'verify_batch_conversion', side_effect=verify):
            self.module.start_conversion()
            self.wait_for(entered.is_set)
            started = time.monotonic()
            self.module.on_unload()
            self.assertLess(time.monotonic() - started, 1)
            self.assertTrue(self.module.worker_thread.wait(0))
            self.app.processEvents()
        self.info.assert_not_called()
        self.error.assert_not_called()

    def test_failed_and_repaired_counts_preserved(self):
        verified = BatchVerificationReport(total_files=3, ok_count=1,
                                            repaired_count=1, failed_count=1)
        with patch.object(m, 'convert_batch', return_value=self.report), \
             patch.object(m, 'verify_batch_conversion', return_value=verified):
            self.module.start_conversion()
            self.wait_for(lambda: self.info.called)
        self.assertIn('2/3', self.module.txt_log.toPlainText())

    def test_real_conversion_then_real_verification(self):
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo
        source = Path(self.tmp.name) / 'input.png'
        info = PngInfo()
        info.add_text('parameters', 'test\nSteps: 20, Seed: 42')
        Image.new('RGB', (16, 16), 'blue').save(source, pnginfo=info)
        before = source.read_bytes()
        self.module.combo_format.setCurrentIndex(1)
        self.module.files_to_convert = [source]
        self.module.start_conversion()
        self.wait_for(lambda: self.info.called or self.error.called)
        self.error.assert_not_called()
        self.assertIn('1/1', self.module.txt_log.toPlainText())
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(len(list(Path(self.tmp.name).glob('*.webp'))), 1)


if __name__ == '__main__':
    unittest.main()
