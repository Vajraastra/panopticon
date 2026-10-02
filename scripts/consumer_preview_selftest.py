"""Real private Qt images; verify consumer decode ownership, stale results and shutdown."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer,QCoreApplication,QEvent
from PySide6.QtGui import QImage,QColor,QPixmap
from PySide6.QtWidgets import QApplication
from core.event_bus import EventBus
from core.library_service import LibraryService
from core.components.worker_preview import WorkerPreviewDialog
from modules.image_optimizer.module import ImageOptimizerModule
from modules.librarian.module import LibrarianModule

APP=QApplication.instance() or QApplication([])


def pump(condition,seconds=5):
    deadline=time.monotonic()+seconds
    while not condition() and time.monotonic()<deadline:
        APP.processEvents();time.sleep(.002)
    if not condition():raise AssertionError('private preview deadline')


class NoFilePixmap:
    def __new__(cls,*args,**kwargs):raise AssertionError('GUI must not decode a file')
    fromImage=staticmethod(QPixmap.fromImage)


class ConsumerPreview(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.service=LibraryService(self.root/'native.db')
        self.bus=EventBus();self.modules=[];self.dialogs=[];self.releases=[]
        self.paths=[str(self.root/'red.bmp'),str(self.root/'blue.bmp')]
        for path,color in zip(self.paths,('red','blue')):self.image(path,color)

    def tearDown(self):
        for release in self.releases:release.set()
        for dialog in self.dialogs:dialog.close()
        for module in self.modules:module.on_unload()
        pump(lambda:all(module.shutdown_ready() for module in self.modules)
             and all(dialog.shutdown_ready() for dialog in self.dialogs))
        for module in self.modules:
            if module.view is not None:module.view.close();module.view.deleteLater()
            module.deleteLater()
        for dialog in self.dialogs:dialog.deleteLater()
        APP.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        self.service.close();self.temp.cleanup()

    def image(self,path,color):
        image=QImage(40,40,QImage.Format_RGB32);image.fill(QColor(color))
        self.assertTrue(image.save(path,'BMP'))

    def optimizer(self,paths=None):
        module=ImageOptimizerModule();module.on_load({'data_service':self.service,'event_bus':self.bus})
        module.get_view();self.modules.append(module)
        module.queue=list(self.paths if paths is None else paths);module._update_ui()
        return module

    def blocked(self,actual):
        entered=threading.Event();release=threading.Event();threads=[]
        self.releases.append(release)
        def load(*args,**kwargs):
            threads.append(threading.get_ident());entered.set()
            if not release.wait(4):raise TimeoutError('private release')
            return actual(*args,**kwargs)
        return load,entered,release,threads

    def test_optimizer_thumbnails_decode_off_gui_with_responsive_bounded_pool(self):
        from core.thumbnail_loader import load_thumbnail
        load,entered,release,threads=self.blocked(load_thumbnail)
        ticks=[];timer=QTimer();timer.setInterval(2);timer.timeout.connect(lambda:ticks.append(1))
        with patch('core.thumbnail_loader.load_thumbnail',side_effect=load),patch('modules.image_optimizer.module.QPixmap',NoFilePixmap):
            module=self.optimizer();timer.start()
            pump(lambda:entered.is_set() and len(ticks)>=5)
            self.assertTrue(all(thread!=threading.get_ident() for thread in threads))
            self.assertLessEqual(len(module._thumb_loader._active),4)
            module.on_unload();self.assertFalse(module.shutdown_ready())
            release.set();pump(module.shutdown_ready)
        timer.stop()

    def test_librarian_thumbnails_use_worker_and_hold_shutdown_until_pool_finishes(self):
        from core.thumbnail_loader import load_thumbnail
        module=LibrarianModule();module.on_load({'data_service':self.service,'event_bus':self.bus})
        module.get_view();self.modules.append(module)
        pump(lambda:not module.overview_reader._active and not module.browse_reader._active)
        load,entered,release,threads=self.blocked(load_thumbnail)
        with patch('core.thumbnail_loader.load_thumbnail',side_effect=load),patch('modules.librarian.module.QPixmap',NoFilePixmap):
            module.total_paths=self.paths;module._selection_count=len(self.paths)
            module.refresh_thumbnails_grid();pump(entered.is_set)
            self.assertTrue(all(thread!=threading.get_ident() for thread in threads))
            module.on_unload();self.assertFalse(module.shutdown_ready())
            release.set();pump(module.shutdown_ready)

    def test_same_count_new_paths_replace_preview_and_discard_old_result(self):
        from core.thumbnail_loader import load_thumbnail
        load,entered,release,_=self.blocked(load_thumbnail)
        with patch('core.thumbnail_loader.load_thumbnail',side_effect=load):
            module=self.optimizer([self.paths[0]]);pump(entered.is_set)
            module.queue=[self.paths[1]];module._update_ui();release.set()
            pump(lambda:not module.list_preview.item(0).icon().isNull())
            pixel=module.list_preview.item(0).icon().pixmap(40,40).toImage().pixelColor(10,10)
            self.assertEqual(pixel,QColor('blue'))

    def test_native_notice_from_worker_invalidates_same_stat_cache_on_qt(self):
        module=self.optimizer([self.paths[0]])
        pump(lambda:not module.list_preview.item(0).icon().isNull())
        before=os.stat(self.paths[0]);self.image(self.paths[0],'blue')
        self.assertEqual(os.stat(self.paths[0]).st_size,before.st_size)
        os.utime(self.paths[0],ns=(before.st_atime_ns,before.st_mtime_ns))
        calls=[];update=module._update_ui
        def updated():calls.append(threading.get_ident());return update()
        with patch.object(module,'_update_ui',side_effect=updated):
            thread=threading.Thread(target=lambda:self.bus.publish('library_changed',str(self.root)))
            thread.start();thread.join(2);self.assertFalse(thread.is_alive())
            def blue():
                item=module.list_preview.item(0)
                return item is not None and not item.icon().isNull() and item.icon().pixmap(40,40).toImage().pixelColor(10,10)==QColor('blue')
            pump(blue)
        self.assertEqual(calls,[threading.get_ident()])

    def test_optimizer_analysis_runs_off_gui_and_cannot_apply_after_source_replacement(self):
        module=self.optimizer([self.paths[0]])
        from modules.image_optimizer.module import analyze_image
        load,entered,release,threads=self.blocked(analyze_image)
        module.lbl_suggestion.setText('captured context')
        with patch('modules.image_optimizer.module.analyze_image',side_effect=load):
            module._analyze_first();pump(entered.is_set)
            module.queue=[self.paths[1]];module._update_ui()
            release.set();pump(module._analysis.shutdown_ready);APP.processEvents()
        self.assertTrue(all(thread!=threading.get_ident() for thread in threads))
        self.assertEqual(module.lbl_suggestion.text(),'captured context')

    def test_preview_escape_defers_done_until_actual_decoder_completion(self):
        import core.components.worker_preview as component
        load,entered,release,threads=self.blocked(component._load)
        with patch.object(component,'_load',side_effect=load):
            dialog=WorkerPreviewDialog(self.paths[0],lambda key,default:default)
            self.dialogs.append(dialog);done=[];dialog.finished.connect(done.append)
            dialog.show();pump(entered.is_set);dialog.reject()
            self.assertFalse(dialog.shutdown_ready());self.assertEqual(done,[])
            release.set();pump(lambda:bool(done))
        self.assertTrue(dialog.shutdown_ready());self.assertEqual(len(done),1)
        self.assertTrue(all(thread!=threading.get_ident() for thread in threads))


if __name__=='__main__':unittest.main()
