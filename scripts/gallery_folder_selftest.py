"""Private folder/Qt discovery; no real library, indexing or downloads."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu
from core.folder_images import FolderImages
from core.library_service import LibraryService
from core.theme_manager import ThemeManager
from modules.gallery.module import GalleryModule
from gallery_data_selftest import pump


class FolderDiscovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.folder=self.root/'images';self.folder.mkdir()
        self.path=self.folder/'á.PNG';Image.new('RGB',(40,40),'red').save(self.path)
        self.child=self.folder/'sub';self.child.mkdir()
        self.other=self.child/'β.webp';Image.new('RGB',(40,40),'blue').save(self.other)
        (self.folder/'caption.txt').write_text('ignored')
        self.service=LibraryService(self.root/'native.db');self.addCleanup(self.service.close)
        self.module=None;self.releases=[];self.addCleanup(self.cleanup_module)

    def cleanup_module(self):
        for release in self.releases:release.set()
        if self.module is not None:
            self.module.on_unload()
            self.assertTrue(pump(self.module.shutdown_ready))
            self.module.view_widget.close();self.module.view_widget.deleteLater()
            QApplication.processEvents()

    def view(self):
        self.module=GalleryModule()
        self.module.on_load(dict(data_service=self.service,theme_manager=ThemeManager()))
        self.module.get_view()
        return self.module

    def open(self,folder):
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(folder)):
            self.module.open_folder_dialog()

    def block(self):
        entered,release=threading.Event(),threading.Event();self.releases.append(release)
        actual=FolderImages.read
        def slow(adapter,*args,**kwargs):
            entered.set()
            if not release.wait(3):raise TimeoutError('fixture release')
            return actual(adapter,*args,**kwargs)
        return slow,entered,release

    def test_complete_recursive_unicode_scope_does_not_register_or_edit_files(self):
        before={path:path.read_bytes() for path in (self.path,self.other)}
        files=FolderImages().read('folder_images',str(self.folder))
        self.assertEqual(set(files),{str(path).replace('\\','/') for path in before})
        self.assertEqual(self.service.read('count_files'),0)
        self.assertEqual(before,{path:path.read_bytes() for path in before})

    def test_count_memory_deadline_and_cancellation_fail_without_prefix(self):
        import core.folder_images as product
        with patch.object(product,'MAX_CONTAINER_ITEMS',1):
            with self.assertRaisesRegex(ValueError,'requires_staging'):
                FolderImages().read('folder_images',str(self.folder))
        with patch.object(product,'MAX_BYTES',1):
            with self.assertRaisesRegex(ValueError,'requires_staging'):
                FolderImages().read('folder_images',str(self.folder))
        with patch.object(product,'MAX_SECONDS',-1):
            with self.assertRaisesRegex(ValueError,'budget_exceeded'):
                FolderImages().read('folder_images',str(self.folder))
        event=threading.Event();event.set()
        with self.assertRaisesRegex(ValueError,'cancelled'):
            FolderImages().read('folder_images',str(self.folder),cancel_event=event)

    def test_io_error_and_root_replacement_close_enumeration_and_refuse_prefix(self):
        import core.folder_images as product
        closed=[]
        def fail(*args,**kwargs):
            try:
                yield 'image',str(self.path),self.path.name
                kwargs['onerror'](OSError('private unavailable subtree'))
            finally:closed.append(True)
        with patch.object(product,'entries',side_effect=fail):
            with self.assertRaisesRegex(OSError,'unavailable subtree'):
                FolderImages().read('folder_images',str(self.folder))
        self.assertEqual(closed,[True])
        with patch.object(product,'directory_chain_identity',side_effect=[(1,2),(1,3)]):
            with self.assertRaisesRegex(ValueError,'root_changed'):
                FolderImages().read('folder_images',str(self.folder))

    def test_slow_discovery_off_gui_responsive_and_close_waits_native_completion(self):
        module=self.view();slow,entered,release=self.block()
        threads=[]
        def observed(*args,**kwargs):
            threads.append(threading.get_ident());return slow(*args,**kwargs)
        with patch.object(FolderImages,'read',observed):
            self.open(self.folder);self.assertTrue(entered.wait(1))
            ticks=[];QTimer.singleShot(0,lambda:ticks.append(True))
            self.assertTrue(pump(lambda:bool(ticks)))
            module.on_unload();self.assertFalse(module.shutdown_ready())
            release.set();self.assertTrue(pump(module.shutdown_ready))
        self.assertNotIn(threading.get_ident(),threads)
        self.assertEqual(module.state._custom_paths,[])

    def test_newer_set_or_native_mode_cannot_be_overwritten_by_old_discovery(self):
        module=self.view();slow,entered,release=self.block()
        with patch.object(FolderImages,'read',slow):
            self.open(self.folder);self.assertTrue(entered.wait(1))
            paths=[str(self.other)];module.load_image_set(paths);paths.clear()
            release.set();self.assertTrue(pump(lambda:not module.folder_reader._active))
        self.assertEqual(module.state._custom_paths,[str(self.other)])
        slow,entered,release=self.block()
        with patch.object(FolderImages,'read',slow):
            self.open(self.folder);self.assertTrue(entered.wait(1))
            module.state.set_mode(module.state.VIEW_IMAGES)
            release.set();self.assertTrue(pump(lambda:not module.folder_reader._active))
        self.assertEqual(module.state.mode,module.state.VIEW_IMAGES)

    def test_failure_preserves_view_empty_success_clears_view_and_stale_selection(self):
        module=self.view();module.load_image_set([str(self.path)])
        module.state.toggle_selection(str(self.path))
        self.open(self.root/'missing')
        self.assertTrue(pump(lambda:not module.folder_reader._active))
        self.assertEqual(module.state._custom_paths,[str(self.path)])
        self.assertTrue(module.view_widget.lbl_status.toolTip())
        empty=self.root/'empty';empty.mkdir();self.open(empty)
        self.assertTrue(pump(lambda:not module.folder_reader._active))
        self.assertEqual(module.state._custom_paths,[])
        self.assertFalse(module.state.selected_paths)

    def test_handoff_keeps_selection_order_across_pages_and_reselection(self):
        module=self.view();module.set_send_targets(('optimizer','pickler'))
        # Deliberately differ from set iteration, independent of hash randomization.
        paths=list({str(self.path),str(self.other),str(self.folder/'third.png')})[::-1]
        module.load_image_set(paths);module.state.toggle_picker_mode(True)
        for path in paths:module.state.toggle_selection(path)
        module.state.set_page(1)
        self.assertEqual(list(module.state.selected_paths),paths)
        module.state.toggle_selection(paths[0]);module.state.toggle_selection(paths[0])
        expected=paths[1:]+paths[:1]
        for index,signal in enumerate((module.request_open_optimizer,module.request_open_pickler)):
            received=[];signal.connect(received.append)
            class Menu(QMenu):
                def exec(self,point):return self.actions()[index]
            with patch('modules.gallery.module.QMenu',Menu):
                module.show_send_menu()
            self.assertEqual(received,[expected])
        module.state.toggle_picker_mode(False)
        self.assertFalse(module.state.selected_paths)
        module.state.toggle_selection(paths[0]);module.load_image_set(paths[1:])
        self.assertFalse(module.state.selected_paths)

    def test_latest_folder_request_replaces_pending_older_request(self):
        module=self.view();slow,entered,release=self.block()
        newest=self.root/'newest';newest.mkdir()
        image=newest/'last.jpg';Image.new('RGB',(40,40),'green').save(image)
        with patch.object(FolderImages,'read',slow):
            self.open(self.folder);self.assertTrue(entered.wait(1))
            self.open(self.child);self.open(newest);release.set()
            self.assertTrue(pump(lambda:not module.folder_reader._active))
        self.assertEqual(module.state._custom_paths,[str(image).replace('\\','/')])


if __name__=='__main__':unittest.main()
