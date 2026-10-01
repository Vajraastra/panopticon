from PySide6.QtCore import QObject, Signal, QRunnable, QThreadPool, QSize, Qt
from PySide6.QtGui import QImage
import os
import sys
import logging
import threading

log = logging.getLogger(__name__)

class WorkerSignals(QObject):
    """Signals for background results."""
    result = Signal(str, QImage)

class ThumbnailWorker(QRunnable):
    """Worker thread for loading and scaling a single image using QImage (thread-safe)."""
    def __init__(self, path, size, cancel_event=None):
        super().__init__()
        self.path = path
        self.size = size
        self.cancel_event = cancel_event
        self.signals = WorkerSignals()

    def run(self):
        try:
            if self.cancel_event is not None and self.cancel_event.is_set(): return
            # print(f"[Loader] Starting load for: {os.path.basename(self.path)}")
            image = QImage(self.path)
            if self.cancel_event is not None and self.cancel_event.is_set(): return
            if not image.isNull():
                scaled = image.scaled(self.size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.signals.result.emit(self.path, scaled)
            else:
                self.signals.result.emit(self.path, None)
        except Exception as e:
            log.error("Error crítico cargando %s: %s", self.path, e)
            self.signals.result.emit(self.path, None)

class ThumbnailLoader(QObject):
    """
    Singleton-loader managing a thread pool and a dictionary-based memory cache for QImages.
    """
    thumbnail_ready = Signal(str, QImage)

    def __init__(self, max_items=1500):
        super().__init__()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(min(8, max(1, os.cpu_count() or 1)))
        self._closed = False
        self._cancel = threading.Event()
            
        self.cache = {} # path -> QImage
        self.max_items = max_items
        self.pending_paths = set()

    def get_thumbnail_image(self, path, size=QSize(130, 130)):
        if self._closed: return None
        # Normalize to forward slashes to match DB consistency
        norm_path = os.path.normpath(path).replace('\\', '/')
        
        # 1. Check Cache
        if norm_path in self.cache:
            # print(f"[Loader] Cache HIT for {os.path.basename(path)}")
            return self.cache[norm_path]

        # 2. Check if already loading
        if norm_path in self.pending_paths:
            return None

        # 3. Start loading
        self.pending_paths.add(norm_path)
        worker = ThumbnailWorker(norm_path, size, self._cancel)
        # Connect signal to our local handler
        worker.signals.result.connect(self._on_worker_done)
        self.pool.start(worker)
        return None

    def _on_worker_done(self, path, image):
        if self._closed: return
        # This will be executed via QueuedConnection if the worker is in another thread
        # ensuring thread safety when accessing self.cache/pending_paths
        
        if image:
            # Simple LRU-ish cache management (cap by count)
            if len(self.cache) >= self.max_items:
                keys = list(self.cache.keys())
                # Remove first 100 to avoid frequent clearing
                for i in range(min(100, len(keys))):
                    del self.cache[keys[i]]
            
            self.cache[path] = image
        
        if path in self.pending_paths:
            self.pending_paths.remove(path)
            
        # print(f"[Loader] Finished loading: {os.path.basename(path)} (Success: {image is not None})")
        self.thumbnail_ready.emit(path, image)

    def shutdown(self):
        self._closed = True
        self._cancel.set()
        self.pool.clear()  # Only this loader's queued work, never global pools.
        self.pending_paths.clear()
        self.cache.clear()

    def shutdown_ready(self):
        # Zero timeout: poll actual completion, including idle pool threads.
        return self.pool.waitForDone(0)


# Global Instance
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
