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
import time
from concurrent.futures import Future
from contextlib import closing, ExitStack
from pathlib import Path
from queue import Queue, Full

from core.library_connections import open_reader
from core.library_metrics import LibraryMetrics
from core.library_repository import LibraryRepository
from core.library_writer_guard import writer_guard
from core.library_versions_schema import backup_database, migrate as migrate_versions, upgrade_table, ready as versions_ready
from core.paths import ProjectPaths

log = logging.getLogger(__name__)
READS = frozenset({
    'get_known_files_in_folder', 'get_files_recursive', 'get_watched_folders',
    'get_folder_count', 'get_folder_preview', 'search_by_terms', 'get_file_id',
    'get_tags_for_file', 'search_files_paginated', 'get_folders_paginated',
    'count_files_missing_hash', 'get_files_missing_hash', 'get_all_tags',
    'get_file_rating', 'count_files', 'get_curation_snapshot', 'get_curation_restore_state',
    'get_library_overview', 'get_library_selection', 'get_library_page',
    'resolve_library_selection', 'check_fts',
    'get_copy_versions_page', 'get_copy_version_state',
    'get_duplicate_snapshot',
    'get_duplicate_recovery_state',
    'get_duplicate_retirement_roots',
    'get_duplicate_candidates_page', 'get_duplicate_candidate_copies',
    'get_duplicate_search_status',
    'get_folder_overlap',
    'get_phash_registry_status', 'get_phash_page',
    'get_phash_states',
})

# Agrupar puede recorrer toda la DB aun con LIMIT. Acotar duración y permitir
# cancelación sin mantener indefinidamente un lector que retenga el WAL.
READ_BUDGET_SECONDS = {
    'get_duplicate_recovery_state': 10,
    'get_duplicate_candidates_page': 10,
    'get_duplicate_candidate_copies': 10,
    'get_folder_overlap': 10,
    'get_phash_page': 10,
    'get_phash_states': 10,
}
WRITES = frozenset({
    'register_files_minimal', 'update_file_metadata', 'remove_files',
    'add_watched_folder', 'remove_watched_folder', 'add_tag_to_file',
    'remove_tag_from_file', 'set_hash_original_bulk', 'update_file_rating',
    'rebuild_fts', 'repair_fts', 'vacuum_database', 'edit_curation', 'restore_curation', 'register_curation_copy', 'adopt_curation_identity',
    'archive_copy', 'make_copy_online',
    'archive_duplicate_copies',
    'prepare_duplicate_search',
    'prepare_phash_registry', 'store_phash_batch',
})


class LibraryError(RuntimeError):
    def __init__(self, operation, reason):
        self.operation = operation
        self.reason = reason
        super().__init__(f'{operation}: {reason}')


