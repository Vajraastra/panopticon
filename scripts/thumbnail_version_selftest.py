"""Private QImages/Qt pools and Native fixtures; no real image/library changes."""
from pathlib import Path
from contextlib import closing
import hashlib
import os
import sys
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication
from core.library_service import LibraryService, LibraryError
from core.library_repository import LibraryRepository
from modules.gallery.logic.thumbnail_cache import ThumbnailCache
from modules.gallery.logic.thumbnail_decode import load_thumbnail
from modules.gallery.logic.loader import ThumbnailLoader
from modules.gallery.module import GalleryModule
from modules.gallery.ui.components import ClickableThumbnail


def pump(predicate, timeout=5):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(.002)
    raise AssertionError('private Qt fixture did not reach expected state')


def image(color, width=32, height=16):
    value = QImage(width, height, QImage.Format_ARGB32)
    value.fill(QColor(color))
    return value


class Cache(unittest.TestCase):
    def test_count_byte_lru_and_detached_images(self):
        cache = ThumbnailCache(2, 1024)
        red = image('red', 8, 8)
        epoch = cache.epoch()
        cache.put('a', (1,), red, epoch)
        cache.put('b', (2,), red, epoch)
        copy = cache.get('a', (1,), epoch)
        copy.fill(QColor('blue'))
        self.assertEqual(cache.get('a', (1,), epoch).pixelColor(0, 0), QColor('red'))
        cache.put('c', (3,), red, epoch)
        self.assertIsNone(cache.get('b', (2,), epoch))
        self.assertEqual(cache.snapshot()[:2], (2, 512))
        cache.put('oversized', (4,), image('blue', 32, 32), epoch)
        self.assertEqual(cache.snapshot()[:2], (2, 512))
        cache.clear()
        cache.put('old-worker', (1,), red, epoch)
        self.assertEqual(cache.snapshot()[:2], (0, 0))


