"""Cropper: SQLite temporal, Qt y publicación segura; sin biblioteca real."""
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image
from PySide6.QtCore import QRectF, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from core.library_repository import LibraryRepository
from core.metadata import MetadataBundle, MetadataExtractor, MetadataStamper
from core.theme_manager import ThemeManager
from modules.cropper import module as product
from modules.cropper.logic import cropper_logic as logic
import curation_snapshot_selftest as helpers


class CropperData(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.source = str(self.root / 'source.png')
        self.output = str(self.root / 'crop.png')
        Image.new('RGB', (100, 80), 'red').save(self.source)
        self.modules, self.workers, self.releases = [], [], []
        opener = patch.object(product.CachePaths, 'open_folder')
        self.open_folder = opener.start()
        self.addCleanup(opener.stop)
        self.addCleanup(self.shutdown)

    def shutdown(self):
        for release in self.releases:
            release.set()
        for module in self.modules:
            module.on_unload()
            if module.worker is not None:
                self.workers.append(module.worker)
        for worker in self.workers:
            worker.stop()
            self.assertTrue(worker.wait(3000))
        QApplication.processEvents()
        for module in self.modules:
            module.view.close()
            module.view.deleteLater()
        QApplication.processEvents()

    def view(self):
        module = product.CropperModule()
        module.on_load(dict(data_service=self.service, theme_manager=ThemeManager()))
        module.get_view()
        module.load_image(self.source)
        self.modules.append(module)
        return module

    def start(self, module, output=None):
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(output or self.output, 'PNG (*.png)')):
            module.save_crop()

    def worker(self, source=None, rect=None):
        worker = product.CropWorker(source or self.source, rect or QRectF(.1, .2, .5, .5),
                                    self.output, self.service)
        self.workers.append(worker)
        return worker

    def run_worker(self, worker):
        results = []
        worker.finished_signal.connect(results.append)
        worker.start()
        self.assertTrue(helpers.pump(lambda: bool(results)))
        self.assertTrue(worker.wait(3000))
        return results[0]

    def block_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        real = LibraryRepository.get_curation_snapshot
        def blocked(repo, paths):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('fixture release')
            return real(repo, paths)
        return entered, release, blocked

    def test_snapshot_two_selects_off_gui_and_connection_closed_before_encode(self):
        self.register(self.source)
        self.service.submit('edit_curation', self.source, add_tags=['alpha'], rating=3).result(3)
        from core import library_service
        real_open, real_read = library_service.open_reader, self.service.read
        statements, connections, threads = [], [], []
        def reader(path):
            conn = real_open(path)
            conn.set_trace_callback(statements.append)
            connections.append(conn)
            return conn
        def read(*args, **kwargs):
            threads.append(threading.get_ident())
            return real_read(*args, **kwargs)
        def crop(source, rect, dest, **kwargs):
            import sqlite3
            self.assertEqual((kwargs['tags'], kwargs['rating']), (['alpha'], 3))
            with self.assertRaises(sqlite3.ProgrammingError):
                connections[0].execute('SELECT 1')
            return dest
        with patch.object(library_service, 'open_reader', reader), patch.object(self.service, 'read', read), \
                patch.object(product, 'crop_image', side_effect=crop):
            result = self.run_worker(self.worker())
        self.assertIsNone(result['error'])
        self.assertEqual(sum(s.lstrip().upper().startswith('SELECT') for s in statements), 2)
        self.assertNotEqual(threads[0], threading.get_ident())

    def test_snapshot_keeps_tags_rating_pair_during_concurrent_commit(self):
        self.register(self.source)
        self.service.submit('edit_curation', self.source, add_tags=['old'], rating=2).result(3)
        from core import library_service
        real_open = library_service.open_reader
        changed = []
        def trace(sql):
            if 'FROM file_tags' in sql and not changed:
                self.service.submit('edit_curation', self.source, add_tags=['new'],
                                    remove_tags=['old'], rating=5).result(3)
                changed.append(True)
        def reader(path):
            conn = real_open(path)
            conn.set_trace_callback(trace)
            return conn
        with patch.object(library_service, 'open_reader', reader), \
                patch.object(product, 'crop_image', return_value=self.output) as crop:
            result = self.run_worker(self.worker())
        self.assertIsNone(result['error'])
        self.assertTrue(changed)
        self.assertEqual((crop.call_args.kwargs['tags'], crop.call_args.kwargs['rating']), (['old'], 2))
        self.assertEqual(self.service.read('get_curation_snapshot', [self.source])[0]['rating'], 5)

    def test_missing_external_no_implicit_registration(self):
        with patch.object(product, 'crop_image', return_value=self.output) as crop:
            self.assertIsNone(self.run_worker(self.worker())['error'])
        self.assertEqual((crop.call_args.kwargs['tags'], crop.call_args.kwargs['rating']), ([], 0))
        self.assertEqual(self.service.read('count_files'), 0)

    def test_capture_source_and_rectangle_before_nested_dialog_and_reentrant_save(self):
        module = self.view()
        original = QRectF(module.cropper_widget.get_crop_rect_normalized())
        def dialog(*args):
            module.save_crop()  # nested entry must not open another dialog
            module.load_image(self.output)
            module.cropper_widget._selection.setWidth(.3)
            return self.output[:-4], 'PNG (*.png)'
        with patch.object(QFileDialog, 'getSaveFileName', side_effect=dialog) as select, \
                patch.object(product, 'crop_image', return_value=self.output) as crop, \
                patch.object(QMessageBox, 'information') as done:
            module.save_crop()
            self.assertTrue(helpers.pump(lambda: not module._saving))
        self.assertEqual(select.call_count, 1)
        self.assertEqual(crop.call_args.args, (self.source, original, self.output))
        self.assertEqual(done.call_count, 1)
        self.open_folder.assert_called_once_with(str(self.root))
        self.assertEqual(module.cropper_widget.image_path, self.output)

    def test_slow_read_keeps_qt_responsive_blocks_double_start_and_keeps_new_image(self):
        module = self.view()
        entered, release, blocked = self.block_snapshot()
        with patch.object(LibraryRepository, 'get_curation_snapshot', blocked), \
                patch.object(product, 'crop_image', return_value=self.output) as crop, \
                patch.object(QMessageBox, 'information'):
            start = time.monotonic()
            self.start(module)
            self.assertLess(time.monotonic() - start, .3)
            self.assertTrue(entered.wait(1))
            ticks = []
            QTimer.singleShot(0, lambda: ticks.append(True))
            self.assertTrue(helpers.pump(lambda: bool(ticks)))
            self.assertFalse(module.btn_save.isEnabled())
            with patch.object(QFileDialog, 'getSaveFileName') as dialog:
                module.save_crop()
            dialog.assert_not_called()
            module.load_image(self.output)
            release.set()
            self.assertTrue(helpers.pump(lambda: not module._saving))
        self.assertEqual(crop.call_args.args[0], self.source)
        self.assertEqual(module.cropper_widget.image_path, self.output)
        self.assertTrue(module.btn_save.isEnabled())

    def test_slow_encode_keeps_qt_responsive(self):
        module = self.view()
        gui_thread = threading.get_ident()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def crop(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.output
        def completed(*args):
            self.assertEqual(threading.get_ident(), gui_thread)
        with patch.object(product, 'crop_image', side_effect=crop), \
                patch.object(QMessageBox, 'information', side_effect=completed):
            self.start(module)
            self.assertTrue(entered.wait(1))
            ticks = []
            QTimer.singleShot(0, lambda: ticks.append(True))
            self.assertTrue(helpers.pump(lambda: bool(ticks)))
            release.set()
            self.assertTrue(helpers.pump(lambda: not module._saving))

    def test_read_error_no_export_no_false_success_then_retry(self):
        module = self.view()
        with patch.object(LibraryRepository, 'get_curation_snapshot', side_effect=RuntimeError('locked')), \
                patch.object(product, 'crop_image') as crop, patch.object(QMessageBox, 'warning') as warning, \
                patch.object(QMessageBox, 'information') as done:
            self.start(module)
            self.assertTrue(helpers.pump(lambda: not module._saving))
        crop.assert_not_called()
        done.assert_not_called()
        self.assertIn('locked', warning.call_args.args[2])
        self.assertTrue(module.btn_save.isEnabled())
        self.assertEqual(module.cropper_widget.image_path, self.source)
        with patch.object(product, 'crop_image', return_value=self.output), patch.object(QMessageBox, 'information') as done:
            self.start(module)
            self.assertTrue(helpers.pump(lambda: not module._saving))
        done.assert_called_once()

    def test_blank_exception_is_still_error(self):
        module = self.view()
        with patch.object(LibraryRepository, 'get_curation_snapshot', side_effect=RuntimeError()), \
                patch.object(QMessageBox, 'warning') as warning, patch.object(QMessageBox, 'information') as done:
            self.start(module)
            self.assertTrue(helpers.pump(lambda: not module._saving))
        warning.assert_called_once()
        done.assert_not_called()

    def test_short_snapshot_no_export(self):
        with patch.object(self.service, 'read', return_value=[]), patch.object(product, 'crop_image') as crop:
            self.assertIsNotNone(self.run_worker(self.worker())['error'])
        crop.assert_not_called()

    def test_unload_during_read_cancels_without_qt_join_or_late_ui(self):
        module = self.view()
        entered, release, blocked = self.block_snapshot()
        with patch.object(LibraryRepository, 'get_curation_snapshot', blocked), \
                patch.object(product, 'crop_image') as crop, patch.object(QMessageBox, 'warning') as warning, \
                patch.object(QMessageBox, 'information') as done:
            self.start(module)
            self.assertTrue(entered.wait(1))
            start = time.monotonic()
            module.on_unload()
            self.assertLess(time.monotonic() - start, .1)
            release.set()
            self.assertTrue(module.worker.wait(3000))
            QApplication.processEvents()
        crop.assert_not_called()
        warning.assert_not_called()
        done.assert_not_called()
        module.load_image_set([self.output])
        self.assertEqual(module.cropper_widget.image_path, self.source)

    def test_cancelled_dialog_and_unload_inside_dialog_start_nothing(self):
        module = self.view()
        with patch.object(QFileDialog, 'getSaveFileName', return_value=('', 'PNG (*.png)')):
            module.save_crop()
        self.assertIsNone(module.worker)
        def dialog(*args):
            module.on_unload()
            return self.output, 'PNG (*.png)'
        with patch.object(QFileDialog, 'getSaveFileName', side_effect=dialog):
            module.save_crop()
        self.assertIsNone(module.worker)

    def test_native_finished_but_terminal_pending_blocks_second_start(self):
        module = self.view()
        with patch.object(product, 'crop_image', return_value=self.output), patch.object(QMessageBox, 'information'):
            self.start(module)
            self.assertTrue(module.worker.wait(3000))
            self.assertTrue(module._saving)
            with patch.object(QFileDialog, 'getSaveFileName') as dialog:
                module.save_crop()
            dialog.assert_not_called()
            self.assertTrue(helpers.pump(lambda: not module._saving))

    def test_actual_png_metadata_dimensions_source_and_db_unchanged(self):
        self.register(self.source)
        MetadataStamper.stamp(self.source, MetadataBundle(tags=['embedded'], positive_prompt='fixture prompt'))
        self.service.submit('edit_curation', self.source, add_tags=['central'], rating=4).result(3)
        before = Path(self.source).read_bytes()
        result = self.run_worker(self.worker())
        self.assertIsNone(result['error'])
        with Image.open(self.output) as image:
            self.assertEqual(image.size, (50, 40))
        bundle = MetadataExtractor.extract(self.output)
        self.assertEqual(set(bundle.tags), {'central', 'embedded'})
        self.assertEqual(bundle.rating, 4)
        self.assertEqual(bundle.positive_prompt, 'fixture prompt')
        self.assertEqual(Path(self.source).read_bytes(), before)
        self.assertEqual(self.service.read('count_files'), 1)
        self.assertFalse(list(self.root.glob('.panopticon-crop-*')))

    def test_same_source_and_case_alias_rejected(self):
        before = Path(self.source).read_bytes()
        for output in (self.source, self.source.upper()):
            with self.assertRaises(logic.SourceDestinationError):
                logic.crop_image(self.source, QRectF(0, 0, .5, .5), output)
        self.assertEqual(Path(self.source).read_bytes(), before)

    def test_hardlink_source_alias_rejected(self):
        os.link(self.source, self.output)
        before = Path(self.source).read_bytes()
        with self.assertRaises(logic.SourceDestinationError):
            logic.crop_image(self.source, QRectF(0, 0, .5, .5), self.output)
        self.assertEqual(Path(self.source).read_bytes(), before)
        self.assertEqual(Path(self.output).read_bytes(), before)

    def test_false_stamp_keeps_existing_destination_and_cleans_temp(self):
        Path(self.output).write_bytes(b'existing destination')
        with patch.object(MetadataStamper, 'stamp', return_value=False):
            with self.assertRaisesRegex(OSError, 'stamping'):
                logic.crop_image(self.source, QRectF(0, 0, .5, .5), self.output, tags=['alpha'])
        self.assertEqual(Path(self.output).read_bytes(), b'existing destination')
        self.assertFalse(list(self.root.glob('.panopticon-crop-*')))

    def test_encode_failure_keeps_destination_and_cleans_temp(self):
        Path(self.output).write_bytes(b'existing destination')
        with patch.object(Image.Image, 'save', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                logic.crop_image(self.source, QRectF(0, 0, .5, .5), self.output)
        self.assertEqual(Path(self.output).read_bytes(), b'existing destination')
        self.assertFalse(list(self.root.glob('.panopticon-crop-*')))

    def test_publish_failure_keeps_destination_and_cleans_temp(self):
        Path(self.output).write_bytes(b'existing destination')
        with patch.object(logic.os, 'replace', side_effect=PermissionError('locked destination')):
            with self.assertRaisesRegex(PermissionError, 'locked destination'):
                logic.crop_image(self.source, QRectF(0, 0, .5, .5), self.output)
        self.assertEqual(Path(self.output).read_bytes(), b'existing destination')
        self.assertFalse(list(self.root.glob('.panopticon-crop-*')))

    def test_cancel_after_encode_discards_temp_and_preserves_destination(self):
        Path(self.output).write_bytes(b'existing destination')
        event = threading.Event()
        real_save = Image.Image.save
        def save(*args, **kwargs):
            real_save(*args, **kwargs)
            event.set()
        with patch.object(Image.Image, 'save', side_effect=save, autospec=True):
            with self.assertRaises(logic.CropCancelled):
                logic.crop_image(self.source, QRectF(0, 0, .5, .5), self.output, cancel_event=event)
        self.assertEqual(Path(self.output).read_bytes(), b'existing destination')
        self.assertFalse(list(self.root.glob('.panopticon-crop-*')))

    def test_source_guard_has_actionable_ui_error_no_success(self):
        module = self.view()
        with patch.object(QMessageBox, 'warning') as warning, patch.object(QMessageBox, 'information') as done:
            self.start(module, self.source)
            self.assertTrue(helpers.pump(lambda: not module._saving))
        self.assertIn('different destination', warning.call_args.args[2])
        done.assert_not_called()
        self.assertTrue(module.btn_save.isEnabled())

    def test_destination_published_only_after_complete_metadata(self):
        Path(self.output).write_bytes(b'existing destination')
        before = Path(self.source).read_bytes()
        real_stamp = MetadataStamper.stamp
        def stamp(path, bundle):
            self.assertEqual(Path(self.output).read_bytes(), b'existing destination')
            self.assertEqual(Path(self.source).read_bytes(), before)
            with Image.open(path) as image:
                self.assertEqual(image.size, (50, 40))
            return real_stamp(path, bundle)
        with patch.object(MetadataStamper, 'stamp', side_effect=stamp):
            logic.crop_image(self.source, QRectF(0, 0, .5, .5), self.output, tags=['alpha'], rating=3)
        self.assertEqual(MetadataExtractor.extract(self.output).tags, ['alpha'])
        self.assertEqual(Path(self.source).read_bytes(), before)

    def test_destination_becomes_source_alias_during_encode_rejected_before_publication(self):
        before = Path(self.source).read_bytes()
        real_save = Image.Image.save
        def save(*args, **kwargs):
            real_save(*args, **kwargs)
            os.link(self.source, self.output)
        with patch.object(Image.Image, 'save', side_effect=save, autospec=True):
            with self.assertRaises(logic.SourceDestinationError):
                logic.crop_image(self.source, QRectF(0, 0, .5, .5), self.output)
        self.assertEqual(Path(self.source).read_bytes(), before)
        self.assertEqual(Path(self.output).read_bytes(), before)
        self.assertFalse(list(self.root.glob('.panopticon-crop-*')))

    def test_actual_stamp_failure_is_visible_and_does_not_open_folder(self):
        module = self.view()
        self.register(self.source)
        self.service.submit('edit_curation', self.source, add_tags=['central'], rating=4).result(3)
        with patch.object(MetadataStamper, 'stamp', return_value=False), \
                patch.object(QMessageBox, 'warning') as warning, patch.object(QMessageBox, 'information') as done:
            self.start(module)
            self.assertTrue(helpers.pump(lambda: not module._saving))
        warning.assert_called_once()
        done.assert_not_called()
        self.open_folder.assert_not_called()
        self.assertFalse(Path(self.output).exists())

    def test_actual_jpeg_metadata_and_source_bytes(self):
        before = Path(self.source).read_bytes()
        output = self.root / 'crop.jpg'
        logic.crop_image(self.source, QRectF(0, 0, .5, .5), output, tags=['central'], rating=3)
        with Image.open(output) as image:
            self.assertEqual(image.size, (50, 40))
            self.assertEqual(image.format, 'JPEG')
        bundle = MetadataExtractor.extract(output)
        self.assertEqual((bundle.tags, bundle.rating), (['central'], 3))
        self.assertEqual(Path(self.source).read_bytes(), before)
        self.assertFalse(list(self.root.glob('.panopticon-crop-*')))


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main()
