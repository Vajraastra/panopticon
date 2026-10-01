"""Private images, native DB and offscreen Qt; no user collections."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image
from PySide6.QtCore import QSize, QTimer
from PySide6.QtWidgets import QApplication, QWidget
from core.image_preview import ImagePreview, load_preview, MAX_SOURCE_BYTES
from core.metadata.bundle import MetadataBundle
from core.theme_manager import ThemeManager
from modules.gallery.ui.viewer_window import AdvancedViewer
import curation_snapshot_selftest as fixtures


class PreviewFiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_scaled_preview_preserves_source_dimensions_and_bytes(self):
        for ext in ['png', 'jpg', 'webp']:
            with self.subTest(ext=ext):
                path = self.root/('wide.' + ext)
                Image.new('RGB', (3200, 400), 'red').save(path)
                before = hashlib.sha256(path.read_bytes()).digest()
                result = load_preview(str(path), lambda path: MetadataBundle(tags=['tag'], rating=4), threading.Event())
                self.assertEqual((result['width'], result['height']), (3200, 400))
                self.assertEqual((result['image'].width(), result['image'].height()), (2048, 256))
                self.assertEqual((result['tags'], result['rating']), (('tag',), 4))
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)

    def test_cancelled_before_io(self):
        cancel = threading.Event(); cancel.set()
        with patch('core.image_preview.os.stat', side_effect=AssertionError('unexpected IO')):
            self.assertIsNone(load_preview('C:/private.png', lambda path: None, cancel))

    def test_small_source_is_not_upscaled_in_worker(self):
        path = self.root/'small.png'
        Image.new('RGB', (32, 16), 'red').save(path)
        result = load_preview(str(path), lambda path: MetadataBundle(), threading.Event())
        self.assertEqual(result['image'].size(), QSize(32, 16))

    def test_cancel_after_decode_skips_metadata(self):
        path = self.root/'source.png'
        Image.new('RGB', (8, 8)).save(path)
        cancel = threading.Event()
        original = __import__('core.image_preview', fromlist=['QImageReader']).QImageReader
        class Reader:
            def __init__(self, path): self.reader = original(path)
            def size(self): return self.reader.size()
            def setScaledSize(self, value): self.reader.setScaledSize(value)
            def device(self): return self.reader.device()
            def read(self):
                result = self.reader.read(); cancel.set(); return result
        with patch('core.image_preview.QImageReader', Reader):
            self.assertIsNone(load_preview(str(path), lambda path: self.fail('metadata after cancellation'), cancel))

    def test_changed_file_rejects_mixed_preview(self):
        path = self.root/'source.png'
        Image.new('RGB', (8, 8), 'red').save(path)
        def metadata(path):
            Image.new('RGB', (9, 9), 'blue').save(path)
            return MetadataBundle()
        with self.assertRaisesRegex(ValueError, 'preview_source_changed'):
            load_preview(str(path), metadata, threading.Event())

    def test_source_file_limit_before_decoder(self):
        path = self.root/'huge.png'
        with path.open('wb') as target: target.truncate(MAX_SOURCE_BYTES + 1)
        with patch('core.image_preview.QImageReader', side_effect=AssertionError('decode huge file')):
            with self.assertRaisesRegex(ValueError, 'preview_file_limit'):
                load_preview(str(path), lambda path: None, threading.Event())

    def test_source_pixel_limit_before_decode(self):
        with patch('core.image_preview.QImageReader') as reader:
            reader.return_value.size.return_value = QSize(10001, 10001)
            with self.assertRaisesRegex(ValueError, 'preview_pixel_limit'):
                load_preview(str(self.root/'absent.png'), lambda path: None, threading.Event())
            reader.return_value.read.assert_not_called()

    def test_failed_decode_still_supplies_embedded_curation(self):
        result = load_preview(str(self.root/'absent.png'), lambda path: MetadataBundle(tags=['external'], rating=3), threading.Event())
        self.assertTrue(result['image'].isNull())
        self.assertEqual((result['tags'], result['rating']), (('external',), 3))


class PreviewQt(fixtures.Fixture):
    setUpClass = classmethod(fixtures.CurationQt.setUpClass.__func__)
    def setUp(self):
        super().setUp()
        self.views, self.controllers = [], []
        self.releases = []
        self.addCleanup(self.stop)

    def stop(self):
        for event in self.releases: event.set()
        for view in self.views: view.shutdown(); view.close()
        for controller in self.controllers:
            controller.close()
            for thread in controller._threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())
        for view in self.views:
            for reader in (view.curation_reader, view.tags_reader):
                for thread in reader._threads: thread.join(3)
            view.deleteLater()
        self.app.processEvents()

    def image(self, name, color='red'):
        path = str(Path(self.temp.name)/(name + '.png'))
        Image.new('RGB', (32, 16), color).save(path)
        return path

    def viewer(self, paths, parent=None):
        view = AdvancedViewer(paths, parent=parent, service=self.service)
        self.views.append(view); self.controllers.append(view.preview)
        return view

    def ready(self, view):
        self.assertTrue(fixtures.pump(lambda: view._curation_ready))

    def blocker(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []
        def metadata(path):
            calls.append((path, threading.get_ident()))
            if len(calls) == 1:
                entered.set(); release.wait(3)
            return MetadataBundle(tags=[Path(path).stem], rating=3)
        return entered, release, calls, metadata

    def test_decode_stat_and_extraction_are_off_gui(self):
        main = threading.get_ident(); threads = []
        original = load_preview
        def tracked(*args): threads.append(threading.get_ident()); return original(*args)
        constructor = ImagePreview
        with patch('modules.gallery.ui.viewer_window.ImagePreview', lambda metadata, parent: constructor(metadata, parent, loader=tracked)):
            view = self.viewer([self.image('real')]); self.ready(view)
        self.assertTrue(threads)
        self.assertTrue(all(thread != main for thread in threads))
        self.assertIsNotNone(view._preview_image)

    def test_slow_metadata_keeps_qt_responsive(self):
        entered, release, calls, metadata = self.blocker()
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract', metadata):
            started = time.monotonic(); view = self.viewer([self.image('slow')])
            self.assertLess(time.monotonic() - started, .3)
            self.assertTrue(entered.wait(3))
            ticks = []; QTimer.singleShot(0, lambda: ticks.append(True))
            self.assertTrue(fixtures.pump(lambda: ticks))
            self.assertFalse(view._curation_ready)
            self.assertFalse(view.input_tag.isEnabled())
            release.set(); self.ready(view)

    def test_navigation_coalesces_and_only_last_image_and_tags_win(self):
        paths = [self.image('a'), self.image('b', 'green'), self.image('c', 'blue')]
        entered, release, calls, metadata = self.blocker()
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract', metadata):
            view = self.viewer(paths); self.assertTrue(entered.wait(3))
            for _ in range(20): view.next_image()
            self.assertEqual(view.current_idx, 2)
            self.assertIsNone(view._preview_image)
            release.set(); self.ready(view)
        self.assertEqual([path for path, thread in calls], [paths[0], paths[2]])
        self.assertEqual(view._preview_image.pixelColor(0, 0).name(), '#0000ff')
        self.assertEqual((view._file_tags, view._current_rating), (('c',), 3))

    def test_close_rejects_late_preview_without_waiting(self):
        entered, release, calls, metadata = self.blocker()
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract', metadata):
            view = self.viewer([self.image('slow')]); self.assertTrue(entered.wait(3))
            started = time.monotonic(); view.reject()
            self.assertLess(time.monotonic() - started, .1)
            self.assertFalse(view.preview.shutdown_ready())
            release.set(); self.assertTrue(fixtures.pump(view.preview.shutdown_ready))
        self.assertIsNone(view._preview_image)
        self.assertFalse(view._preview_ready)

    def test_module_shutdown_tracks_actual_preview_worker(self):
        from modules.gallery.module import GalleryModule
        parent = QWidget(); self.addCleanup(parent.deleteLater)
        entered, release, calls, metadata = self.blocker()
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract', metadata):
            view = self.viewer([self.image('slow')], parent=parent)
            self.assertTrue(entered.wait(3))
            module = GalleryModule(); module.view_widget = parent
            from types import SimpleNamespace
            parent.grid = SimpleNamespace(loader=SimpleNamespace(shutdown_ready=lambda: True))
            # A worker belongs to the viewer beneath the module's real parent tree.
            view.shutdown()
            self.assertTrue(fixtures.pump(lambda: all(reader.shutdown_ready() for reader in (view.curation_reader, view.tags_reader))))
            self.assertFalse(view.preview.shutdown_ready())
            self.assertFalse(module.shutdown_ready())
            release.set(); self.assertTrue(fixtures.pump(module.shutdown_ready))

    def test_fullscreen_and_resize_reuse_preview_without_file_io(self):
        path = self.image('same'); before = Path(path).read_bytes()
        view = self.viewer([path]); self.ready(view)
        with (patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract', side_effect=AssertionError('repeat metadata')),
              patch('core.image_preview.QImageReader', side_effect=AssertionError('repeat decode'))):
            view.toggle_fullscreen(); self.app.processEvents()
            view.resize(1300, 900); self.app.processEvents()
            self.assertFalse(view.fs_window.lbl_image.pixmap().isNull())
        self.assertEqual(Path(path).read_bytes(), before)

    def test_delayed_layout_rescales_after_preview_is_ready(self):
        view = self.viewer([self.image('layout')]); self.ready(view)
        view.show(); view.resize(1250, 900); self.app.processEvents()
        view.lbl_image.resize(500, 400); self.app.processEvents()
        self.assertFalse(view.lbl_image.pixmap().isNull())
        expected = QSize(32, 16).scaled(view.lbl_image.size(), __import__('PySide6.QtCore',fromlist=['Qt']).Qt.KeepAspectRatio)
        self.assertEqual(view.lbl_image.pixmap().size(), expected)

    def test_preview_failure_clears_old_image_and_embedded_tags(self):
        paths = [self.image('a'), self.image('b')]
        view = self.viewer(paths); self.ready(view)
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract', side_effect=ValueError('private extraction failure')):
            view.next_image(); self.ready(view)
        self.assertIsNone(view._preview_image)
        self.assertEqual((view._file_tags, view._current_rating), ((), 0))
        self.assertTrue(view.lbl_image.pixmap().isNull())

    def test_failed_preview_does_not_replace_authoritative_native_curation(self):
        path = self.image('native'); self.register(path)
        self.service.submit('edit_curation', path, add_tags=['curated'], rating=4).result(3)
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract', side_effect=ValueError('private error')):
            view = self.viewer([path]); self.ready(view)
        self.assertEqual((view._current_rating, view.tags_layout.count()), (4, 1))
        self.assertTrue(view.input_tag.isEnabled())

    def test_independent_viewers_do_not_cancel_each_other(self):
        views = [self.viewer([self.image(name)]) for name in ['one', 'two']]
        for view in views: self.ready(view)
        self.assertIsNot(views[0].preview, views[1].preview)
        views[0].shutdown()
        self.assertFalse(views[1].preview._closed)
        self.assertIsNotNone(views[1]._preview_image)

    def test_invalid_path_rejected_before_start(self):
        controller = ImagePreview(lambda path: self.fail('unexpected metadata'))
        self.controllers.append(controller)
        for path in ['', None, 1, 'x\x00y', 'x'*4097]:
            with self.assertRaises(ValueError): controller.request(path)
        self.assertTrue(controller.shutdown_ready())
        controller.close()
        with self.assertRaises(RuntimeError): controller.request('a.png')

    def test_no_fixed_colors_and_viewer_controls_have_tooltips(self):
        theme = ThemeManager.active()
        view = self.viewer([self.image('theme')]); self.ready(view)
        self.assertEqual(view.styleSheet(), theme.get_stylesheet())
        for control in [view.btn_prev, view.btn_next, view.btn_fullscreen, view.btn_slideshow,
                        view.spin_timer, view.input_tag, view.btn_add_tag, view.lbl_rating_stars]:
            self.assertTrue(control.toolTip())
        source = (ROOT/'modules/gallery/ui/viewer_window.py').read_text(encoding='utf-8')
        import re
        self.assertFalse(re.search(r'#[0-9a-fA-F]{3,8}\b', source))


ROOT = Path(__file__).resolve().parents[1]
if __name__ == '__main__':
    unittest.main()
