"""Contadores de costo constante; no guarda payloads, rutas, SQL ni historial."""
import threading
import time
from queue import Full


class LibraryMetrics:
    def __init__(self, capacity):
        self._lock = threading.Lock()
        self._queued_since = self._write_since = self._reads_since = None
        self._wal_sampled = None
        self._values = dict(capacity=capacity, accepting=True, queued=0, active=None,
            high_water=0, accepted=0, committed=0, failed=0, cancelled=0,
            rejected_full=0, rejected_closed=0, rejected_payload=0, reads_active=0, reads_completed=0,
            reads_failed=0, reads_cancelled=0, reads_rejected_closed=0, reads_rejected_payload=0, reads_rejected_result=0,
            queue_wait_count=0, queue_wait_seconds=0.0, queue_wait_max_seconds=0.0,
            write_count=0, write_seconds=0.0, write_max_seconds=0.0,
            begin_count=0, begin_seconds=0.0, begin_max_seconds=0.0,
            commit_count=0, commit_seconds=0.0, commit_max_seconds=0.0,
            read_count=0, read_seconds=0.0, read_max_seconds=0.0,
            wal_status='unsampled', wal_bytes=None, wal_observer_active=False,
            wal_samples=0, wal_sample_errors=0,
            wal_stat_seconds=0.0, wal_stat_max_seconds=0.0)

    def snapshot(self):
        with self._lock:
            now = time.perf_counter()
            result = dict(self._values)
            for key, started in (('queue_busy_seconds', self._queued_since),
                                 ('write_active_seconds', self._write_since),
                                 ('reads_busy_seconds', self._reads_since)):
                result[key] = 0.0 if started is None else max(0.0, now - started)
            result['wal_sample_age_seconds'] = (None if self._wal_sampled is None
                                                else max(0.0, now - self._wal_sampled))
            return result

    def wal_sample(self, status, size, sampled, elapsed):
        with self._lock:
            self._wal_sampled = sampled
            self._values.update(wal_status=status, wal_bytes=size, wal_observer_active=True,
                                wal_stat_seconds=max(0.0, elapsed))
            self._values['wal_samples'] += 1
            self._values['wal_sample_errors'] += int(status == 'error')
            self._values['wal_stat_max_seconds'] = max(self._values['wal_stat_max_seconds'], elapsed)

    def wal_stopped(self):
        with self._lock:
            self._values['wal_observer_active'] = False

    def enqueue(self, queue, command):
        # El dueño no puede contar un inicio antes de contabilizar su admisión.
        with self._lock:
            try:
                queue.put_nowait(command)
            except Full:
                self._values['rejected_full'] += 1
                raise
            if self._values['queued'] == 0:
                self._queued_since = time.perf_counter()
            self._values['accepted'] += 1
            self._values['queued'] += 1
            self._values['high_water'] = max(self._values['high_water'], self._values['queued'])

    def reject_full(self):
        with self._lock:
            self._values['rejected_full'] += 1

    def reject_payload(self, *, read=False):
        with self._lock:
            self._values['reads_rejected_payload' if read else 'rejected_payload'] += 1

    def reject_result(self):
        with self._lock:
            self._values['reads_rejected_result'] += 1

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
            if self._values['queued'] == 0:
                self._queued_since = None
            self._values['active'] = operation
            self._write_since = started
            self._duration('queue_wait', started - submitted)
        return started

    def finish_write(self, outcome, started):
        with self._lock:
            self._values[outcome] += 1
            self._values['active'] = None
            self._write_since = None
            self._duration('write', time.perf_counter() - started)

    def discard(self, *, cancelled=False):
        with self._lock:
            self._values['queued'] -= 1
            if self._values['queued'] == 0:
                self._queued_since = None
            self._values['cancelled' if cancelled else 'failed'] += 1

    def start_read(self):
        started = time.perf_counter()
        with self._lock:
            if self._values['reads_active'] == 0:
                self._reads_since = started
            self._values['reads_active'] += 1
        return started

    def finish_read(self, outcome, started):
        with self._lock:
            self._values['reads_active'] -= 1
            if self._values['reads_active'] == 0:
                self._reads_since = None
            self._values['reads_' + outcome] += 1
            self._duration('read', time.perf_counter() - started)
