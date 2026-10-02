"""Offscreen Qt lifecycle, real Cherry view with local-only runtime work."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget, QLineEdit, QVBoxLayout, QMenu
from PySide6.QtTest import QTest
from core.base_module import BaseModule
from modules.cherry.module import CherryModule
from modules.gallery.module import GalleryModule
from main import MainWindow
from core.paths import ProjectPaths
from core.library_service import close_library_services, request_close_library_services


class Tool(BaseModule):
    def __init__(self, name):
        super().__init__()
        self._name = name
        self.view = None
        self.unloads = 0
        self.received = None

    def get_view(self):
        if self.view is None:
            self.view = QWidget()
            self.edit = QLineEdit()
            QVBoxLayout(self.view).addWidget(self.edit)
        return self.view

    def load_image_set(self, paths):
        self.received = paths

    def on_unload(self):
        self.unloads += 1


class ModuleWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for replacement in (
            patch.object(ProjectPaths,'root',return_value=Path(temp.name)),
            patch('main.close_library_services',side_effect=lambda **kwargs:close_library_services()),
            patch('main.request_close_library_services',side_effect=lambda **kwargs:request_close_library_services())):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.addCleanup(close_library_services)
        with patch.object(MainWindow, 'load_available_modules'):
            self.main = MainWindow()
        self.a, self.b = Tool('A'), Tool('B')
        for module in (self.a, self.b):
            module.on_load(self.main.context)
            self.main.loaded_modules[module.name] = module
            self.main.add_module_card(module)
        self.main.show()

    def tearDown(self):
        self.main.close()
        for _ in range(150):
            if self.main._shutdown_finished: break
            QTest.qWait(20)
        self.assertTrue(self.main._shutdown_finished)
        self.main.deleteLater()
        self.app.processEvents()

    def test_two_windows_keep_dashboard_and_sessions_available(self):
        self.assertIsNone(self.a.view)
        self.main.switch_to_module(self.a)
        self.main.switch_to_module(self.b, ['input.png'])
        wa, wb = self.main.module_windows['A'], self.main.module_windows['B']
        self.assertTrue(self.main.isVisible() and self.main.dashboard_page.isVisible())
        self.assertTrue(wa.isVisible() and wb.isVisible())
        self.assertTrue(wa.isWindow() and wb.isWindow())
        self.assertEqual(wa.windowModality(), Qt.NonModal)
        self.assertEqual(self.b.received, ['input.png'])
        self.a.edit.setText('preserved')
        wa.close()
        self.assertFalse(wa.isVisible())
        self.assertTrue(wb.isVisible())
        self.assertEqual(self.a.unloads, 0)
        self.main.switch_to_module(self.a)
        self.assertIs(self.main.module_windows['A'], wa)
        self.assertEqual(self.a.edit.text(), 'preserved')
        self.main.on_navigate('dashboard')
        self.assertTrue(wa.isVisible() and wb.isVisible())
        self.assertEqual(self.main.root_stack.count(), 2)

    def test_gallery_sends_selection_to_reused_optimizer_and_pickler_sessions(self):
        gallery = GalleryModule()
        optimizer, pickler = Tool('Image Optimizer'), Tool('Pickler')
        for module in (gallery, optimizer, pickler):
            module.on_load(self.main.context)
            self.main.loaded_modules[module.name] = module
        self.main.setup_integrations()
        self.main.switch_to_module(gallery)
        paths = {'C:/private/uno.png', 'C:/private/dos.png'}
        gallery.state.selected_paths = paths.copy()
        for index, target in enumerate((optimizer, pickler)):
            def choose(menu, point, index=index):
                self.assertEqual(len(menu.actions()), 2)
                self.assertTrue(all(action.toolTip() for action in menu.actions()))
                return menu.actions()[index]
            class Menu(QMenu):
                def exec(self, point):
                    return choose(self, point)
            with patch('modules.gallery.module.QMenu', Menu):
                gallery.show_send_menu()
            self.assertEqual(set(target.received), paths)
            window = self.main.module_windows[target.name]
            self.assertTrue(window.isVisible())
            target.edit.setText('session retained')
            window.close()
            gallery.state.selected_paths = {'C:/private/tres.png'}
            with patch('modules.gallery.module.QMenu', Menu):
                gallery.show_send_menu()
            self.assertIs(self.main.module_windows[target.name], window)
            self.assertEqual(target.received, ['C:/private/tres.png'])
            self.assertEqual(target.edit.text(), 'session retained')
            gallery.state.selected_paths = paths.copy()

    def test_gallery_unavailable_target_and_late_navigation_do_not_open_windows(self):
        gallery = GalleryModule()
        gallery.on_load(self.main.context)
        self.main.loaded_modules[gallery.name] = gallery
        self.main.setup_integrations()
        self.main.switch_to_module(gallery)
        gallery.state.selected_paths = {'C:/private/uno.png'}
        delivered = []
        gallery.request_open_optimizer.connect(delivered.append)
        def choose(menu, point):
            self.assertFalse(any(action.isEnabled() for action in menu.actions()))
            return menu.actions()[0]
        class Menu(QMenu):
            def exec(self, point):
                return choose(self, point)
        with patch('modules.gallery.module.QMenu', Menu):
            gallery.show_send_menu()
        self.assertEqual(delivered, [])
        self.main._shutting_down = True
        self.main.switch_to_module(self.a, ['late.png'])
        self.assertNotIn('A', self.main.module_windows)
        self.assertIsNone(self.a.received)
        # Restore the harness to initiate normal shutdown and unload all modules.
        self.main._shutting_down = False

    def test_real_cherry_runtime_keeps_working_with_another_window(self):
        cherry = CherryModule()
        cherry.on_load(self.main.context)
        self.main.loaded_modules[cherry.name] = cherry
        # Do not read the user's index or contact any service.
        with patch('modules.cherry.ui.view.CherryView.refresh'):
            self.main.switch_to_module(cherry)
        self.assertTrue(cherry.runtime.start())
        async def local_job():
            await asyncio.sleep(0.15)
            return 'completed'
        future = cherry.runtime.submit(local_job())
        self.main.switch_to_module(self.a)
        cw = self.main.module_windows[cherry.name]
        self.assertTrue(cw.isVisible() and self.main.isVisible())
        cw.close()
        QTest.qWait(220)
        self.assertEqual(future.result(timeout=2), 'completed')
        self.main.switch_to_module(cherry)
        self.assertIs(self.main.module_windows[cherry.name], cw)
        self.assertTrue(cw.isVisible())
        self.main.close()
        for _ in range(150):
            if self.main._shutdown_finished: break
            QTest.qWait(20)
        self.assertTrue(self.main._shutdown_finished)
        self.assertFalse(cw.isVisible())
        self.assertFalse(cherry.runtime.is_running)
        self.assertEqual(self.a.unloads, 1)


if __name__ == '__main__':
    unittest.main()
