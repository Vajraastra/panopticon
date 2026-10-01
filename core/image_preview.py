"""One active file preview and latest pending request; QPixmap stays on the GUI."""
import os
import threading
from PySide6.QtCore import QObject, Signal, Qt, QSize, Slot
from PySide6.QtGui import QImageReader

MAX_SOURCE_PIXELS = 100_000_000
MAX_PREVIEW_EDGE = 2048
MAX_SOURCE_BYTES = 256 * 1024 * 1024


def _identity(path):
    try:
        value = os.stat(path)
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns
    except FileNotFoundError:
        return None


def load_preview(path, metadata_loader, cancel):
    if cancel.is_set():
        return None
    before = _identity(path)
    if before is not None and before[2] > MAX_SOURCE_BYTES:
        raise ValueError('preview_file_limit')
    reader = QImageReader(path)
    try:
        size = reader.size()
        if size.isValid() and size.width() * size.height() > MAX_SOURCE_PIXELS:
            raise ValueError('preview_pixel_limit')
        if size.isValid() and max(size.width(), size.height()) > MAX_PREVIEW_EDGE:
            reader.setScaledSize(size.scaled(QSize(MAX_PREVIEW_EDGE, MAX_PREVIEW_EDGE), Qt.KeepAspectRatio))
        image = reader.read()
    finally:
        device = reader.device()
        if device is not None:
            device.close()
    if cancel.is_set():
        return None
    if not image.isNull() and max(image.width(), image.height()) > MAX_PREVIEW_EDGE:
        # Some plugins ignore decoder scaling; limit the delivered image too.
        image = image.scaled(MAX_PREVIEW_EDGE, MAX_PREVIEW_EDGE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    bundle = metadata_loader(path)
    if cancel.is_set():
        return None
    if _identity(path) != before:
        raise ValueError('preview_source_changed')
    return dict(path=path, image=image, width=size.width(), height=size.height(),
                bytes=None if before is None else before[2], tags=tuple(bundle.tags),
                rating=bundle.rating, quality=bundle.quality_score)


class ImagePreview(QObject):
    result = Signal(int, object, object)
    _delivered = Signal(int, object, object)

    def __init__(self, metadata_loader, parent=None, *, loader=load_preview):
        super().__init__(parent)
        self.metadata_loader, self.loader = metadata_loader, loader
        self._ticket = 0
        self._active = False
        self._pending = None
        self._closed = False
        self._cancel = None
        self._threads = set()
        self._delivered.connect(self._receive, Qt.QueuedConnection)
        self.destroyed.connect(lambda: self.close())

    def request(self, path):
        if self._closed:
            raise RuntimeError('Preview closed')
        if not isinstance(path, str) or not path or len(path) > 4096 or '\x00' in path:
            raise ValueError('invalid_preview_path')
        self._ticket += 1
        request = self._ticket, path
        if self._active:
            self._pending = request
            self._cancel.set()
        else:
            self._start(request)
        return self._ticket

    def _start(self, request):
        ticket, path = request
        self._active = True
        cancel = self._cancel = threading.Event()
        def run():
            value = error = None
            try:
                value = self.loader(path, self.metadata_loader, cancel)
            except Exception as exc:
                # A traceback can retain decoder/file/image locals after delivery.
                error = RuntimeError(str(exc))
            try:
                self._delivered.emit(ticket, value, error)
            except RuntimeError:
                pass
        self._threads = {thread for thread in self._threads if thread.is_alive()}
        thread = threading.Thread(target=run, name='PanopticonPreview', daemon=True)
        self._threads.add(thread)
        try:
            thread.start()
        except Exception as exc:
            self._threads.discard(thread)
            self._delivered.emit(ticket, None, exc)

    @Slot(int, object, object)
    def _receive(self, ticket, value, error):
        self._active = False
        if self._closed:
            return
        if self._pending is not None:
            request, self._pending = self._pending, None
            self._start(request)
        elif ticket == self._ticket:
            self.result.emit(ticket, value, error)

    def close(self):
        self._closed = True
        self._ticket += 1
        self._pending = None
        if self._cancel is not None:
            self._cancel.set()

    def shutdown_ready(self):
        return all(not thread.is_alive() for thread in self._threads)
