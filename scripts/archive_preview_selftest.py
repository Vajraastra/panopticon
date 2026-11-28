"""Primera imagen de ZIP/RAR en memoria (core/archive_preview.py y load_archive_preview).

Los RAR de tests/fixtures/archive_preview/ se crearon con WinRAR 7 (RAR5):
normal (b.png, a10.png, a2.png, readme.txt), sólido sin ordenar (z_first.png,
readme.txt, a.png), cifrado (-p), con cabeceras cifradas (-hp) y sin imágenes.
"""
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from core.archive_preview import Cancelled, inspect_archive, natural_key
from core.file_kinds import file_kind, format_size
from core.image_preview import load_archive_preview

app = QApplication.instance() or QApplication([])
FIXTURES = ROOT / 'tests/fixtures/archive_preview'


def png(color='red', size=(24, 16)):
    image = QImage(*size, QImage.Format_RGB32); image.fill(QColor(color))
    buffer = QBuffer(); buffer.open(QIODevice.WriteOnly); image.save(buffer, 'PNG')
    return bytes(buffer.data())


class ArchivePreview(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def zip(self, name, members, **kwargs):
        path = self.root / name
        with zipfile.ZipFile(path, 'w', **kwargs) as archive:
            for member, data in members.items():
                archive.writestr(member, data)
        return path

    def test_natural_order(self):
        names = ['img10.png', 'img2.png', 'IMG1.png', 'sub/img3.png']
        self.assertEqual(sorted(names, key=natural_key), ['IMG1.png', 'img2.png', 'img10.png', 'sub/img3.png'])

    def test_zip_first_by_natural_name_ignores_non_images_and_dirs(self):
        path = self.zip('p.cbz', {'b.png': png('red'), 'a10.png': png('green'), 'a2.png': png('blue'),
                                  'dir/': b'', 'notes.txt': b'x'})
        info = inspect_archive(path)
        self.assertEqual((info['format'], info['image_count'], info['entry_count']), ('zip', 3, 4))
        self.assertEqual(info['first_name'], 'a2.png'); self.assertIsNone(info['problem'])
        self.assertEqual(info['data'], png('blue'))

    def test_zip_without_images_and_damaged(self):
        self.assertEqual(inspect_archive(self.zip('t.zip', {'a.txt': b'x'}))['problem'], 'no_images')
        broken = self.root / 'broken.zip'; broken.write_bytes(b'PK\x03\x04 not really')
        with self.assertRaises(ValueError):
            inspect_archive(broken)
        raw = self.zip('ok.zip', {'a.png': png() * 50}, compression=zipfile.ZIP_DEFLATED).read_bytes()
        truncated = self.root / 'trunc.zip'; truncated.write_bytes(raw[:len(raw) // 2])
        with self.assertRaises(ValueError):
            inspect_archive(truncated)

    def test_zip_cancel(self):
        path = self.zip('p.zip', {'a.png': png()})
        event = threading.Event(); event.set()
        with self.assertRaises(Cancelled):
            inspect_archive(path, event)
        self.assertIsNone(load_archive_preview(str(path), event))

    def test_rar_normal_solid_and_problems(self):
        normal = inspect_archive(FIXTURES / 'normal.rar')
        self.assertEqual((normal['image_count'], normal['entry_count'], normal['first_name']), (3, 4, 'a2.png'))
        self.assertFalse(normal['solid']); self.assertTrue(QImage.fromData(normal['data']).width() == 24)
        solid = inspect_archive(FIXTURES / 'solid.rar')
        self.assertTrue(solid['solid']); self.assertEqual(solid['first_name'], 'z_first.png')
        self.assertFalse(QImage.fromData(solid['data']).isNull())
        self.assertEqual(inspect_archive(FIXTURES / 'encrypted.rar')['problem'], 'encrypted')
        self.assertEqual(inspect_archive(FIXTURES / 'header-encrypted.rar')['problem'], 'encrypted')
        self.assertEqual(inspect_archive(FIXTURES / 'no-images.rar')['problem'], 'no_images')

    def test_rar_truncated_is_damaged(self):
        raw = (FIXTURES / 'normal.rar').read_bytes()
        path = self.root / 'trunc.rar'; path.write_bytes(raw[:len(raw) - 120])
        with self.assertRaises(ValueError):
            inspect_archive(path)

    def test_load_archive_preview_decodes_and_reports_problems(self):
        value = load_archive_preview(str(FIXTURES / 'normal.rar'), threading.Event())
        self.assertFalse(value['image'].isNull()); self.assertEqual((value['width'], value['height']), (24, 16))
        self.assertEqual(value['bytes'], (FIXTURES / 'normal.rar').stat().st_size)
        self.assertNotIn('data', value['archive'])
        damaged = self.root / 'x.rar'; damaged.write_bytes(b'this is not a rar archive')
        value = load_archive_preview(str(damaged), threading.Event())
        self.assertTrue(value['image'].isNull()); self.assertEqual(value['archive']['problem'], 'archive_damaged')
        self.assertIsNotNone(value['identity'])
        fake = self.zip('fake.zip', {'a.png': b'not a png'})
        value = load_archive_preview(str(fake), threading.Event())
        self.assertTrue(value['image'].isNull()); self.assertEqual(value['archive']['problem'], 'undecodable')

    def test_file_kinds(self):
        self.assertEqual(file_kind('a.JPEG'), ('📷', 'JPG', 'image'))
        self.assertEqual(file_kind('a.gif')[2], 'animated')
        self.assertEqual(file_kind('a.cbr'), ('📚', 'CBR', 'archive'))
        self.assertEqual(file_kind('a.xyz')[1:], ('XYZ', 'other'))
        self.assertEqual(format_size(512), '512 B'); self.assertEqual(format_size(1536), '1.5 KB')
        self.assertEqual(format_size(3 * 1024 ** 3), '3.0 GB')


if __name__ == '__main__':
    unittest.main(verbosity=2)
