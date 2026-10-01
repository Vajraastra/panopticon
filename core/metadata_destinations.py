"""Guarda RO de destinos por lote, invalidada por commits incluso externos.

No índice/UDF persistente, DDL, reserva de rutas ni atomicidad disco/SQLite.
El dueño crea/cierra el lector en el worker; no retiene transacciones entre usos.
"""
import os
import sqlite3
import sys
import threading
import time

from core.generation_history import path_keys, path_status

MAX_ROWS = 500_000
MAX_BYTES = 64 * 1024 * 1024  # UTF-8 de claves; además overhead del set/Python.
MAX_KEY_MEMORY = 64 * 1024 * 1024  # Tamaños Python; sin contar cursor/transitorios.
BUDGET_SECONDS = 10


class DestinationPaths:
    def __init__(self, conn, db_path, *, normalize, check_open, metrics, error, cancel_event):
        self._conn = conn
        self._path = db_path
        self._identity = self._file_identity()
        self._owner = threading.get_ident()
        self._normalize = normalize
        self._check_open = check_open
        self._metrics = metrics
        self._error = error
        self._cancel_event = cancel_event
        self._version = None
        self._keys = None
        self._closed = False

    def _file_identity(self):
        stat = os.stat(self._path)
        return stat.st_dev, stat.st_ino

    def close(self):
        self._closed = True
        self._keys = None

    def read(self, operation, paths, *, cancel_event=None):
        if operation != 'get_metadata_path_status':
            raise ValueError('invalid_destination_operation')
        if self._closed or threading.get_ident() != self._owner:
            raise self._error(operation, 'destination_guard_closed_or_wrong_thread')
        self._check_open()
        started = self._metrics.start_read()
        outcome = 'failed'
        deadline = time.monotonic() + BUDGET_SECONDS
        def check():
            if ((self._cancel_event is not None and self._cancel_event.is_set()) or
                    (cancel_event is not None and cancel_event.is_set())):
                raise self._error(operation, 'cancelled')
            if time.monotonic() >= deadline:
                raise self._error(operation, 'destination_read_budget_exceeded')
        def interrupt():
            try:
                check()
                return 0
            except self._error:
                return 1
        try:
            check()
            keys = path_keys(paths, normalize=self._normalize)
            if self._file_identity() != self._identity:
                raise self._error(operation, 'destination_database_replaced')
            self._conn.set_progress_handler(interrupt, 1000)
            version = self._conn.execute('PRAGMA data_version').fetchone()[0]
            if version != self._version:
                self._keys = None
                self._version = None
                self._refresh(version, check)
            if self._keys is None:
                # Límite excedido: jamás usar un conjunto incompleto como aprobación.
                self._conn.execute('BEGIN')
                try:
                    result = path_status(self._conn, paths, normalize=self._normalize)
                finally:
                    self._conn.rollback()
            else:
                result = dict(occupied=any(key in self._keys for key in keys))
            check()
            if self._file_identity() != self._identity:
                raise self._error(operation, 'destination_database_replaced')
            outcome = 'completed'
            return result
        except Exception as exc:
            if isinstance(exc, sqlite3.Error):
                try:
                    check()  # Distinguir interrupción/cancelación de fallo SQL.
                except self._error as interrupted:
                    exc = interrupted
                else:
                    exc = self._error(operation, str(exc))
            if isinstance(exc, self._error) and exc.reason == 'cancelled':
                outcome = 'cancelled'
            raise exc
        finally:
            self._conn.set_progress_handler(None, 0)
            self._metrics.finish_read(outcome, started)

    def _refresh(self, version, check):
        # PRAGMA data_version sólo se compara en ESTA conexión persistente.
        # Commit durante el recorrido invalida también el snapshot recién leído.
        for _ in range(3):
            keys, rows, size, key_memory = set(), 0, 0, 0
            self._conn.execute('BEGIN')
            try:
                cursor = self._conn.execute('SELECT substr(path,1,4097) FROM files')
                try:
                    for (path,) in cursor:
                        check()
                        rows += 1
                        if not isinstance(path, str) or not path or len(path) > 4096 or '\x00' in path:
                            raise ValueError('invalid_stored_metadata_path')
                        key = path.casefold()  # Igual que la guarda UDF histórica.
                        size += len(key.encode('utf-8'))
                        if rows > MAX_ROWS or size > MAX_BYTES:
                            keys = None
                            break
                        if key not in keys:
                            key_memory += sys.getsizeof(key)
                            keys.add(key)
                        if key_memory + sys.getsizeof(keys) > MAX_KEY_MEMORY:
                            keys = None
                            break
                finally:
                    cursor.close()
            finally:
                self._conn.rollback()
            current = self._conn.execute('PRAGMA data_version').fetchone()[0]
            check()
            if version == current:
                self._keys, self._version = keys, current
                return
            version = current
        raise self._error('get_metadata_path_status', 'destination_revision_unstable')
