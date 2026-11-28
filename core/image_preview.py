"""One active file preview and latest pending request; QPixmap stays on the GUI."""
import os
import threading
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Signal, Qt, QSize, Slot
from PySide6.QtGui import QImage, QImageReader

MAX_SOURCE_PIXELS = 100_000_000
MAX_PREVIEW_EDGE = 2048
MAX_SOURCE_BYTES = 256 * 1024 * 1024


def _identity(path):
    try:
        value = os.stat(path)
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns
    except FileNotFoundError:
        return None


def _read_bounded(reader):
    """Decode with the pixel/edge limits; returns (image, original size)."""
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
    if not image.isNull() and max(image.width(), image.height()) > MAX_PREVIEW_EDGE:
        # Some plugins ignore decoder scaling; limit the delivered image too.
        image = image.scaled(MAX_PREVIEW_EDGE, MAX_PREVIEW_EDGE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return image, size


def load_archive_preview(path, cancel):
    """First image of a ZIP/RAR, decoded in memory (see core/archive_preview.py).

    An archive that cannot be shown (encrypted, split, no images, damaged) still
    returns a result with a null image and ``archive['problem']`` so the caller can
    say why and keep the file reviewable.
    """
    from core.archive_preview import Cancelled, inspect_archive
    if cancel.is_set():
        return None
    before = _identity(path)
    try:
        info = inspect_archive(path, cancel)
    except Cancelled:
        return None
    except ValueError as exc:
        info = dict(format=None, image_count=0, entry_count=0, unpacked_bytes=0, first_name=None,
                    data=None, solid=False, problem=str(exc) if str(exc).startswith('archive_') else 'archive_damaged')
    data = info.pop('data')
    image, size = QImage(), QSize()
    if data is not None:
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.ReadOnly)
        try:
            image, size = _read_bounded(QImageReader(buffer))
        except ValueError as exc:
            info['problem'] = str(exc)
        if image.isNull() and info['problem'] is None:
            info['problem'] = 'undecodable'
    if cancel.is_set():
        return None
    if _identity(path) != before:
        raise ValueError('preview_source_changed')
    return dict(path=path, image=image, width=size.width() if size.isValid() else None,
                height=size.height() if size.isValid() else None, identity=before,
                bytes=None if before is None else before[2], tags=(), rating=None, quality=None,
                archive=info)


def load_preview(path, metadata_loader, cancel):
    if cancel.is_set():
        return None
    before = _identity(path)
    if before is not None and before[2] > MAX_SOURCE_BYTES:
        raise ValueError('preview_file_limit')
    image, size = _read_bounded(QImageReader(path))
    if cancel.is_set():
        return None
    bundle = metadata_loader(path)
    if cancel.is_set():
        return None
    if _identity(path) != before:
        raise ValueError('preview_source_changed')
    return dict(path=path, image=image, width=size.width(), height=size.height(), identity=before,
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
