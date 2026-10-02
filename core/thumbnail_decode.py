"""Worker-only stat/decode/version verification; never constructs QPixmap."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QImage, QImageReader
from core.image_preview import _identity, MAX_SOURCE_BYTES, MAX_SOURCE_PIXELS


def load_thumbnail(path, size, cache, epoch, cancel):
    if cancel.is_set():
        return QImage()
    identity = _identity(path)
    if identity is None:
        return QImage()
    if identity[2] > MAX_SOURCE_BYTES:
        raise ValueError('thumbnail_file_limit')
    key = path, size.width(), size.height()
    cached = cache.get(key, identity, epoch) if cache is not None else None
    if cached is not None:
        return QImage() if cancel.is_set() or _identity(path) != identity else cached
    reader = QImageReader(path)
    try:
        source = reader.size()
        if source.isValid() and source.width() * source.height() > MAX_SOURCE_PIXELS:
            raise ValueError('thumbnail_pixel_limit')
        if source.isValid() and (source.width() > size.width() or source.height() > size.height()):
            reader.setScaledSize(source.scaled(size, Qt.KeepAspectRatio))
        image = reader.read()
    finally:
        device = reader.device()
        if device is not None:
            device.close()
    if cancel.is_set() or _identity(path) != identity:
        return QImage()
    if image.isNull():
        return image
    image = image.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    if cache is not None:
        cache.put(key, identity, image, epoch)
    return image
