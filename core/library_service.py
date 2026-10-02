"""Un escritor por DB y lecturas breves en el hilo que las solicita.

submit() entrega un Future sin esperar a SQLite. La fachada antigua conserva
llamadas síncronas hasta migrar las vistas a entrega asíncrona por Qt.
"""
import atexit
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import Future
from contextlib import closing, ExitStack, contextmanager
from pathlib import Path
from queue import Queue, Full, Empty

from core.library_connections import open_reader
from core.library_metrics import LibraryMetrics
from core.library_wal import WalSampler
from core.library_revision import RevisionClock
from core.library_results import ReadResultError
from core.library_payload import snapshot as snapshot_write_payload, snapshot_read
from core.library_repository import LibraryRepository
from core.library_writer_guard import writer_guard
from core.library_versions_schema import backup_database, migrate as migrate_versions, upgrade_table, ready as versions_ready
from core.paths import ProjectPaths

log = logging.getLogger(__name__)
READS = frozenset({
    'get_path_identity_ingest_state', 'get_path_identity_status', 'get_path_identity_matches', 'get_path_identity_resolution',
    'get_path_identity_records', 'get_path_identity_review_page',
    'get_metadata_edit_history_record', 'get_metadata_edit_state', 'get_metadata_edit_receipt', 'get_generation_history_status', 'get_generation_history_page',
    'get_metadata_path_status', 'get_derived_copies_status', 'get_derived_copy_receipt',
    'get_inventory_page', 'get_known_files_in_folder', 'get_files_recursive', 'get_watched_folders',
    'get_folder_count', 'get_folder_preview', 'search_by_terms', 'get_file_id',
    'get_tags_for_file', 'search_files_paginated', 'get_folders_paginated',
    'count_files_missing_hash', 'get_files_missing_hash', 'get_tags_page', 'get_all_tags',
    'get_file_rating', 'count_files', 'get_curation_snapshot', 'get_curation_restore_state',
    'get_library_overview', 'get_library_selection', 'get_library_page',
    'resolve_library_selection', 'check_fts',
    'get_copy_versions_page', 'get_copy_version_state',
    'get_transfer_status', 'get_transfer_receipt', 'get_transfer_sources',
    'get_duplicate_snapshot',
    'get_duplicate_recovery_state',
    'get_duplicate_retirement_roots',
    'get_duplicate_candidates_page', 'get_duplicate_candidate_copies',
    'get_duplicate_search_status',
    'get_folder_overlap',
    'get_folder_archive_preview',
    'get_phash_registry_status', 'get_phash_page',
    'get_phash_states',
})

# Agrupar puede recorrer toda la DB aun con LIMIT. Acotar duración y permitir
# cancelación sin mantener indefinidamente un lector que retenga el WAL.
DEFAULT_READ_BUDGET_SECONDS = 10
READ_BUDGET_SECONDS = {
    'get_metadata_edit_state': 10,
    'get_metadata_edit_history_record': 10,
    'get_metadata_edit_receipt': 10,
    'get_derived_copy_receipt': 10,
    'get_metadata_path_status': 10,
    'get_generation_history_page': 10,
    'get_curation_snapshot': 10,
    'get_folder_archive_preview': 10,
    'get_duplicate_snapshot': 10,
    'get_duplicate_recovery_state': 10,
    'get_duplicate_candidates_page': 10,
    'get_duplicate_candidate_copies': 10,
    'get_folder_overlap': 10,
    'get_phash_page': 10,
    'get_phash_states': 10,
}
WRITES = frozenset({
    'prepare_path_identity', 'store_path_identity_batch',
    'prepare_transfers', 'approve_transfer', 'commit_transfer', 'abort_transfer', 'retire_transfer_copy', 'approve_transfer_restore',
    'approve_metadata_backup_purge',
    'prepare_generation_history', 'approve_metadata_edit', 'commit_metadata_edit',
    'prepare_derived_copies', 'approve_derived_copy', 'register_derived_copy',
    'register_catalog_files_observed',
    'register_files_observed', 'register_files_minimal', 'update_file_metadata', 'remove_files',
    'archive_missing_copies',
    'add_watched_folder', 'remove_watched_folder', 'add_tag_to_file',
    'archive_watched_folder_reviewed',
    'remove_tag_from_file', 'set_hash_original_bulk', 'update_file_rating',
    'rebuild_fts', 'repair_fts', 'vacuum_database', 'edit_curation', 'restore_curation', 'register_curation_copy', 'adopt_curation_identity',
    'archive_copy', 'make_copy_online',
    'archive_duplicate_copies',
    'restore_duplicate_copy',
    'approve_duplicate_purge',
    'prepare_duplicate_search',
    'prepare_phash_registry', 'store_phash_batch',
})


