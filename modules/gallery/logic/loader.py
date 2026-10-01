"""Per-view thumbnails with bounded admission and worker-validated cache."""
from collections import OrderedDict
import logging
import os
import threading
from PySide6.QtCore import QObject, Signal, Slot, QRunnable, QThreadPool, QSize, Qt
from PySide6.QtGui import QImage
from .thumbnail_cache import ThumbnailCache
from .thumbnail_decode import load_thumbnail

log = logging.getLogger(__name__)


class WorkerSignals(QObject):
    result = Signal(int, str, QImage)


class ThumbnailWorker(QRunnable):
    def __init__(self, path, size, cancel_event=None, *, ticket=0, cache=None, epoch=None):
        super().__init__()
        self.path, self.size = path, QSize(size)
        self.cancel_event = cancel_event or threading.Event()
        self.ticket, self.cache, self.epoch = ticket, cache, epoch
        self.signals = WorkerSignals()

    def run(self):
        image = QImage()
        try:
            image = load_thumbnail(self.path, self.size, self.cache, self.epoch, self.cancel_event)
        except Exception as exc:
            log.warning('Thumbnail failed: %s', str(exc))
        finally:
            # Even cancelled/failed jobs release their bounded admission slot.
            self.signals.result.emit(self.ticket, self.path, image)


class ThumbnailLoader(QObject):
    thumbnail_ready = Signal(str, QImage)

    def __init__(self, max_items=256, *, max_bytes=16*1024*1024, max_pending=256):
        super().__init__()
        if any(type(value) is not int or value < 1 for value in (max_items, max_bytes, max_pending)):
            raise ValueError('invalid_thumbnail_limits')
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(min(4, max(1, os.cpu_count() or 1)))
        self.cache = ThumbnailCache(max_items, max_bytes)
        self.max_items, self.max_pending = max_items, max_pending
        self._pending = OrderedDict()
        self._active = {}
        self._generation = self._ticket = 0
        self._closed = False

    @property
    def pending_paths(self):
        if self._closed:
            return set()
        return {key[0] for key in self._pending} | {
            key[0] for generation, key, _ in self._active.values() if generation == self._generation}

    def get_thumbnail_image(self, path, size=QSize(130, 130)):
        if self._closed:
            return None
        if (not isinstance(path, str) or not path or len(path) > 4096 or '\x00' in path
                or not isinstance(size, QSize) or not size.isValid()
                or not 1 <= size.width() <= 512 or not 1 <= size.height() <= 512):
            raise ValueError('invalid_thumbnail_request')
        normalized = os.path.normpath(path).replace('\\', '/')
        key = normalized, size.width(), size.height()
        if key in self._pending or any(
                generation == self._generation and active_key == key
                for generation, active_key, _ in self._active.values()):
            return None
        if len(self._pending) >= self.max_pending:
            raise ValueError('thumbnail_queue_full')
        self._pending[key] = QSize(size)
        self._schedule()
        # Cache hits also validate the file's stat in the worker.
        return None

    def _schedule(self):
        while not self._closed and self._pending and len(self._active) < self.pool.maxThreadCount():
            key, size = self._pending.popitem(last=False)
            self._ticket += 1
            worker = ThumbnailWorker(key[0], size, ticket=self._ticket,
                                     cache=self.cache, epoch=self.cache.epoch())
            self._active[self._ticket] = self._generation, key, worker
            worker.signals.result.connect(self._on_worker_done, Qt.QueuedConnection)
            self.pool.start(worker)

    @Slot(int, str, QImage)
    def _on_worker_done(self, ticket, path, image):
        record = self._active.pop(ticket, None)
        if self._closed or record is None:
            return
        if record[0] == self._generation:
            self.thumbnail_ready.emit(path, image)
        self._schedule()

    def invalidate(self, *, clear_cache=False):
        self._generation += 1
        self._pending.clear()
        for _, _, worker in self._active.values():
            worker.cancel_event.set()
        if clear_cache:
            self.cache.clear()

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self.invalidate(clear_cache=True)
        self.pool.clear()
        self._active.clear()

    def shutdown_ready(self):
        # Logical result delivery is not native pool/thread completion.
        return self.pool.waitForDone(0)


# Compatibility factory; active Gallery grids create their own loader.
_loader_instance = None


def get_loader():
    global _loader_instance
    if _loader_instance is not None and _loader_instance._closed:
        if not _loader_instance.shutdown_ready():
            raise RuntimeError('Gallery thumbnails are still shutting down')
        _loader_instance = None
    if _loader_instance is None:
        _loader_instance = ThumbnailLoader()
    return _loader_instance
