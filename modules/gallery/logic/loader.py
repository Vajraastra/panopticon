from PySide6.QtCore import QObject, Signal, QRunnable, QThreadPool, QSize, Qt
from PySide6.QtGui import QImage
import os
import sys

class WorkerSignals(QObject):
    """Signals for background results."""
    result = Signal(str, QImage)

class ThumbnailWorker(QRunnable):
    """Worker thread for loading and scaling a single image using QImage (thread-safe)."""
    def __init__(self, path, size):
        super().__init__()
        self.path = path
        self.size = size
        self.signals = WorkerSignals()

    def run(self):
        try:
            # print(f"[Loader] Starting load for: {os.path.basename(self.path)}")
            image = QImage(self.path)
            if not image.isNull():
                scaled = image.scaled(self.size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.signals.result.emit(self.path, scaled)
            else:
                self.signals.result.emit(self.path, None)
        except Exception as e:
            print(f"[Loader ERR] Critical load error for {self.path}: {e}")
            self.signals.result.emit(self.path, None)

class ThumbnailLoader(QObject):
    """
    Singleton-loader managing a thread pool and a dictionary-based memory cache for QImages.
    """
    thumbnail_ready = Signal(str, QImage)

    def __init__(self, max_items=1500):
        super().__init__()
        self.pool = QThreadPool.globalInstance()
        # Ensure enough threads for pre-fetching
        if self.pool.maxThreadCount() < 4:
            self.pool.setMaxThreadCount(4)
            
        self.cache = {} # path -> QImage
        self.max_items = max_items
        self.pending_paths = set()

    def get_thumbnail_image(self, path, size=QSize(130, 130)):
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
        worker = ThumbnailWorker(norm_path, size)
        # Connect signal to our local handler
        worker.signals.result.connect(self._on_worker_done)
        self.pool.start(worker)
        return None

    def _on_worker_done(self, path, image):
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

# Global Instance
_loader_instance = None
def get_loader():
    global _loader_instance
    if _loader_instance is None:
        _loader_instance = ThumbnailLoader()
    return _loader_instance
