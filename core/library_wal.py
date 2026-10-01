"""Owner-only physical WAL telemetry; no SQLite checkpoint or file reads."""
import os
import stat
import time


class WalSampler:
    def __init__(self, db_path, metrics):
        self._path = db_path + '-wal'
        self._metrics = metrics
        self._next = 0.0

    def poll(self):
        started = time.perf_counter()
        if started < self._next:
            return
        size = None
        try:
            info = os.stat(self._path)
            if not stat.S_ISREG(info.st_mode) or info.st_size < 0:
                raise OSError('invalid_wal_stat')
            status, size = 'present', info.st_size
        except FileNotFoundError:
            status, size = 'absent', 0
        except Exception:
            status = 'error'
        finished = time.perf_counter()
        self._next = finished + 1.0
        self._metrics.wal_sample(status, size, finished, finished - started)

    def stop(self):
        self._metrics.wal_stopped()