# These successful Native commands can represent published/replaced image bytes.
# Observation-only registration/hash and ordinary curation retain physical caches.
PREVIEW_WRITES = frozenset({'commit_metadata_edit', 'update_file_metadata',
                          'register_derived_copy', 'restore_duplicate_copy', 'commit_transfer', 'retire_transfer_copy', 'abort_transfer'})


class LibraryError(RuntimeError):
    def __init__(self, operation, reason):
        self.operation = operation
        self.reason = reason
        super().__init__(f'{operation}: {reason}')


class LibraryQueueFull(LibraryError):
    """Admission failed; no command entered FIFO or started SQL."""
    def __init__(self, operation):
        super().__init__(operation, 'queue_full')


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
        self._wal = WalSampler(self.db_path, self._metrics)
        self._revision = RevisionClock()
        from core.path_identity_clock import CoverageCache
        self._path_coverage = CoverageCache()
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
        repo = None
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
            repo = LibraryRepository(conn, cache_derived_paths=True, path_coverage_cache=self._path_coverage)
            self._revision.poll(conn)
            self._wal.poll()
            self._ready.set_result(True)
            while True:
                self._wal.poll()
                try:
                    command = self._queue.get(timeout=.1)
                except Empty:
                    self._revision.poll(conn)
                    # Admission is closed before exit: no command can appear
                    # after the drained queue is observed under this lock.
                    with self._admission:
                        if self._closed and self._queue.empty(): break
                    continue
                try:
                    if command is None:
                        break
                    self._revision.poll(conn)
                    future, operation, args, kwargs, submitted = command
                    started = self._metrics.start_write(operation, submitted)
                    if not future.set_running_or_notify_cancel():
                        self._metrics.finish_write('cancelled', started)
                        started = None
                        continue
                    try:
                        changes_before = conn.total_changes
                        if operation == 'prepare_path_identity':
                            from core.path_identity_registry import needs_prepare
                            if needs_prepare(conn):
                                backup_database(conn, self.db_path, 'path-identity')
                        if operation == 'prepare_transfers':
                            from core.library_transfers import status
                            if not status(conn)['prepared']:
                                backup_database(conn, self.db_path, 'transfers')
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
                            if conn.total_changes != changes_before:
                                self._revision.committed(previews_changed=operation in PREVIEW_WRITES)
                        finally:
                            self._metrics.observe('commit', time.perf_counter() - phase)
                        repo.finish_path_ingest(True)
                        repo.finish_derived_paths(True, operation)
                        self._metrics.finish_write('committed', started)
                        started = None
                    except BaseException as exc:
                        repo.finish_path_ingest(False)
                        repo.finish_derived_paths(False, operation)
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
                self._revision.stopped()
                if repo is not None:
                    repo.finish_derived_paths(False, 'shutdown')
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
                self._wal.stop()
                ownership.close()

    def submit(self, operation, *args, **kwargs):
        if operation not in WRITES:
            raise ValueError(f'Unknown write: {operation}')
        future = Future()
        # Reject closed/full admission before copying any producer data.
        with self._admission:
            if self._closed:
                self._metrics.reject_closed()
                future.set_exception(LibraryError(operation, 'service_closed'))
                return future
            if self._queue.full():
                self._metrics.reject_full()
                future.set_exception(LibraryQueueFull(operation))
                return future
        try:
            saved_args, saved_kwargs = snapshot_write_payload(args, kwargs)
        except ValueError as exc:
            self._metrics.reject_payload()
            future.set_exception(LibraryError(operation, str(exc)))
            return future
        command = future, operation, saved_args, saved_kwargs, time.perf_counter()
        # Another producer or shutdown can win during the bounded snapshot.
        with self._admission:
            if self._closed:
                self._metrics.reject_closed()
                future.set_exception(LibraryError(operation, 'service_closed'))
            else:
                try:
                    self._metrics.enqueue(self._queue, command)
                except Full:
                    future.set_exception(LibraryQueueFull(operation))
        return future

    def register_files_observed(self, records, *, cancel_event=None):
        """Worker-only, bounded observations; migration stays explicit."""
        from core.library_path_ingest import register
        event = cancel_event if cancel_event is not None else threading.Event()
        return register(self, records, event)

    def backfill_path_identity(self, **kwargs):
        """Explicit worker operation, bounded pages and confirmed progress."""
        from core.library_path_migration import backfill
        return backfill(self, **kwargs)

    def resolve_path_identity(self, path, *, cancel_event=None):
        """Worker-only Windows lookup and conservative Native review result."""
        from core.library_path_migration import resolve
        return resolve(self, path, cancel_event=cancel_event)

    def publish_path_identity_page(self, page, *, cancel_event=None):
        """Worker-only: recheck physical evidence before bounded FIFO SQL CAS."""
        from core.library_path_identity import publish_page
        return publish_page(self, page, cancel_event=cancel_event)

    def inspect_path_identity_page(self, **kwargs):
        """Worker-only preparation; filesystem probes run outside SQL snapshots."""
        from core.library_path_identity import inspect_page
        return inspect_page(self, **kwargs)

    def read(self, operation, *args, cancel_event=None, **kwargs):
        return self._read(operation, args, kwargs, cancel_event, lambda: open_reader(self.db_path))

    def _read(self, operation, args, kwargs, cancel_event, reader_factory):
        if operation not in READS:
            raise ValueError(f'Unknown read: {operation}')
        with self._admission:
            if self._closed:
                self._metrics.reject_closed(read=True)
                raise LibraryError(operation, 'service_closed')
        started = self._metrics.start_read()
        outcome = 'failed'
        timed_out = False
        # Preserve the timeout reason of existing bounded discovery APIs.
        timeout_reason = ('duplicate_discovery_budget_exceeded'
                          if operation in READ_BUDGET_SECONDS else 'read_budget_exceeded')
        budget = READ_BUDGET_SECONDS.get(operation, DEFAULT_READ_BUDGET_SECONDS)
        deadline = time.monotonic() + budget
        def check():
            if cancel_event is not None and cancel_event.is_set():
                raise LibraryError(operation, 'cancelled')
            if time.monotonic() >= deadline:
                raise LibraryError(operation, timeout_reason)
        try:
            check()
            try:
                saved_args, saved_kwargs = snapshot_read(args, kwargs)
            except ValueError as exc:
                self._metrics.reject_payload(read=True)
                raise LibraryError(operation, str(exc)) from exc
            check()
            with closing(reader_factory()) as conn:
                def interrupt():
                    nonlocal timed_out
                    timed_out = time.monotonic() >= deadline
                    return int(timed_out or (cancel_event is not None and cancel_event.is_set()))
                conn.set_progress_handler(interrupt, 1000)
                check()
                # Count + página y consultas relacionadas ven la misma revisión.
                conn.execute('BEGIN')
                value = getattr(LibraryRepository(conn, path_coverage_cache=self._path_coverage), operation)(*saved_args, **saved_kwargs)
                # Python result work may not execute enough SQLite instructions
                # to invoke the handler. Never deliver a late/cancelled result.
                check()
                outcome = 'completed'
                return value
        except ReadResultError as exc:
            outcome = 'failed'
            try:
                check()
            except LibraryError as interrupted:
                if interrupted.reason == 'cancelled':
                    outcome = 'cancelled'
                raise
            if str(exc) == 'read_result_too_large':
                self._metrics.reject_result()
            raise LibraryError(operation, str(exc)) from exc
        except sqlite3.Error as exc:
            outcome = 'failed'
            if cancel_event is not None and cancel_event.is_set():
                outcome = 'cancelled'
                raise LibraryError(operation, 'cancelled') from exc
            if timed_out:
                raise LibraryError(operation, timeout_reason) from exc
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

    @contextmanager
    def inventory_reader(self, *, cancel_event=None):
        """Un lector del worker, sin transacción retenida entre páginas."""
        from core.library_inventory import InventoryReader
        operation = 'get_inventory_page'
        with self._admission:
            if self._closed:
                self._metrics.reject_closed(read=True)
                raise LibraryError(operation, 'service_closed')
        if cancel_event is not None and cancel_event.is_set():
            raise LibraryError(operation, 'cancelled')
        with closing(open_reader(self.db_path)) as conn:
            reader = InventoryReader(self, conn, cancel_event)
            try:
                yield reader
            finally:
                reader.close()

    @contextmanager
    def metadata_destination_guard(self, *, cancel_event=None):
        """Lector privado del lote; sin snapshot WAL retenido entre imágenes."""
        from core.metadata_destinations import DestinationPaths
        operation = 'get_metadata_path_status'
        def check_open():
            with self._admission:
                if self._closed:
                    self._metrics.reject_closed(read=True)
                    raise LibraryError(operation, 'service_closed')
        check_open()
        with closing(open_reader(self.db_path)) as conn:
            guard = DestinationPaths(conn, self.db_path,
                normalize=LibraryRepository(conn)._normalize_path,
                check_open=check_open, metrics=self._metrics,
                error=LibraryError, cancel_event=cancel_event)
            try:
                yield guard
            finally:
                guard.close()

    def revision_snapshot(self):
        """Epoch, sequence, monitor health; constant-cost, without SQL/filesystem IO.

        Ephemeral invalidation hint, not a durable receipt or approval fingerprint.
        External commits are coalesced by persistent owner data_version polling.
        """
        return self._revision.snapshot()

    def preview_revision_snapshot(self):
        """Local coarse hint for published bytes/unknown external DB changes."""
        return self._revision.preview_snapshot()

    def metrics_snapshot(self):
        """Contadores/timings locales, sin SQL, stat, payloads ni espera del dueño.

        BEGIN incluye espera por el escritor SQLite, pero no distingue su causa.
        WAL: tamaño físico muestreado por el dueño como máximo1Hz, edad/salud.
        No equivale a frames pendientes ni informa checkpoints o memoria total.
        Busy mide ocupación continua, no el lector/elemento más antiguo ni locks.
        """
        return self._metrics.snapshot()

    def request_close(self):
        """Reject new admission and let the owner drain accepted commands.

        No queue insertion or join: safe even when the bounded queue is full.
        Already admitted readers own their connections; their producer must
        finish before application shutdown proceeds.
        """
        with self._admission:
            if not self._closed:
                self._closed = True
                self._metrics.closed()

    def shutdown_ready(self):
        """No SQL or wait; true only after owner cleanup and guard release."""
        return not self._thread.is_alive()

    def close(self):
        """Blocking compatibility API for tests/CLI; GUI uses request_close."""
        if threading.current_thread() is self._thread:
            raise RuntimeError('Cannot join writer from writer')
        self.request_close()
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


def request_close_library_services(*, permanent=False):
    """Close registry admission; keep owners retained until final close."""
    global _accepting
    with _registry_lock:
        if permanent: _accepting = False
        services = tuple(_services.values())
    for service in services: service.request_close()


def library_services_shutdown_ready():
    with _registry_lock:
        services = tuple(_services.values())
    return all(service.shutdown_ready() for service in services)


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
