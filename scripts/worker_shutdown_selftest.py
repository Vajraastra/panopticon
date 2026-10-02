"""Private Qt lifetimes, full terminal failures and accepted Native writes."""
from contextlib import ExitStack
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
from PySide6.QtCore import QTimer, QRunnable, QThreadPool
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from core.library_async import LibraryReader
from core.library_repository import LibraryRepository
from core.library_service import get_library_service,close_library_services,request_close_library_services
from core.paths import ProjectPaths
from core.theme_manager import ThemeManager
from modules.format_scanner import module as scanner
from modules.image_optimizer import module as optimizer
from modules.gallery.module import GalleryModule
from core import thumbnail_loader as thumbnails
import main as product

def pump(condition,seconds=3):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        QApplication.processEvents()
        if condition():return True
        time.sleep(.003)
    return False

class Workers(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(ProjectPaths,'root',return_value=self.root))
        self.stack.enter_context(patch.object(product,'close_library_services',side_effect=lambda **kwargs:close_library_services()))
        self.stack.enter_context(patch.object(product,'request_close_library_services',side_effect=lambda **kwargs:request_close_library_services()))
        self.service=get_library_service()
        self.addCleanup(close_library_services)
        self.context=dict(data_service=self.service,theme_manager=ThemeManager(),derived_journal_root=self.root/'journals')
        self.modules=[]
        self.workers=[]
        self.releases=[]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for release in self.releases:release.set()
        for module in self.modules:module.on_unload()
        for worker in self.workers:
            self.assertTrue(worker.wait(3000))
        for module in self.modules:self.assertTrue(pump(module.shutdown_ready))
        QApplication.processEvents()
        for module in self.modules:
            view=getattr(module,'view',None) or getattr(module,'view_widget',None)
            if view is not None:view.close();view.deleteLater()
        QApplication.processEvents()

    def module(self,cls):
        module=cls()
        module.on_load(self.context)
        module.get_view()
        self.modules.append(module)
        return module

    def scanner_start(self,module):
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)):module.start_scan()
        self.workers.append(module.worker)

    def blocked_walk(self):
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        def walk(*args,**kwargs):
            entered.set()
            if not release.wait(3):raise TimeoutError('fixture')
            yield str(self.root),[],['a.png','b.jpg']
        return entered,release,walk

    def test_scanner_cancel_keeps_native_worker_retained_and_qt_responsive(self):
        module=self.module(scanner.FormatScannerModule)
        entered,release,walk=self.blocked_walk()
        with patch.object(scanner.os,'walk',side_effect=walk):
            self.scanner_start(module)
            self.assertTrue(entered.wait(1))
            start=time.monotonic();module.on_unload()
            self.assertLess(time.monotonic()-start,.1)
            self.assertFalse(module.shutdown_ready())
            self.assertIn(module.worker,scanner._LIVE_WORKERS)
            ticks=[];QTimer.singleShot(0,lambda:ticks.append(1))
            self.assertTrue(pump(lambda:bool(ticks)))
            release.set()
            self.assertTrue(pump(module.shutdown_ready))
            self.assertTrue(pump(lambda:module.worker not in scanner._LIVE_WORKERS))
        self.assertEqual(module.worker.outcome,'cancelled')
        self.assertNotIn('STATISTICAL RESULTS',module.txt_log.toPlainText())

    def test_scanner_error_has_one_terminal_and_no_false_complete_report(self):
        module=self.module(scanner.FormatScannerModule)
        worker=scanner.ScannerWorker(str(self.root),'private scan')
        terminals=[];worker.finished_signal.connect(terminals.append)
        with patch.object(scanner.os,'walk',side_effect=PermissionError('<denied>')),patch.object(scanner,'ScannerWorker',return_value=worker):
            self.scanner_start(module)
            self.assertTrue(pump(lambda:not module._scanning))
        self.assertEqual(len(terminals),1)
        self.assertEqual(module.worker.outcome,'error')
        self.assertIn('incomplete',module.txt_log.toPlainText())
        self.assertIn('<denied>',module.txt_log.toPlainText())
        self.assertNotIn('STATISTICAL RESULTS',module.txt_log.toPlainText())
        self.assertTrue(module.btn_select.isEnabled())

    def test_scanner_double_start_and_nested_dialog_are_rejected(self):
        module=self.module(scanner.FormatScannerModule)
        entered,release,walk=self.blocked_walk()
        def dialog(*args):
            module.start_scan()
            return str(self.root)
        with patch.object(scanner.os,'walk',side_effect=walk),patch.object(QFileDialog,'getExistingDirectory',side_effect=dialog) as select:
            module.start_scan();self.workers.append(module.worker)
            self.assertTrue(entered.wait(1))
            module.start_scan()
            self.assertEqual(select.call_count,1)
            release.set()
            self.assertTrue(pump(lambda:not module._scanning))
        self.assertEqual(module.worker.outcome,'complete')
        self.assertIn('Total images analyzed: 2',module.txt_log.toPlainText())

    def test_scanner_closed_inside_dialog_starts_no_worker(self):
        module=self.module(scanner.FormatScannerModule)
        def dialog(*args):module.on_unload();return str(self.root)
        with patch.object(QFileDialog,'getExistingDirectory',side_effect=dialog):module.start_scan()
        self.assertIsNone(module.worker)

    def test_reader_readiness_waits_for_connection_work_without_delivering_late_ui(self):
        entered,release=threading.Event(),threading.Event();self.releases.append(release)
        read=self.service.read
        def delayed(*args,**kwargs):
            entered.set();release.wait(3)
            return read(*args,**kwargs)
        reader=LibraryReader(self.service)
        delivered=[];reader.result.connect(lambda *args:delivered.append(args))
        with patch.object(self.service,'read',side_effect=delayed):
            reader.request('count_files')
            self.assertTrue(entered.wait(1))
            reader.close()
            self.assertFalse(reader.shutdown_ready())
            release.set()
            self.assertTrue(pump(reader.shutdown_ready))
        QApplication.processEvents()
        self.assertFalse(delivered)
        reader.deleteLater()

    def test_gallery_close_waits_for_all_readers_and_rejects_new_inputs(self):
        module=self.module(GalleryModule)
        self.assertTrue(pump(lambda:module.view_widget.grid.reader.shutdown_ready() and module.view_widget.sidebar.reader.shutdown_ready()))
        entered,release=threading.Event(),threading.Event();self.releases.append(release)
        read=self.service.read
        def delayed(*args,**kwargs):entered.set();release.wait(3);return read(*args,**kwargs)
        with patch.object(self.service,'read',side_effect=delayed):
            module.view_widget.grid.reader.request('count_files')
            self.assertTrue(entered.wait(1))
            module.on_unload()
            self.assertFalse(module.shutdown_ready())
            module.load_image_set(['never.png'])
            release.set();self.assertTrue(pump(module.shutdown_ready))
        self.assertTrue(module.view_widget.grid._closed)

    def test_thumbnail_shutdown_clears_only_owned_queue_and_waits_for_running_job(self):
        loader=thumbnails.ThumbnailLoader();loader.pool.setMaxThreadCount(1)
        entered,release,foreign_entered,foreign_release=[threading.Event() for _ in range(4)]
        self.releases.extend([release,foreign_release])
        started=[]
        def delayed(worker):entered.set();started.append(worker.path);release.wait(3)
        class Foreign(QRunnable):
            def run(self):foreign_entered.set();foreign_release.wait(3)
        foreign_pool=QThreadPool.globalInstance();foreign_pool.start(Foreign())
        self.assertTrue(foreign_entered.wait(1))
        try:
            with patch.object(thumbnails.ThumbnailWorker,'run',delayed):
                loader.get_thumbnail_image(str(self.root/'a.png'))
                self.assertTrue(entered.wait(1))
                loader.get_thumbnail_image(str(self.root/'b.png'))
                loader.shutdown()
                self.assertFalse(loader.shutdown_ready())
                self.assertIsNone(loader.get_thumbnail_image(str(self.root/'c.png')))
                release.set();self.assertTrue(pump(loader.shutdown_ready))
            self.assertEqual(len(started),1)
            self.assertFalse(foreign_release.is_set())
            self.assertFalse(loader.pending_paths)
        finally:
            release.set();foreign_release.set()
            self.assertTrue(loader.pool.waitForDone(3000))
            self.assertTrue(foreign_pool.waitForDone(3000))
            loader.deleteLater()

    def test_optimizer_shutdown_waits_for_accepted_derived_commit_and_receipt(self):
        module=self.module(optimizer.ImageOptimizerModule)
        source=self.root/'source.png';output=self.root/'out';output.mkdir()
        with Image.new('RGB',(24,16),'green') as image:image.save(source)
        self.service.submit('register_files_minimal',[dict(path=str(source),filename=source.name,size=source.stat().st_size,created=1)]).result()
        module.load_image_set([str(source)])
        entered,release=threading.Event(),threading.Event();self.releases.append(release)
        register=LibraryRepository.register_derived_copy
        def delayed(repo,*args,**kwargs):entered.set();release.wait(3);return register(repo,*args,**kwargs)
        with patch.object(LibraryRepository,'register_derived_copy',delayed),patch.object(QFileDialog,'getExistingDirectory',return_value=str(output)),patch.object(QMessageBox,'information'):
            module._run_all();self.workers.append(module.worker)
            self.assertTrue(entered.wait(2))
            self.assertTrue(list(output.glob('*.png')))
            module.on_unload()
            self.assertFalse(module.shutdown_ready())
            self.assertEqual(self.service.read('count_files'),1)
            release.set();self.assertTrue(pump(module.shutdown_ready))
        self.assertEqual(self.service.read('count_files'),2)
        self.assertTrue(self.service.read('check_fts')['consistent'])

if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    unittest.main(verbosity=2)
