"""Centro de recuperación: reúne entradas del core y de los módulos sin ejecutar nada.

Offscreen, raíz de proyecto temporal; los módulos son dobles mínimos.
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
from main import MainWindow


class Tool(BaseModule):
    def __init__(self, name, entries=True):
        super().__init__()
        self._name = name
        self.view = None
        self.opened = 0
        self._entries = entries

    def get_view(self):
        if self.view is None:
            self.view = QWidget()
            QVBoxLayout(self.view).addWidget(QLineEdit())
        return self.view

    def recovery_entries(self):
        if self._entries is None:
            raise RuntimeError('broken module')
        if not self._entries:
            return []
        return [(f'{self.name}_journal', f'{self.name} journal', 'Review it.', self.open_review)]

    def open_review(self):
        self.opened += 1


class RecoveryCenter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for replacement in (
                patch.object(ProjectPaths, 'root', return_value=Path(temp.name)),
                patch('main.close_library_services', side_effect=lambda **kw: close_library_services()),
                patch('main.request_close_library_services', side_effect=lambda **kw: request_close_library_services())):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.addCleanup(close_library_services)
        with patch.object(MainWindow, 'load_available_modules'):
            self.main = MainWindow()
        self.a, self.quiet, self.broken = Tool('A'), Tool('Quiet', entries=False), Tool('Broken', entries=None)
        for module in (self.a, self.quiet, self.broken):
            module.on_load(self.main.context)
            self.main.loaded_modules[module.name] = module
        self.main.show()

    def tearDown(self):
        self.main.close()
        # Margen amplio: bajo la suite completa el cierre ordenado tarda más de 3 s.
        for _ in range(500):
            if self.main._shutdown_finished:
                break
            QTest.qWait(20)
        self.assertTrue(self.main._shutdown_finished)
        self.main.deleteLater()
        self.app.processEvents()

    def test_lists_core_and_module_entries_and_skips_broken_module(self):
        keys = [entry.key for entry in self.main.recovery_entries()]
        self.assertEqual(keys, ['producer_journals', 'A_journal'])

    def test_opening_does_not_run_anything_until_an_entry_is_chosen(self):
        self.main.open_recovery_center()
        dialog = self.main._recovery_center
        self.assertTrue(dialog.isVisible())
        self.assertEqual(self.a.opened, 0)
        self.assertNotIn('A', self.main.module_windows)
        self.assertTrue(all(button.toolTip() for button in dialog.buttons.values()))

    def test_module_entry_opens_its_window_then_its_review(self):
        self.main.open_recovery_center()
        self.main._recovery_center.buttons['A_journal'].click()
        self.assertEqual(self.a.opened, 1)
        self.assertTrue(self.main.module_windows['A'].isVisible())

    def test_core_entry_opens_producer_journals(self):
        with patch.object(MainWindow, 'open_producer_recovery') as opened:
            self.main.open_recovery_center()  # la lista se arma con el método ya parcheado
            self.main._recovery_center.buttons['producer_journals'].click()
        opened.assert_called_once()

    def test_reopening_rebuilds_a_single_dialog(self):
        self.main.open_recovery_center()
        first = self.main._recovery_center
        self.main.open_recovery_center()
        self.assertIsNot(first, self.main._recovery_center)
        self.assertFalse(first.isVisible())


if __name__ == '__main__':
    unittest.main()
