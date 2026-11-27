"""Navegador de imágenes compartido (U8): extremos, vuelta, bloqueo y atajos.

Offscreen; no toca la base ni los módulos.
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget
from core.components.image_navigator import ImageNavigator, KEYS_ALT_ARROWS


def tr(_key, default):
    return default


class ImageNavigatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def window(self, **kwargs):
        window = QWidget()
        layout = QVBoxLayout(window)
        nav = ImageNavigator(tr, **kwargs)
        field = QLineEdit('abc')
        layout.addWidget(nav)
        layout.addWidget(field)
        nav.install_shortcuts(window)
        steps = []
        nav.navigate.connect(steps.append)
        window.show()
        QTest.qWaitForWindowExposed(window)
        window.activateWindow()
        self.addCleanup(window.deleteLater)
        return window, nav, field, steps

    def test_edges_counter_and_disabled(self):
        _window, nav, _field, steps = self.window()
        nav.set_position(0, 3)
        self.assertEqual(nav.lbl_position.text(), '1 / 3')
        self.assertFalse(nav.btn_prev.isEnabled())
        self.assertTrue(nav.btn_next.isEnabled())
        nav.btn_next.click()
        nav.set_position(2, 3)
        self.assertFalse(nav.btn_next.isEnabled())
        nav.set_position(1, 3, enabled=False)
        nav.btn_prev.click()
        self.assertEqual(steps, [1])
        nav.set_position(-1, 0)
        self.assertEqual(nav.lbl_position.text(), '0 / 0')

    def test_wrap_keeps_both_buttons(self):
        _window, nav, _field, _steps = self.window(wrap=True)
        nav.set_position(0, 2)
        self.assertTrue(nav.btn_prev.isEnabled() and nav.btn_next.isEnabled())
        nav.set_position(0, 1)
        self.assertFalse(nav.btn_prev.isEnabled() or nav.btn_next.isEnabled())

    def test_arrows_navigate_but_text_field_keeps_them(self):
        window, nav, field, steps = self.window()
        nav.set_position(1, 3)
        window.setFocus()
        QTest.keyClick(window, Qt.Key_Right)
        QTest.keyClick(window, Qt.Key_Left)
        self.assertEqual(steps, [1, -1])
        field.setFocus()
        field.setCursorPosition(1)
        QTest.keyClick(field, Qt.Key_Right)
        self.assertEqual(field.cursorPosition(), 2)
        self.assertEqual(steps, [1, -1])

    def test_alt_keys_and_tips(self):
        window, nav, _field, steps = self.window(keys=KEYS_ALT_ARROWS, show_name=True)
        nav.set_position(0, 2, 'a.png')
        self.assertIn('a.png', nav.lbl_position.text())
        self.assertIn('Alt+→', nav.btn_next.toolTip())
        window.setFocus()
        QTest.keyClick(window, Qt.Key_Right)
        QTest.keyClick(window, Qt.Key_Right, Qt.AltModifier)
        self.assertEqual(steps, [1])


if __name__ == '__main__':
    unittest.main()