class PreviewHints(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LibraryService(Path(self.temp.name)/'private.db')
        self.addCleanup(self.service.close)
        self.path = (Path(self.temp.name)/'one.png').as_posix()
        self.service.submit('register_files_minimal', [
            dict(path=self.path, filename='one.png', size=1, created=1)]).result(3)

    def test_curation_and_observations_keep_physical_hint_but_metadata_advances_it(self):
        initial = self.service.preview_revision_snapshot()
        revision = self.service.revision_snapshot()
        self.service.submit('edit_curation', self.path, rating=5, add_tags=['curated']).result(3)
        self.assertNotEqual(self.service.revision_snapshot(), revision)
        self.assertEqual(self.service.preview_revision_snapshot(), initial)
        self.service.submit('register_files_minimal', [
            dict(path=self.path, filename='one.png', size=2, created=1)]).result(3)
        self.assertEqual(self.service.preview_revision_snapshot(), initial)
        self.service.submit('update_file_metadata', self.path, dict(positive='changed')).result(3)
        self.assertNotEqual(self.service.preview_revision_snapshot(), initial)

    def test_failed_publication_metadata_does_not_advance_physical_hint(self):
        initial = self.service.preview_revision_snapshot()
        with patch.object(LibraryRepository, '_fts_sync_file', side_effect=RuntimeError('private-rollback')):
            with self.assertRaises(LibraryError):
                self.service.submit('update_file_metadata', self.path, dict(positive='changed')).result(3)
        self.assertEqual(self.service.preview_revision_snapshot(), initial)

    def test_foreign_commit_invalidates_unknown_physical_state_and_snapshot_is_pure(self):
        initial = self.service.preview_revision_snapshot()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('UPDATE files SET rating=2')
            conn.commit()
        deadline = time.monotonic()+3
        while time.monotonic() < deadline and self.service.preview_revision_snapshot() == initial:
            time.sleep(.002)
        self.assertNotEqual(self.service.preview_revision_snapshot(), initial)
        with patch('sqlite3.connect', side_effect=AssertionError('SQL')), patch(
                'core.library_service.os.stat', side_effect=AssertionError('filesystem')):
            for _ in range(1000):
                self.assertEqual(len(self.service.preview_revision_snapshot()), 3)

    def test_byte_budget_evicts_even_below_count_limit(self):
        cache = ThumbnailCache(100, 256)
        epoch = cache.epoch()
        cache.put('a', (1,), image('red', 8, 8), epoch)
        cache.put('b', (2,), image('blue', 8, 8), epoch)
        self.assertEqual(cache.snapshot()[:2], (1, 256))
        self.assertIsNone(cache.get('a', (1,), epoch))
        self.assertIsNone(cache.get('b', (3,), epoch))
        self.assertEqual(cache.snapshot()[:2], (0, 0))


class Thumbnails(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.loaders = []
        self.releases = []
        self.addCleanup(self.close_loaders)

    def close_loaders(self):
        for release in self.releases:
            release.set()
        for loader in self.loaders:
            loader.shutdown()
        pump(lambda: all(loader.shutdown_ready() for loader in self.loaders))

    def loader(self, **kwargs):
        loader = ThumbnailLoader(**kwargs)
        self.loaders.append(loader)
        return loader

    def source(self, name='private.png', color='red', width=32, height=16):
        path = self.root/name
        self.assertTrue(image(color, width, height).save(str(path)))
        return path.as_posix()

    def request(self, loader, path, size=QSize(130, 130)):
        received = []
        def receive(actual, value):
            if actual == path:
                received.append(QImage(value))
        loader.thumbnail_ready.connect(receive)
        try:
            self.assertIsNone(loader.get_thumbnail_image(path, size))
            pump(lambda: bool(received))
            return received[-1]
        finally:
            loader.thumbnail_ready.disconnect(receive)

    def test_real_image_cache_hit_skips_decoder_but_validates_stat_in_worker(self):
        loader, path = self.loader(), self.source()
        first = self.request(loader, path)
        self.assertEqual(first.size(), QSize(130, 65))
        from modules.gallery.logic import thumbnail_decode
        stat_calls, actual = [], thumbnail_decode._identity
        def identity(source):
            stat_calls.append(threading.get_ident())
            return actual(source)
        with patch.object(thumbnail_decode, '_identity', identity), patch.object(
                thumbnail_decode, 'QImageReader', side_effect=AssertionError('cache hit decoded')):
            second = self.request(loader, path)
        self.assertEqual(second, first)
        self.assertEqual(len(stat_calls), 2)
        self.assertNotIn(threading.get_ident(), stat_calls)

    def test_same_path_new_physical_version_and_requested_size_return_new_image(self):
        loader, path = self.loader(), self.source()
        self.assertEqual(self.request(loader, path).pixelColor(0, 0), QColor('red'))
        before = os.stat(path)
        self.assertTrue(image('blue', 32, 16).save(path))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns+1000000000))
        second = self.request(loader, path, QSize(80, 80))
        self.assertEqual(second.size(), QSize(80, 40))
        self.assertEqual(second.pixelColor(0, 0), QColor('blue'))
        third = self.request(loader, path)
        self.assertEqual(third.size(), QSize(130, 65))
        self.assertEqual(third.pixelColor(0, 0), QColor('blue'))

    def test_replaced_file_identity_invalidates_cache(self):
        loader, path = self.loader(), self.source()
        self.request(loader, path)
        replacement = self.source('replacement.png', 'green')
        before = os.stat(path)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, path)
        self.assertEqual(self.request(loader, path).pixelColor(0, 0), QColor('green'))

    def test_missing_unreadable_and_file_limits_deliver_null_without_mutating_source(self):
        loader, path = self.loader(), self.source()
        before = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        self.assertTrue(self.request(loader, (self.root/'missing.png').as_posix()).isNull())
        bad = self.root/'bad.png'
        bad.write_bytes(b'not an image')
        self.assertTrue(self.request(loader, bad.as_posix()).isNull())
        with patch('modules.gallery.logic.thumbnail_decode.MAX_SOURCE_BYTES', 1):
            self.assertTrue(self.request(loader, path).isNull())
        with patch('modules.gallery.logic.thumbnail_decode.MAX_SOURCE_PIXELS', 1):
            self.assertTrue(self.request(loader, path).isNull())
        self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), before)
        moved = self.root/'moved.png'
        os.rename(path, moved)  # Decoder handle must already be closed on Windows.
        self.assertTrue(moved.exists())

    def test_changed_file_during_decode_or_cache_validation_is_not_delivered(self):
        path = self.source()
        cache = ThumbnailCache(2, 1024*1024)
        cancel = threading.Event()
        identities = [(1, 1, 1, 1), (1, 1, 1, 2)]
        with patch('modules.gallery.logic.thumbnail_decode._identity', side_effect=identities):
            self.assertTrue(load_thumbnail(path, QSize(130, 130), cache, cache.epoch(), cancel).isNull())
        cache.put((path, 130, 130), identities[0], image('red'), cache.epoch())
        with patch('modules.gallery.logic.thumbnail_decode._identity', side_effect=identities):
            self.assertTrue(load_thumbnail(path, QSize(130, 130), cache, cache.epoch(), cancel).isNull())

    def test_pending_queue_is_bounded_and_duplicate_requests_coalesce(self):
        loader = self.loader(max_pending=2)
        loader.pool.setMaxThreadCount(1)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        actual = __import__('modules.gallery.logic.loader', fromlist=['load_thumbnail']).load_thumbnail
        calls = []
        def slow(*args):
            calls.append(args[0]); entered.set()
            if not release.wait(4):
                raise RuntimeError('private-thumbnail-timeout')
            return actual(*args)
        paths = [self.source(f'{index}.png') for index in range(4)]
        with patch('modules.gallery.logic.loader.load_thumbnail', slow):
            loader.get_thumbnail_image(paths[0])
            self.assertTrue(entered.wait(2))
            loader.get_thumbnail_image(paths[1])
            loader.get_thumbnail_image(paths[1])
            loader.get_thumbnail_image(paths[2])
            with self.assertRaisesRegex(ValueError, 'thumbnail_queue_full'):
                loader.get_thumbnail_image(paths[3])
            self.assertEqual((len(loader._active), len(loader._pending)), (1, 2))
            release.set()
            pump(lambda: not loader.pending_paths)
        self.assertEqual(calls, paths[:3])

    def test_navigation_discards_old_queued_work_and_stale_results(self):
        loader = self.loader()
        loader.pool.setMaxThreadCount(1)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        from modules.gallery.logic import loader as module
        actual, calls, received = module.load_thumbnail, [], []
        paths = [self.source(f'{index}.png') for index in range(3)]
        def slow(*args):
            calls.append(args[0])
            if len(calls) == 1:
                entered.set()
                if not release.wait(4):
                    raise RuntimeError('private-thumbnail-timeout')
            return actual(*args)
        loader.thumbnail_ready.connect(lambda path, value: received.append(path))
        with patch.object(module, 'load_thumbnail', slow):
            loader.get_thumbnail_image(paths[0])
            self.assertTrue(entered.wait(2))
            loader.get_thumbnail_image(paths[1])
            loader.invalidate()
            loader.get_thumbnail_image(paths[2])
            release.set()
            pump(lambda: received == [paths[2]])
        self.assertEqual(calls, [paths[0], paths[2]])

    def test_shutdown_does_not_publish_late_result_or_stop_another_loader(self):
        one, two = self.loader(), self.loader()
        path = self.source()
        first = self.request(one, path)
        one.shutdown()
        self.assertIsNone(one.get_thumbnail_image(path))
        second = self.request(two, path)
        self.assertEqual(first, second)
        self.assertFalse(two._closed)

    def test_invalid_input_is_rejected_before_worker_or_filesystem(self):
        loader = self.loader()
        with patch('modules.gallery.logic.loader.ThumbnailWorker', side_effect=AssertionError('worker')):
            for path, size in [('', QSize(130, 130)), ('a\x00b', QSize(130, 130)),
                               ('a', QSize(513, 1)), ('a', QSize(0, 0))]:
                with self.assertRaises(ValueError):
                    loader.get_thumbnail_image(path, size)

    def test_native_invalidation_clears_cache_and_prevents_old_epoch_repopulation(self):
        loader, path = self.loader(), self.source()
        self.request(loader, path)
        epoch = loader.cache.epoch()
        self.assertEqual(loader.cache.snapshot()[0], 1)
        loader.invalidate(clear_cache=True)
        loader.cache.put((path, 130, 130), (1,), image('red'), epoch)
        self.assertEqual(loader.cache.snapshot()[0], 0)
        self.assertFalse(self.request(loader, path).isNull())

    def test_two_gallery_windows_own_independent_loaders_and_close_independently(self):
        service = LibraryService(self.root/'private.db')
        modules = []
        self.addCleanup(service.close)
        def cleanup():
            for module in modules:
                module.on_unload()
            pump(lambda: all(module.shutdown_ready() for module in modules))
        self.addCleanup(cleanup)
        path = self.source()
        service.submit('register_files_minimal', [dict(path=path, filename='private.png', size=1, created=1)]).result(3)
        for _ in range(2):
            module = GalleryModule()
            module.on_load({'data_service': service})
            module.get_view()
            modules.append(module)
            module.load_image_set([path])
        pump(lambda: all(not module.view_widget.grid.reader._active and
                         module.view_widget.grid.loader.cache.snapshot()[0] == 1 for module in modules))
        self.assertIsNot(modules[0].view_widget.grid.loader, modules[1].view_widget.grid.loader)
        modules[0].on_unload()
        pump(modules[0].shutdown_ready)
        remaining = modules[1].view_widget.grid
        self.assertFalse(remaining.loader._closed)
        self.assertFalse(self.request(remaining.loader, path).isNull())
        cache_epoch = remaining.loader.cache.epoch()
        service.submit('edit_curation', path, rating=4).result(3)
        pump(lambda: remaining.grid_layout.count() > 0 and
             remaining.grid_layout.itemAt(0).widget().rating == 4)
        self.assertEqual(remaining.loader.cache.epoch(), cache_epoch)
        service.submit('update_file_metadata', path, dict(positive='changed')).result(3)
        pump(lambda: remaining.loader.cache.epoch() > cache_epoch)
        pump(lambda: remaining.grid_layout.count() > 0)
        thumb = remaining.grid_layout.itemAt(0).widget()
        self.assertIsInstance(thumb, ClickableThumbnail)
        remaining.on_thumbnail_loaded(path, QImage())
        self.assertTrue(thumb.text())
        remaining.shutdown()
        remaining.on_thumbnail_loaded(path, image('blue'))
        self.assertTrue(thumb.text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
