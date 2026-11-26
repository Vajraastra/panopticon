"""Gallery «Enviar resultados» y «Seleccionar página» (portados de Librarian, U1).

SQLite temporal y lectores Qt reales; sin biblioteca real ni archivos de imagen.
El conjunto se envía completo o se rechaza: nunca un prefijo.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox
from core.theme_manager import ThemeManager
from modules.gallery.module import GalleryModule
import curation_snapshot_selftest as helpers
from gallery_data_selftest import pump

TARGETS = [('Pickler', 'Pickler', '🥒'), ('Image Optimizer', 'Optimizer', '🚀')]


def choose(name):
    class Menu(QMenu):
        def exec(self, point):
            return next(a for a in self.actions() if a.data() == name)
    return patch('modules.gallery.module.QMenu', Menu)


class GallerySend(helpers.Fixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.module = GalleryModule()
        self.module.on_load(dict(data_service=self.service, theme_manager=ThemeManager()))
        self.module.get_view()
        self.module.set_send_targets(TARGETS)
        self.sent = []
        self.module.request_open_module.connect(lambda name, paths: self.sent.append((name, paths)))
        self.addCleanup(self.close)

    def close(self):
        self.module.on_unload()
        self.assertTrue(pump(self.module.shutdown_ready))
        self.module.view_widget.close()
        self.module.view_widget.deleteLater()
        QApplication.processEvents()

    def send_all(self, name='Pickler'):
        with choose(name):
            self.module.send_all_results()
        self.assertTrue(pump(lambda: self.module._results_target is None))

    def test_library_folder_results_are_sent_complete(self):
        paths = [f'C:/images/{i:04}.png' for i in range(250)]  # más que una página
        self.register(*paths)
        self.module.state.set_mode(self.module.state.VIEW_IMAGES, folder='C:/images')
        self.send_all('Image Optimizer')
        self.assertEqual(len(self.sent), 1)
        name, sent = self.sent[0]
        self.assertEqual(name, 'Image Optimizer')
        self.assertEqual(sorted(sent), paths)

    def test_more_than_limit_is_refused_without_prefix(self):
        paths = [f'C:/big/{i:05}.png' for i in range(GalleryModule.SEND_ALL_LIMIT + 1)]
        self.register(*paths)
        self.module.state.set_mode(self.module.state.VIEW_IMAGES, folder='C:/big')
        with patch.object(QMessageBox, 'warning') as warning:
            self.send_all()
        self.assertEqual(self.sent, [])
        warning.assert_called_once()
        self.assertTrue(self.module.view_widget.btn_send_all.isEnabled())

    def test_unfiltered_custom_set_is_sent_and_oversized_set_refused(self):
        self.module.load_image_set(['C:/x/1.png', 'C:/x/2.png'])
        self.send_all()
        self.assertEqual(self.sent, [('Pickler', ['C:/x/1.png', 'C:/x/2.png'])])
        self.sent.clear()
        self.module.load_image_set([f'C:/x/{i}.png' for i in range(GalleryModule.SEND_ALL_LIMIT + 1)])
        with patch.object(QMessageBox, 'warning') as warning:
            self.send_all()
        self.assertEqual(self.sent, [])
        warning.assert_called_once()

    def test_cancelled_menu_sends_nothing(self):
        self.module.load_image_set(['C:/x/1.png'])
        class Menu(QMenu):
            def exec(self, point):
                return None
        with patch('modules.gallery.module.QMenu', Menu):
            self.module.send_all_results()
        self.assertEqual(self.sent, [])

    def test_select_page_adds_visible_page_and_keeps_order(self):
        state, view = self.module.state, self.module.view_widget
        state.toggle_picker_mode(True)
        view.grid.current_page_paths = ['C:/p/b.png', 'C:/p/a.png']
        state.toggle_selection('C:/p/z.png')
        view.btn_select_page.click()
        self.assertEqual(list(state.selected_paths), ['C:/p/z.png', 'C:/p/b.png', 'C:/p/a.png'])
        view.btn_select_page.click()  # idempotente
        self.assertEqual(len(state.selected_paths), 3)

    def test_bottom_bar_buttons_follow_mode(self):
        state, view = self.module.state, self.module.view_widget
        view.show()
        state.set_mode(state.VIEW_ALBUMS)
        self.assertFalse(view.btn_send_all.isVisible() or view.btn_select_page.isVisible())
        state.set_mode(state.VIEW_IMAGES, folder='C:/images')
        self.assertTrue(view.btn_send_all.isVisible())
        state.toggle_picker_mode(True)
        self.assertTrue(view.btn_select_page.isVisible())
        self.assertFalse(view.btn_send_all.isVisible())


if __name__ == '__main__':
    unittest.main()
