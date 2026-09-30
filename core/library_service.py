"""Un escritor por DB y lecturas breves en el hilo que las solicita.

submit() entrega un Future sin esperar a SQLite. La fachada antigua conserva
llamadas síncronas hasta migrar las vistas a entrega asíncrona por Qt.
"""
import atexit
import copy
import logging
import os
import sqlite3
import threading
import uuid
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path
from queue import Queue, Full

from core.library_connections import open_reader
from core.library_repository import LibraryRepository
from core.paths import ProjectPaths

log = logging.getLogger(__name__)
READS = frozenset({
    'get_known_files_in_folder', 'get_files_recursive', 'get_watched_folders',
    'get_folder_count', 'get_folder_preview', 'search_by_terms', 'get_file_id',
    'get_tags_for_file', 'search_files_paginated', 'get_folders_paginated',
    'count_files_missing_hash', 'get_files_missing_hash', 'get_all_tags',
    'get_file_rating', 'count_files', 'get_curation_snapshot',
    'get_library_overview', 'get_library_selection', 'check_fts',
})
WRITES = frozenset({
    'register_files_minimal', 'update_file_metadata', 'remove_files',
    'add_watched_folder', 'remove_watched_folder', 'add_tag_to_file',
    'remove_tag_from_file', 'set_hash_original_bulk', 'update_file_rating',
    'rebuild_fts', 'repair_fts', 'vacuum_database', 'edit_curation',
})


class LibraryError(RuntimeError):
    def __init__(self, operation, reason):
        self.operation = operation
        self.reason = reason
        super().__init__(f'{operation}: {reason}')


