"""Contadores de costo constante; no guarda payloads, rutas, SQL ni historial."""
import threading
import time
from queue import Full


class LibraryMetrics:
    def __init__(self, capacity):
        self._lock = threading.Lock()
        self._values = dict(capacity=capacity, accepting=True, queued=0, active=None,
            high_water=0, accepted=0, committed=0, failed=0, cancelled=0,
            rejected_full=0, rejected_closed=0, rejected_payload=0, reads_active=0, reads_completed=0,
            reads_failed=0, reads_cancelled=0, reads_rejected_closed=0, reads_rejected_payload=0,
            queue_wait_count=0, queue_wait_seconds=0.0, queue_wait_max_seconds=0.0,
            write_count=0, write_seconds=0.0, write_max_seconds=0.0,
            begin_count=0, begin_seconds=0.0, begin_max_seconds=0.0,
            commit_count=0, commit_seconds=0.0, commit_max_seconds=0.0,
            read_count=0, read_seconds=0.0, read_max_seconds=0.0)

    def snapshot(self):
        with self._lock:
            return dict(self._values)

    def enqueue(self, queue, command):
        # El dueño no puede contar un inicio antes de contabilizar su admisión.
        with self._lock:
            try:
                queue.put_nowait(command)
            except Full:
                self._values['rejected_full'] += 1
                raise
            self._values['accepted'] += 1
            self._values['queued'] += 1
            self._values['high_water'] = max(self._values['high_water'], self._values['queued'])

    def reject_full(self):
        with self._lock:
            self._values['rejected_full'] += 1

    def reject_payload(self, *, read=False):
        with self._lock:
            self._values['reads_rejected_payload' if read else 'rejected_payload'] += 1

    def reject_closed(self, *, read=False):
        with self._lock:
            self._values['reads_rejected_closed' if read else 'rejected_closed'] += 1

    def closed(self):
        with self._lock:
            self._values['accepting'] = False

    def _duration(self, phase, seconds):
        self._values[phase + '_count'] += 1
        self._values[phase + '_seconds'] += seconds
        self._values[phase + '_max_seconds'] = max(self._values[phase + '_max_seconds'], seconds)

    def observe(self, phase, seconds):
        with self._lock:
            self._duration(phase, seconds)

    def start_write(self, operation, submitted):
        started = time.perf_counter()
        with self._lock:
            self._values['queued'] -= 1
            self._values['active'] = operation
            self._duration('queue_wait', started - submitted)
        return started

    def finish_write(self, outcome, started):
        with self._lock:
            self._values[outcome] += 1
            self._values['active'] = None
            self._duration('write', time.perf_counter() - started)

    def discard(self, *, cancelled=False):
        with self._lock:
            self._values['queued'] -= 1
            self._values['cancelled' if cancelled else 'failed'] += 1

    def start_read(self):
        with self._lock:
            self._values['reads_active'] += 1
        return time.perf_counter()

    def finish_read(self, outcome, started):
        with self._lock:
            self._values['reads_active'] -= 1
            self._values['reads_' + outcome] += 1
            self._duration('read', time.perf_counter() - started)