def require_ready_schema(conn):
    """Apertura estricta: no crear/migrar ni aceptar un marker desconocido."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='panopticon_schema'").fetchone():
        raise ValueError('library_not_ready')
    if conn.execute("SELECT value FROM panopticon_schema WHERE name='integrity_v1'").fetchone() != ('1',):
        raise ValueError('library_not_ready')
    if not versions_ready(conn):
        raise ValueError('library_not_ready')


def _migrate(conn, db_path):
    # Marca propia: no reutilizar user_version de una instalación previa.
    marker = conn.execute("SELECT 1 FROM sqlite_master WHERE name='panopticon_schema'").fetchone()
    ready = marker and conn.execute(
        "SELECT value FROM panopticon_schema WHERE name='integrity_v1'").fetchone()
    if ready:
        if ready != ('1',):
            raise ValueError('library_not_ready')
        migrate_versions(conn, db_path)
        return
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='files'").fetchone():
        # Backup SQLite consistente, incluyendo WAL. Nunca copiar sólo el .db.
        backup_path = backup_database(conn, db_path, 'integrity')
        log.info('Respaldo previo a migración: %s', backup_path)
    repo = LibraryRepository(conn)
    conn.execute('PRAGMA foreign_keys=OFF')
    conn.execute('BEGIN IMMEDIATE')
    try:
        repo.create_schema()
        repo.run_transformations()
        upgrade_table(conn)
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
    finally:
        conn.execute('PRAGMA foreign_keys=ON')
        if conn.execute('PRAGMA foreign_keys').fetchone() != (1,):
            raise sqlite3.DatabaseError('foreign_keys unavailable')


class LibraryService:
    def __init__(self, db_path, capacity=64, *, require_ready=False):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('invalid_queue_capacity')
        if type(require_ready) is not bool:
            raise ValueError('invalid_ready_policy')
        self._require_ready = require_ready
        self.db_path = str(Path(db_path).resolve())
        self._queue = Queue(maxsize=capacity)
        self._metrics = LibraryMetrics(capacity)
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
        started = None
        ownership = ExitStack()
        try:
            ownership.enter_context(writer_guard(self.db_path))
            if self._require_ready:
                conn = sqlite3.connect(Path(self.db_path).as_uri() + '?mode=rw',
                    uri=True, timeout=5, isolation_level=None)
            else:
                conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
            conn.execute('PRAGMA foreign_keys=ON')
            if conn.execute('PRAGMA foreign_keys').fetchone() != (1,):
                raise sqlite3.DatabaseError('foreign_keys unavailable')
            if self._require_ready:
                require_ready_schema(conn)
                if conn.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
                    raise ValueError('library_not_ready')
            else:
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
                    future, operation, args, kwargs, submitted = command
                    started = self._metrics.start_write(operation, submitted)
                    if not future.set_running_or_notify_cancel():
                        self._metrics.finish_write('cancelled', started)
                        started = None
                        continue
                    try:
                        if operation != 'vacuum_database':
                            phase = time.perf_counter()
                            try:
                                conn.execute('BEGIN IMMEDIATE')
                            finally:
                                self._metrics.observe('begin', time.perf_counter() - phase)
                        value = getattr(repo, operation)(*args, **kwargs)
                        if value is not True:
                            raise LibraryError(operation, 'operation_failed')
                        phase = time.perf_counter()
                        try:
                            conn.commit()
                        finally:
                            self._metrics.observe('commit', time.perf_counter() - phase)
                        self._metrics.finish_write('committed', started)
                        started = None
                    except BaseException as exc:
                        conn.rollback()
                        self._metrics.finish_write('failed', started)
                        started = None
                        future.set_exception(LibraryError(operation, str(exc)))
                    else:
                        future.set_result(value)
                finally:
                    self._queue.task_done()
        except BaseException as exc:
            if started is not None:
                self._metrics.finish_write('failed', started)
                started = None
            if not self._ready.done():
                self._ready.set_exception(LibraryError('startup', str(exc)))
            else:
                log.exception('El escritor central se detuvo')
                if command is not None and not command[0].done():
                    command[0].set_exception(LibraryError(command[1], 'writer_stopped'))
        finally:
            try:
                with self._admission:
                    self._closed = True
                    self._metrics.closed()
                if conn is not None:
                    conn.close()
                # No dejar productores esperando si el escritor falla inesperadamente.
                while not self._queue.empty():
                    command = self._queue.get_nowait()
                    if command is not None:
                        self._metrics.discard(cancelled=command[0].cancelled())
                        if not command[0].done():
                            command[0].set_exception(LibraryError(command[1], 'writer_stopped'))
                    self._queue.task_done()
            finally:
                ownership.close()

    def submit(self, operation, *args, **kwargs):
        if operation not in WRITES:
            raise ValueError(f'Unknown write: {operation}')
        future = Future()
        # El productor no puede cambiar el lote mientras espera su turno.
        command = future, operation, copy.deepcopy(args), copy.deepcopy(kwargs), time.perf_counter()
        with self._admission:
            if self._closed:
                self._metrics.reject_closed()
                future.set_exception(LibraryError(operation, 'service_closed'))
            else:
                try:
                    self._metrics.enqueue(self._queue, command)
                except Full:
                    future.set_exception(LibraryError(operation, 'queue_full'))
        return future

    def read(self, operation, *args, cancel_event=None, **kwargs):
        if operation not in READS:
            raise ValueError(f'Unknown read: {operation}')
        with self._admission:
            if self._closed:
                self._metrics.reject_closed(read=True)
                raise LibraryError(operation, 'service_closed')
        started = self._metrics.start_read()
        outcome = 'failed'
        timed_out = False
        try:
            with closing(open_reader(self.db_path)) as conn:
                if cancel_event is not None:
                    if cancel_event.is_set():
                        raise LibraryError(operation, 'cancelled')
                budget = READ_BUDGET_SECONDS.get(operation)
                if budget is not None or cancel_event is not None:
                    deadline = time.monotonic()+budget if budget is not None else None
                    def interrupt():
                        nonlocal timed_out
                        timed_out = deadline is not None and time.monotonic() >= deadline
                        return int(timed_out or (cancel_event is not None and cancel_event.is_set()))
                    conn.set_progress_handler(interrupt, 1000)
                # Count + página y consultas relacionadas ven la misma revisión.
                conn.execute('BEGIN')
                value = getattr(LibraryRepository(conn), operation)(*args, **kwargs)
                outcome = 'completed'
                return value
        except sqlite3.Error as exc:
            outcome = 'failed'
            if cancel_event is not None and cancel_event.is_set():
                outcome = 'cancelled'
                raise LibraryError(operation, 'cancelled') from exc
            if timed_out:
                raise LibraryError(operation, 'duplicate_discovery_budget_exceeded') from exc
            raise LibraryError(operation, str(exc)) from exc
        except LibraryError as exc:
            outcome = 'failed'
            if exc.reason == 'cancelled':
                outcome = 'cancelled'
            raise
        except BaseException:
            outcome = 'failed'
            raise
        finally:
            self._metrics.finish_read(outcome, started)

    def metrics_snapshot(self):
        """Contadores/timings locales, sin SQL, stat, payloads ni espera del dueño.

        BEGIN incluye espera por el escritor SQLite, pero no distingue su causa.
        No informa frames/checkpoints WAL ni mide memoria/bytes o cambios externos.
        """
        return self._metrics.snapshot()

    def close(self):
        if threading.current_thread() is self._thread:
            raise RuntimeError('Cannot join writer from writer')
        with self._admission:
            first_close = not self._closed
            self._closed = True
            self._metrics.closed()
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
