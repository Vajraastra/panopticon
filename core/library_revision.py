"""Ephemeral invalidation token, owned by the native writer; no schema/history."""
import logging
import threading
import uuid

log = logging.getLogger(__name__)


class RevisionClock:
    def __init__(self):
        self._lock = threading.Lock()
        self._epoch = uuid.uuid4().hex
        self._sequence = 0
        self._version = None
        self._healthy = True

    def snapshot(self):
        with self._lock:
            return self._epoch, self._sequence, self._healthy

    def committed(self):
        with self._lock:
            self._sequence += 1

    def stopped(self):
        with self._lock:
            if self._healthy:
                self._healthy = False
                self._sequence += 1

    def poll(self, conn):
        # data_version comparisons are meaningful only on this persistent connection.
        try:
            version = conn.execute('PRAGMA data_version').fetchone()[0]
        except Exception:
            with self._lock:
                report = self._healthy
                if report:
                    self._healthy = False
                    self._sequence += 1
            if report:
                log.warning('Native revision monitoring unavailable', exc_info=True)
            return
        with self._lock:
            changed = not self._healthy or (self._version is not None and version != self._version)
            self._version, self._healthy = version, True
            if changed:
                self._sequence += 1