def _migrate(conn, db_path):
    # Marca propia: no reutilizar user_version de una instalación previa.
    marker = conn.execute("SELECT 1 FROM sqlite_master WHERE name='panopticon_schema'").fetchone()
    ready = marker and conn.execute(
        "SELECT value FROM panopticon_schema WHERE name='integrity_v1'").fetchone()
    if ready:
        return
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='files'").fetchone():
        # Backup SQLite consistente, incluyendo WAL. Nunca copiar sólo el .db.
        backup_path = Path(str(db_path) + f'.pre-integrity-{uuid.uuid4().hex}.bak')
        temporary = Path(str(backup_path) + '.tmp')
        try:
            with closing(sqlite3.connect(temporary)) as backup:
                conn.backup(backup)
            os.replace(temporary, backup_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        log.info('Respaldo previo a migración: %s', backup_path)
    repo = LibraryRepository(conn)
    conn.execute('BEGIN IMMEDIATE')
    try:
        repo.create_schema()
        repo.run_transformations()
        # Preservar vínculos previos inválidos en una tabla propia recuperable.
        conn.execute('CREATE TABLE IF NOT EXISTS orphaned_file_tags ('
                     'file_id INTEGER, tag_id INTEGER, source TEXT, '
                     'saved_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        orphan = ('file_id IS NULL OR tag_id IS NULL OR '
                  'NOT EXISTS(SELECT 1 FROM files WHERE id=file_tags.file_id) OR '
                  'NOT EXISTS(SELECT 1 FROM tags WHERE id=file_tags.tag_id)')
        conn.execute('INSERT INTO orphaned_file_tags(file_id,tag_id,source) '
                     f'SELECT file_id,tag_id,source FROM file_tags WHERE {orphan}')
        conn.execute(f'DELETE FROM file_tags WHERE {orphan}')
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise sqlite3.IntegrityError('foreign_key_check failed')
        conn.execute('CREATE TABLE IF NOT EXISTS panopticon_schema '
                     '(name TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute("INSERT INTO panopticon_schema VALUES('integrity_v1','1')")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


class LibraryService:
    def __init__(self, db_path, capacity=64):
        self.db_path = str(Path(db_path).resolve())
        self._queue = Queue(maxsize=capacity)
        self._admission = threading.Lock()
        self._closed = False
        self._ready = Future()
        self._thread = threading.Thread(target=self._run, name='PanopticonDB', daemon=True)
        self._thread.start()
        try:
            self._ready.result()
        except BaseException:
            self._thread.join()
            raise

    def _run(self):
        conn = None
        command = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
            conn.execute('PRAGMA foreign_keys=ON')
            if conn.execute('PRAGMA foreign_keys').fetchone() != (1,):
                raise sqlite3.DatabaseError('foreign_keys unavailable')
            _migrate(conn, self.db_path)
            if conn.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
                raise sqlite3.DatabaseError('WAL unavailable')
            conn.execute('PRAGMA synchronous=FULL')
            repo = LibraryRepository(conn)
            self._ready.set_result(True)
            while True:
                command = self._queue.get()
                try:
                    if command is None:
                        break
                    future, operation, args, kwargs = command
                    if not future.set_running_or_notify_cancel():
                        continue
                    try:
                        if operation != 'vacuum_database':
                            conn.execute('BEGIN IMMEDIATE')
                        value = getattr(repo, operation)(*args, **kwargs)
                        if value is not True:
                            raise LibraryError(operation, 'operation_failed')
                        conn.commit()
                        future.set_result(value)
                    except BaseException as exc:
                        conn.rollback()
                        future.set_exception(LibraryError(operation, str(exc)))
                finally:
                    self._queue.task_done()
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(LibraryError('startup', str(exc)))
            else:
                log.exception('El escritor central se detuvo')
                if command is not None and not command[0].done():
                    command[0].set_exception(LibraryError(command[1], 'writer_stopped'))
        finally:
            with self._admission:
                self._closed = True
            if conn is not None:
                conn.close()
            # No dejar productores esperando si el escritor falla inesperadamente.
            while not self._queue.empty():
                command = self._queue.get_nowait()
                if command is not None and not command[0].done():
                    command[0].set_exception(LibraryError(command[1], 'writer_stopped'))
                self._queue.task_done()

    def submit(self, operation, *args, **kwargs):
        if operation not in WRITES:
            raise ValueError(f'Unknown write: {operation}')
        future = Future()
        # El productor no puede cambiar el lote mientras espera su turno.
        command = future, operation, copy.deepcopy(args), copy.deepcopy(kwargs)
        with self._admission:
            if self._closed:
                future.set_exception(LibraryError(operation, 'service_closed'))
            else:
                try:
                    self._queue.put_nowait(command)
                except Full:
                    future.set_exception(LibraryError(operation, 'queue_full'))
        return future

    def read(self, operation, *args, cancel_event=None, **kwargs):
        if operation not in READS:
            raise ValueError(f'Unknown read: {operation}')
        with self._admission:
            if self._closed:
                raise LibraryError(operation, 'service_closed')
        try:
            with closing(open_reader(self.db_path)) as conn:
                if cancel_event is not None:
                    if cancel_event.is_set():
                        raise LibraryError(operation, 'cancelled')
                    conn.set_progress_handler(lambda: int(cancel_event.is_set()), 1000)
                # Count + página y consultas relacionadas ven la misma revisión.
                conn.execute('BEGIN')
                return getattr(LibraryRepository(conn), operation)(*args, **kwargs)
        except sqlite3.Error as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise LibraryError(operation, 'cancelled') from exc
            raise LibraryError(operation, str(exc)) from exc

    def close(self):
        if threading.current_thread() is self._thread:
            raise RuntimeError('Cannot join writer from writer')
        with self._admission:
            first_close = not self._closed
            self._closed = True
        if first_close and self._thread.is_alive():
            # Drenar sólo comandos aceptados; no requiere event loop de Qt.
            self._queue.put(None)
        self._thread.join()


_services = {}
_registry_lock = threading.Lock()
_accepting = True


def get_library_service(db_path=None):
    path = str(Path(db_path or ProjectPaths.root() / 'panopticon.db').resolve())
    key = os.path.normcase(path)
    with _registry_lock:
        if not _accepting:
            raise LibraryError('startup', 'application_closed')
        if key not in _services:
            _services[key] = LibraryService(path)
        return _services[key]


def close_library_services(*, permanent=False):
    global _accepting
    with _registry_lock:
        if permanent:
            _accepting = False
        services = tuple(_services.values())
        _services.clear()
    for service in services:
        service.close()


atexit.register(close_library_services)
