"""Thread-safe LRU of QImage value copies, bounded by count and decoded bytes."""
from collections import OrderedDict
import threading
from PySide6.QtGui import QImage


class ThumbnailCache:
    def __init__(self, max_items, max_bytes):
        self.max_items, self.max_bytes = max_items, max_bytes
        self._lock = threading.Lock()
        self._values = OrderedDict()
        self._bytes = 0
        self._epoch = 0

    def epoch(self):
        with self._lock:
            return self._epoch

    def get(self, key, identity, epoch):
        with self._lock:
            record = self._values.get(key)
            if epoch != self._epoch or record is None:
                return None
            if record[0] != identity:
                self._bytes -= record[2]
                del self._values[key]
                return None
            self._values.move_to_end(key)
            return QImage(record[1])

    def put(self, key, identity, image, epoch):
        size = image.sizeInBytes()
        with self._lock:
            if epoch != self._epoch or image.isNull() or size > self.max_bytes:
                return
            previous = self._values.pop(key, None)
            if previous is not None:
                self._bytes -= previous[2]
            while self._values and (len(self._values) >= self.max_items or self._bytes + size > self.max_bytes):
                _, record = self._values.popitem(last=False)
                self._bytes -= record[2]
            self._values[key] = identity, QImage(image), size
            self._bytes += size

    def clear(self):
        with self._lock:
            self._epoch += 1
            self._values.clear()
            self._bytes = 0

    def snapshot(self):
        with self._lock:
            return len(self._values), self._bytes, self._epoch
