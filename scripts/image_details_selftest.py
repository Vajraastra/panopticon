"""Panel de detalles común (U9): formato de tamaño, estados y filas propias.

Offscreen; no lee archivos (el panel solo muestra lo que el módulo le pasa).
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication, QLabel
from core.components.image_details import ImageDetails, format_bytes


def tr(_key, default):
    return default


class ImageDetailsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_format_bytes(self):
        self.assertEqual(format_bytes(None), '—')
        self.assertEqual(format_bytes(512), '0.5 KB')
        self.assertEqual(format_bytes(1024 * 1024), '1.00 MB')
        self.assertEqual(format_bytes(1023 * 1024), '1023.0 KB')
        self.assertEqual(format_bytes(3 * 1024 ** 3), '3.00 GB')

    def test_lifecycle(self):
        panel = ImageDetails(tr)
        self.addCleanup(panel.deleteLater)
        self.assertEqual(panel.lbl_name.text(), 'No image')
        path = os.path.join('C:' + os.sep, 'fotos', 'a.png')
        panel.show_path(path)
        self.assertEqual(panel.lbl_name.text(), 'a.png')
        self.assertEqual(panel.lbl_folder.text(), os.path.dirname(path))
        self.assertEqual(panel.lbl_resolution.text(), '…')
        panel.set_dimensions(640, 480)
        panel.set_bytes(2048)
        self.assertEqual(panel.lbl_resolution.text(), '640 × 480 px')
        self.assertEqual(panel.lbl_size.text(), '2.0 KB')
        panel.set_dimensions(0, 0)
        self.assertEqual(panel.lbl_resolution.text(), '—')
        panel.set_unavailable()
        self.assertEqual(panel.lbl_size.text(), '—')
        panel.clear()
        self.assertEqual(panel.lbl_folder.text(), '—')

    def test_extra_rows_and_tooltips(self):
        panel = ImageDetails(tr)
        self.addCleanup(panel.deleteLater)
        custom = QLabel('★★★')
        self.assertIs(panel.add_row('Rating', widget=custom), custom)
        quality = panel.add_row('Quality', 'Stored score.')
        self.assertEqual(quality.toolTip(), 'Stored score.')
        for label in (panel.lbl_name, panel.lbl_folder, panel.lbl_resolution, panel.lbl_size):
            self.assertTrue(label.toolTip())


if __name__ == '__main__':
    unittest.main()
