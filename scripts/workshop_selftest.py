"""Taller (U5): una tarjeta, una ventana con pestañas y conjunto compartido.

Offscreen, raíz de proyecto temporal; las herramientas son dobles mínimos que
emiten las señales de BaseModule igual que Cropper, Optimizer y Watermarker.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget
from core.base_module import BaseModule
from core.library_service import close_library_services, request_close_library_services
from core.paths import ProjectPaths
from core.workshop_window import WorkshopWindow
from main import MainWindow


class Tool(BaseModule):
    def __init__(self, name, multiple=True):
        super().__init__()
        self._name = name
        self._icon = '•'
        self.accepts_multiple_images = multiple
        self.view = None
        self.received = []
        self.refuse = False

    def get_view(self):
        if self.view is None:
            self.view = QWidget()
            self.edit = QLineEdit()
            QVBoxLayout(self.view).addWidget(self.edit)
        return self.view

    def load_image_set(self, paths):
        if self.refuse:
            return
        self.received.append(list(paths))
        self.image_set_loaded.emit(list(paths))


class Workshop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for replacement in (
                patch.object(ProjectPaths, 'root', return_value=Path(temp.name)),
                patch('main.close_library_services', side_effect=lambda **kwargs: close_library_services()),
                patch('main.request_close_library_services', side_effect=lambda **kwargs: request_close_library_services())):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.addCleanup(close_library_services)
        with patch.object(MainWindow, 'load_available_modules'):
            self.main = MainWindow()
        self.crop = Tool('Smart Cropper')
        self.opt = Tool('Image Optimizer')
        self.wm = Tool('Watermarker', multiple=False)
        self.meta = Tool('Metadata Hub')
        for module in (self.crop, self.opt, self.wm, self.meta):
            module.on_load(self.main.context)
            self.main.add_module_card(module)
            self.main.loaded_modules[module.name] = module
        self.main.setup_integrations()
        self.main.show()

    def tearDown(self):
        self.main.close()
        for _ in range(500):
            if self.main._shutdown_finished:
                break
            QTest.qWait(20)
        self.assertTrue(self.main._shutdown_finished)
        self.main.deleteLater()
        self.app.processEvents()

    def tab(self, module):
        window = self.main._workshop_window
        window.tabs.setCurrentIndex(window.tools.index(module))

    def test_one_card_for_three_tools(self):
        _label, row = self.main._stage_rows['workshop']
        self.assertEqual(row.count(), 2)  # Taller + Metadata Hub
        self.assertEqual(self.main._stage_rows['other'][1].count(), 0)

    def test_send_opens_tab_and_set_follows_tab_switches(self):
        paths = ['C:/x/a.png', 'C:/x/b.png']
        self.main.open_image_set('Image Optimizer', paths)
        window = self.main._workshop_window
        self.assertIsInstance(window, WorkshopWindow)
        self.assertTrue(window.isVisible())
        self.assertIs(window.module_for_tab(window.tabs.currentIndex()), self.opt)
        for module in (self.crop, self.opt, self.wm):
            self.assertIs(self.main.module_windows[module.name], window)
        self.assertNotIn('Metadata Hub', self.main.module_windows)
        self.assertEqual(self.crop.received, [])  # entrega perezosa
        self.tab(self.crop)
        self.assertEqual(self.crop.received, [paths])
        self.crop.current_image_changed.emit('C:/x/b.png')
        self.tab(self.wm)
        self.assertEqual(self.wm.received, [['C:/x/b.png']])  # solo la actual
        self.assertIn('2', window.lbl_shared.text())
        self.tab(self.opt)
        self.tab(self.crop)
        self.assertEqual(len(self.opt.received), 1)  # ya la tenía: no se reenvía
        self.assertEqual(len(self.crop.received), 1)

    def test_single_image_tool_keeps_set_and_new_image_replaces_it(self):
        self.main.open_image_set('Smart Cropper', ['C:/x/a.png', 'C:/x/b.png'])
        window = self.main._workshop_window
        self.tab(self.wm)  # recibe la actual (a.png): no reemplaza el conjunto
        self.wm.load_image_set(['C:/x/a.png'])
        self.assertEqual(window._shared, ['C:/x/a.png', 'C:/x/b.png'])
        self.wm.load_image_set(['C:/y/new.png'])
        self.tab(self.crop)
        self.assertEqual(self.crop.received[-1], ['C:/y/new.png'])

    def test_refused_delivery_is_retried_and_session_survives_close(self):
        self.main.open_image_set('Smart Cropper', ['C:/x/a.png'])
        window = self.main._workshop_window
        self.opt.refuse = True
        self.tab(self.opt)
        self.assertEqual(self.opt.received, [])
        self.opt.refuse = False
        self.tab(self.crop)
        self.tab(self.opt)
        self.assertEqual(self.opt.received, [['C:/x/a.png']])
        self.opt.edit.setText('kept')
        window.close()
        self.assertFalse(window.isVisible())
        self.main.open_workshop()
        self.assertIs(self.main._workshop_window, window)
        self.assertTrue(window.isVisible())
        self.assertEqual(self.opt.edit.text(), 'kept')

    def test_tabs_have_tooltips(self):
        self.main.open_workshop()
        window = self.main._workshop_window
        self.assertEqual(window.tabs.count(), 3)
        for index in range(window.tabs.count()):
            self.assertTrue(window.tabs.tabToolTip(index))
        self.assertTrue(window.lbl_shared.toolTip())


if __name__ == '__main__':
    unittest.main()
