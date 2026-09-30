"""Dueño especializado de character_profiles.db; no comparte conexiones."""
import atexit
from concurrent.futures import Future
from dataclasses import dataclass
import os
from pathlib import Path
import queue
import sqlite3
import threading
import numpy as np


@dataclass(frozen=True)
class ProfileSnapshot:
    revision: int
    _profiles: tuple

    @property
    def profiles(self):
        # Cabeceras independientes: cambiar shape de un ndarray no altera otro consumidor.
        return tuple((name, np.frombuffer(blob, dtype=np.float32)) for name, blob in self._profiles)


def vector_bytes(embedding):
    vector = np.asarray(embedding)
    if vector.ndim != 1 or not 1 <= vector.size <= 4096 or vector.dtype.kind not in 'fi':
        raise ValueError('invalid_embedding')
    with np.errstate(over='ignore', invalid='ignore'):
        vector = vector.astype(np.float32)
    if not np.isfinite(vector).all() or not np.any(vector):
        raise ValueError('invalid_embedding')
    return vector.tobytes()


def decode_vector(blob):
    if not isinstance(blob, bytes) or not blob or len(blob) % 4 or len(blob) > 16384:
        raise ValueError('invalid_stored_embedding')
    vector = np.frombuffer(blob, dtype=np.float32)
    if not np.isfinite(vector).all() or not np.any(vector):
        raise ValueError('invalid_stored_embedding')
    return vector  # bytes immutable: tampoco puede reactivarse WRITEABLE.


class ProfileService:
    """Cola acotada; SQLite/inicialización/caché sólo en el hilo propietario.

    BEGIN IMMEDIATE protege el promedio también frente a escritores externos.
    data_version invalida caché cuando otra conexión confirma cambios.
    close deja terminar trabajos aceptados; wait es para teardown fuera de Qt.
    """
    def __init__(self, path, capacity=64):
        self.path = str(Path(path).absolute())
        self._queue = queue.Queue(capacity)
        self._lock = threading.Lock()
        self._closed = False
        self._thread = None
        self._cache = None
        self._data_version = None
        self._revision = 0

    def submit(self, operation, *args):
        future = Future()
        try:
            if operation == 'add_reference' and len(args) == 2:
                name, embedding = args
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 256:
                    raise ValueError('invalid_profile_name')
                payload = (name.strip(), vector_bytes(embedding))
            elif operation == 'snapshot' and not args:
                payload = ()
            else:
                raise ValueError('invalid_profile_operation')
            with self._lock:
                if self._closed:
                    raise RuntimeError('profile_service_closed')
                try:
                    self._queue.put_nowait((future, operation, payload))
                except queue.Full:
                    raise RuntimeError('profile_queue_full') from None
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run,
                        name='PanopticonProfiles', daemon=True)
                    try:
                        self._thread.start()
                    except Exception:
                        self._closed = True
                        self._thread = None
                        self._queue.get_nowait()
                        raise
        except Exception as exc:
            future.set_exception(exc)
        return future

    def _connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=1, isolation_level=None)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('''CREATE TABLE IF NOT EXISTS characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL, embedding BLOB NOT NULL,
                samples_count INTEGER DEFAULT 1)''')
            return conn
        except Exception:
            conn.close()
            raise

    def _snapshot(self, conn):
        version = conn.execute('PRAGMA data_version').fetchone()[0]
        if self._cache is not None and version == self._data_version:
            return self._cache
        conn.execute('BEGIN')
        try:
            version = conn.execute('PRAGMA data_version').fetchone()[0]
            rows = conn.execute('SELECT name, embedding, samples_count FROM characters ORDER BY name').fetchall()
            profiles = []
            for name, blob, count in rows:
                if not isinstance(name, str) or not name.strip() or type(count) is not int or count < 1:
                    raise ValueError('invalid_stored_profile')
                decode_vector(blob)
                profiles.append((name, blob))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._revision += 1
        self._cache = ProfileSnapshot(self._revision, tuple(profiles))
        self._data_version = version
        return self._cache

    def _add_reference(self, conn, name, blob):
        embedding = decode_vector(blob)
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT embedding, samples_count FROM characters WHERE name=?', (name,)).fetchone()
            if row:
                old_blob, count = row
                old = decode_vector(old_blob)
                if type(count) is not int or count < 1 or count >= 2**63 - 1:
                    raise ValueError('invalid_sample_count')
                if old.shape != embedding.shape:
                    raise ValueError('embedding_dimension_mismatch')
                # Float64 intermedio evita overflow de float32 * contador.
                average = (old.astype(np.float64) * count + embedding) / (count + 1)
                conn.execute('UPDATE characters SET embedding=?, samples_count=? WHERE name=?',
                    (vector_bytes(average), count + 1, name))
            else:
                conn.execute('INSERT INTO characters(name, embedding) VALUES (?, ?)', (name, blob))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._cache = None  # Nunca publicar aprendizaje especulativo.
        return True

    def _run(self):
        conn = None
        active = None
        try:
            while True:
                try:
                    active, operation, payload = self._queue.get(timeout=.05)
                except queue.Empty:
                    with self._lock:
                        if self._closed:
                            break
                    continue
                if not active.set_running_or_notify_cancel():
                    active = None
                    continue
                try:
                    if conn is None:
                        conn = self._connect()
                    result = self._snapshot(conn) if operation == 'snapshot' else self._add_reference(conn, *payload)
                except Exception as exc:
                    active.set_exception(exc)
                else:
                    active.set_result(result)
                active = None
        except BaseException as exc:
            failure = RuntimeError('profile_owner_failed: ' + (str(exc) or type(exc).__name__))
            failure.__cause__ = exc
            with self._lock:
                self._closed = True
            if active is not None and not active.done():
                active.set_exception(failure)
            while True:
                try:
                    pending, _, _ = self._queue.get_nowait()
                except queue.Empty:
                    break
                if pending.set_running_or_notify_cancel():
                    pending.set_exception(failure)
        finally:
            if conn is not None:
                conn.close()

    def close(self, wait=False):
        with self._lock:
            self._closed = True
        if wait and self._thread is not None:
            if threading.current_thread() is self._thread:
                raise RuntimeError('cannot_join_profile_owner')
            self._thread.join()


_services = {}
_services_lock = threading.Lock()


def get_profile_service(path):
    key = os.path.normcase(os.path.abspath(path))
    with _services_lock:
        if key not in _services:
            _services[key] = ProfileService(path)
        return _services[key]


def close_profile_services():
    with _services_lock:
        services = tuple(_services.values())
        _services.clear()
    for service in services:
        service.close(wait=True)


atexit.register(close_profile_services)
