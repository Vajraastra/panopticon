"""Private Qt/file/Native tests for one-worker scheduling and cooperative close."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PIL import Image
from PySide6.QtCore import QThread, QTimer, QCoreApplication, QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QFileDialog
from core.library_service import LibraryService
from core.paths import CachePaths
from core.native_threads import native_threads_ready
from core.theme_manager import ThemeManager
from modules.watermarker.module import WatermarkerModule
from modules.watermarker.logic import worker as workers
from modules.watermarker.logic import watermarker as logic


def pump(predicate, timeout=4):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        if predicate():
            return True
        time.sleep(.005)
    return False


class WatermarkerQt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(setattr, CachePaths, '_cache_root', CachePaths._cache_root)
        CachePaths.set_cache_root(self.root/'cache')
        self.service = LibraryService(self.root/'private.db')
        self.addCleanup(self.service.close)
        self.source, self.asset, self.output = (self.root/name for name in ('source.png','asset.png','output.png'))
        with Image.new('RGBA',(80,64),'green') as image:
            image.save(self.source)
        with Image.new('RGBA',(8,8),'red') as image:
            image.save(self.asset)
        self.releases, self.modules = [], []
        self.addCleanup(self.shutdown)
        self.info = self.enterContext(patch.object(QMessageBox,'information',return_value=QMessageBox.Ok))
        self.warning = self.enterContext(patch.object(QMessageBox,'warning',return_value=QMessageBox.Ok))
        self.open_folder = self.enterContext(patch.object(CachePaths,'open_folder'))

    def module(self, view=True):
        module = WatermarkerModule()
        module.on_load(dict(theme_manager=ThemeManager(),data_service=self.service))
        self.modules.append(module)
        if view:
            module.get_view()
        return module

    def select(self,module):
        module.source_image, module.watermark_asset = str(self.source),str(self.asset)
        module.generate_preview()
        module._preview_timer.stop()
        module._launch_next()

    def settle(self,module):
        self.assertTrue(pump(lambda:module._worker is None and module._preview_pending is None
                            and module._save_pending is None and module.shutdown_ready()))

    def shutdown(self):
        for release in self.releases:
            release.set()
        for module in self.modules:
            module.on_unload()
            self.assertTrue(pump(lambda:module._worker is None and module.shutdown_ready()))
            if module.view is not None:
                module.view.close()
                module.view.deleteLater()
        self.assertTrue(pump(native_threads_ready))

    def block_preview(self):
        entered,release = threading.Event(),threading.Event()
        self.releases.append(release)
        original = workers.render_preview
        calls = []
        def render(**kwargs):
            calls.append(kwargs.copy())
            self.assertIsNot(QThread.currentThread(),self.app.thread())
            if len(calls) == 1:
                entered.set()
                if not release.wait(4):
                    raise TimeoutError('fixture preview gate')
            return original(**kwargs)
        return entered,release,calls,patch.object(workers,'render_preview',render)

    def save(self,module,output=None):
        with patch.object(QFileDialog,'getSaveFileName',return_value=(str(output or self.output),'')):
            module.save_copy()

    def test_render_decode_and_preview_image_live_off_gui(self):
        seen = []
        original = logic._load_asset
        def load(*args,**kwargs):
            seen.append(QThread.currentThread() is not self.app.thread())
            return original(*args,**kwargs)
        with patch.object(logic,'_load_asset',load):
            module = self.module()
            self.select(module)
            self.settle(module)
        self.assertTrue(seen and all(seen))
        self.assertFalse(module.lbl_preview.pixmap().isNull())
        self.assertLessEqual(module.lbl_preview.pixmap().width(),1600)
        self.assertEqual(list(self.root.glob('.panopticon-watermark-*')),[])

    def test_burst_has_one_running_and_only_latest_preview_pending(self):
        entered,release,calls,context = self.block_preview()
        with context:
            module = self.module()
            self.select(module)
            self.assertTrue(pump(entered.is_set))
            first = module._worker
            for opacity in range(40,81):
                module.slider_opacity.setValue(opacity)
            self.assertIs(module._worker,first)
            self.assertTrue(first.cancel_event.is_set())
            self.assertEqual(module._preview_pending[1]['wm_opacity'],.8)
            self.assertEqual(len(calls),1)
            release.set()
            self.settle(module)
            self.assertEqual(len(calls),2)
            self.assertEqual(calls[-1]['wm_opacity'],.8)
            self.assertFalse(module.lbl_preview.pixmap().isNull())

    def test_qt_timer_ticks_while_decoder_is_blocked(self):
        entered,release,calls,context = self.block_preview()
        timer,ticks = QTimer(),[]
        timer.setInterval(5)
        timer.timeout.connect(lambda:ticks.append(True))
        self.addCleanup(timer.stop)
        with context:
            module = self.module()
            self.select(module)
            self.assertTrue(pump(entered.is_set))
            timer.start()
            self.assertTrue(pump(lambda:len(ticks)>=5))
            self.assertFalse(module.shutdown_ready())
            release.set()
            self.settle(module)

    def test_changed_source_drops_old_preview_and_latest_is_displayed(self):
        other = self.root/'blue.png'
        with Image.new('RGBA',(80,64),'blue') as image:
            image.save(other)
        entered,release,calls,context = self.block_preview()
        with context:
            module = self.module()
            self.select(module)
            self.assertTrue(pump(entered.is_set))
            module.watermark_asset = None
            module.load_image_set([str(other)])
            self.assertTrue(module.lbl_preview.pixmap().isNull())
            release.set()
            self.settle(module)
        self.assertEqual(calls[-1]['source_path'],str(other))
        self.assertEqual(module.lbl_preview.pixmap().toImage().pixelColor(0,0).name(),'#0000ff')

    def test_save_waits_for_preview_then_reopens_latest_without_overlap(self):
        entered,release,calls,context = self.block_preview()
        original = workers.process_image
        saves = []
        def save(**kwargs):
            self.assertTrue(release.is_set())
            self.assertIsNot(QThread.currentThread(),self.app.thread())
            saves.append(kwargs.copy())
            return original(**kwargs)
        with context,patch.object(workers,'process_image',save):
            module = self.module()
            self.select(module)
            self.assertTrue(pump(entered.is_set))
            self.save(module)
            self.assertIsNotNone(module._save_pending)
            self.assertFalse(module.btn_save.isEnabled())
            module.slider_opacity.setValue(80)
            release.set()
            self.settle(module)
        self.assertEqual(len(saves),1)
        self.assertEqual(saves[0]['wm_opacity'],.3)
        self.assertEqual(calls[-1]['wm_opacity'],.8)
        self.assertTrue(self.output.exists())
        self.info.assert_called_once()
        self.open_folder.assert_called_once()

    def test_save_dialog_reentry_is_rejected(self):
        module = self.module()
        module.source_image,module.watermark_asset = str(self.source),str(self.asset)
        def dialog(*args,**kwargs):
            module.save_copy()
            return str(self.output),''
        with patch.object(QFileDialog,'getSaveFileName',side_effect=dialog) as chooser:
            module.save_copy()
        self.settle(module)
        chooser.assert_called_once()
        self.info.assert_called_once()
        self.assertTrue(self.output.exists())

    def test_save_dialog_changed_options_require_fresh_action(self):
        module = self.module()
        module.source_image,module.watermark_asset = str(self.source),str(self.asset)
        def change(*args,**kwargs):
            module.slider_opacity.setValue(80)
            return str(self.output),''
        with patch.object(QFileDialog,'getSaveFileName',side_effect=change):
            module.save_copy()
        self.settle(module)
        self.assertFalse(self.output.exists())
        self.assertIsNone(module._save_pending)
        self.warning.assert_called_once()
        self.info.assert_not_called()

    def test_existing_destination_failure_never_reports_success(self):
        module = self.module()
        module.source_image,module.watermark_asset = str(self.source),str(self.asset)
        self.output.write_bytes(b'curated winner')
        self.save(module)
        self.settle(module)
        self.assertEqual(self.output.read_bytes(),b'curated winner')
        self.warning.assert_called_once()
        self.info.assert_not_called()
        self.open_folder.assert_not_called()
        self.assertTrue(module.btn_save.isEnabled())

    def test_preview_error_is_terminal_and_explicit_retry_works(self):
        with patch.object(workers,'render_preview',side_effect=OSError('private decode failure')):
            module = self.module()
            self.select(module)
            self.settle(module)
        self.assertIn('private decode failure',module.lbl_preview.text())
        module.generate_preview()
        self.settle(module)
        self.assertFalse(module.lbl_preview.pixmap().isNull())

    def test_start_failure_releases_admission_and_retry_works(self):
        with patch.object(workers.WatermarkWorker,'start',side_effect=RuntimeError('start failure')):
            module = self.module()
            self.select(module)
        self.assertIsNone(module._worker)
        self.assertTrue(module.shutdown_ready())
        self.assertIn('start failure',module.lbl_preview.text())
        module.generate_preview()
        self.settle(module)
        self.assertFalse(module.lbl_preview.pixmap().isNull())

    def test_unload_cancels_preview_and_pending_save_without_blocking_qt(self):
        entered,release,calls,context = self.block_preview()
        with context:
            module = self.module()
            self.select(module)
            self.assertTrue(pump(entered.is_set))
            self.save(module)
            started = time.monotonic()
            module.on_unload()
            self.assertLess(time.monotonic()-started,.2)
            self.assertFalse(module.shutdown_ready())
            self.assertIsNone(module._save_pending)
            module.generate_preview()
            module.save_copy()
            release.set()
            self.settle(module)
        self.assertFalse(self.output.exists())
        self.info.assert_not_called()
        self.warning.assert_not_called()

    def test_destroyed_view_retains_running_worker_and_drops_widget_access(self):
        entered,release,calls,context = self.block_preview()
        with context:
            module = self.module()
            self.select(module)
            self.assertTrue(pump(entered.is_set))
            worker = module._worker
            module.view.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
            self.assertIsNone(module.view)
            self.assertTrue(module._closed)
            self.assertIs(module._worker,worker)
            self.assertFalse(module.shutdown_ready())
            release.set()
            self.settle(module)
        self.info.assert_not_called()
        self.warning.assert_not_called()

    def test_unload_admitted_save_before_publication_cancels_without_output(self):
        entered,release = threading.Event(),threading.Event()
        self.releases.append(release)
        original = workers.process_image
        def save(**kwargs):
            entered.set()
            if not release.wait(4):
                raise TimeoutError('fixture save gate')
            return original(**kwargs)
        with patch.object(workers,'process_image',save):
            module = self.module()
            module.source_image,module.watermark_asset = str(self.source),str(self.asset)
            self.save(module)
            self.assertTrue(pump(entered.is_set))
            module.on_unload()
            self.assertFalse(module.shutdown_ready())
            release.set()
            self.settle(module)
        self.assertFalse(self.output.exists())
        self.info.assert_not_called()
        self.warning.assert_not_called()

    def test_unload_after_publication_preserves_output_and_waits_native_exit(self):
        entered,release = threading.Event(),threading.Event()
        self.releases.append(release)
        operation = 'rename' if os.name=='nt' else 'link'
        original = getattr(os,operation)
        def publish(source,destination):
            original(source,destination)
            entered.set()
            if not release.wait(4):
                raise TimeoutError('fixture published gate')
        with patch.object(logic.os,operation,publish):
            module = self.module()
            module.source_image,module.watermark_asset = str(self.source),str(self.asset)
            self.save(module)
            self.assertTrue(pump(entered.is_set))
            self.assertTrue(self.output.exists())
            module.on_unload()
            self.assertFalse(module.shutdown_ready())
            release.set()
            self.settle(module)
        with Image.open(self.output) as image:
            image.load()
            self.assertFalse(image.getexif())
        self.info.assert_not_called()
        self.warning.assert_not_called()
        self.open_folder.assert_not_called()

    def test_load_image_set_before_get_view_stays_lazy_and_then_previews(self):
        module = self.module(view=False)
        module.load_image_set([str(self.source)])
        self.assertIsNone(module.view)
        module.get_view()
        self.settle(module)
        self.assertEqual(module.lbl_image_status.text(),self.source.name)
        self.assertFalse(module.lbl_preview.pixmap().isNull())

    def test_closed_during_save_dialog_does_not_start_new_work(self):
        module = self.module()
        module.source_image,module.watermark_asset = str(self.source),str(self.asset)
        def close(*args,**kwargs):
            module.on_unload()
            return str(self.output),''
        with patch.object(QFileDialog,'getSaveFileName',side_effect=close):
            module.save_copy()
        self.assertTrue(module.shutdown_ready())
        self.assertIsNone(module._worker)
        self.assertFalse(self.output.exists())

    def test_svg_asset_is_painted_in_the_worker(self):
        svg = self.root/'mark.svg'
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8" fill="red"/></svg>')
        seen = []
        original = logic._load_asset
        def load(path,*args,**kwargs):
            if Path(path).suffix=='.svg':
                seen.append(QThread.currentThread() is not self.app.thread())
            return original(path,*args,**kwargs)
        with patch.object(logic,'_load_asset',load):
            module = self.module()
            module.source_image,module.watermark_asset = str(self.source),str(svg)
            module.generate_preview()
            self.settle(module)
        self.assertEqual(seen,[True])
        self.assertFalse(module.lbl_preview.pixmap().isNull())

    def test_unexpected_save_error_is_terminal_and_can_retry(self):
        module = self.module()
        module.source_image,module.watermark_asset = str(self.source),str(self.asset)
        with patch.object(workers,'process_image',side_effect=RuntimeError('unexpected private error')):
            self.save(module)
            self.settle(module)
        self.warning.assert_called_once()
        self.info.assert_not_called()
        self.assertFalse(self.output.exists())
        self.save(module)
        self.settle(module)
        self.assertTrue(self.output.exists())
        self.info.assert_called_once()

    def test_save_result_waits_for_real_qt_final_cleanup(self):
        entered,release = threading.Event(),threading.Event()
        self.releases.append(release)
        class CleaningWorker(workers.WatermarkWorker):
            def run(worker):
                super().run()
                resource = QObject()
                def cleanup():
                    entered.set()
                    release.wait(4)
                resource.destroyed.connect(cleanup,Qt.DirectConnection)
                worker._cleanup_resource = resource
                resource.deleteLater()
        with patch('modules.watermarker.module.WatermarkWorker',CleaningWorker):
            module = self.module()
            module.source_image,module.watermark_asset = str(self.source),str(self.asset)
            self.save(module)
            self.assertTrue(pump(entered.is_set))
            self.assertTrue(module._worker.isFinished())
            self.assertFalse(module._worker.wait(0))
            self.assertFalse(module.shutdown_ready())
            self.assertTrue(self.output.exists())
            self.info.assert_not_called()
            release.set()
            self.settle(module)
        self.info.assert_called_once()


if __name__=='__main__':
    unittest.main(verbosity=2)
