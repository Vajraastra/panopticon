"""Integridad/concurrencia central en fixtures; sin DB de biblioteca o Cherry."""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from core.database_manager import DatabaseManager
from core.library_connections import open_reader
from core.library_repository import LibraryRepository
from core.library_service import (LibraryError, LibraryService, close_library_services,
                                  get_library_service)
from core.paths import ProjectPaths


class LibrarySafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'central # ñ.db'
        self.db = DatabaseManager(self.path)
        self.addCleanup(close_library_services)
        self.file = self.root / 'images' / 'seed.png'
        self.file.parent.mkdir()
        self.file.write_bytes(b'fixture')
        self.assertTrue(self.db.register_files_minimal([self.observation(self.file)]))
        self.assertTrue(self.db.update_file_rating(str(self.file), 5))
        self.assertTrue(self.db.add_tag_to_file(str(self.file), 'preserved'))
        self.id = self.db.get_file_id(str(self.file))

    def observation(self, path):
        return dict(path=str(path), filename=path.name, size=7, created=1)

    def sql(self, statement, params=()):
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute(statement, params).fetchall()
            conn.commit()
            return rows

    def test_one_writer_for_all_managers_and_no_exposed_connection(self):
        with patch.object(LibraryRepository, 'create_schema', side_effect=AssertionError('migration repeated')):
            peers = [DatabaseManager(self.path) for _ in range(20)]
        self.assertTrue(all(p.service is self.db.service for p in peers))
        self.assertFalse(hasattr(self.db, 'conn'))
        self.assertNotEqual(self.db.service._thread.ident, threading.get_ident())

    def test_failed_batch_cannot_be_committed_by_gui_rating(self):
        entered, release = threading.Event(), threading.Event()
        original = LibraryRepository._fts_sync_file
        new_file = self.root / 'new.png'
        writer_ids = []

        def fail_after_insert(repo, file_id, cursor=None):
            writer_ids.append(threading.get_ident())
            if file_id != self.id:
                entered.set()
                if not release.wait(3):
                    raise AssertionError('fixture release timeout')
                raise sqlite3.OperationalError('injected FTS failure')
            return original(repo, file_id, cursor)

        try:
            with patch.object(LibraryRepository, '_fts_sync_file', fail_after_insert):
                batch = self.db.service.submit('register_files_minimal', [self.observation(new_file)])
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.db.count_files(), 1)
                rating = self.db.service.submit('update_file_rating', str(self.file), 3)
                self.assertFalse(rating.done())
                release.set()
                with self.assertRaises(LibraryError):
                    batch.result(3)
                self.assertTrue(rating.result(3))
        finally:
            release.set()
        self.assertEqual(set(writer_ids), {self.db.service._thread.ident})
        self.assertIsNone(self.db.get_file_id(str(new_file)))
        self.assertEqual(self.db.get_file_rating(str(self.file)), 3)
        self.assertEqual(self.sql('SELECT count(*) FROM files_fts'), [(1,)])
        self.assertEqual(self.db.get_tags_for_file(str(self.file)), ['preserved'])

    def test_reader_connections_are_owned_and_closed_in_calling_threads(self):
        ownership = []
        guard = threading.Lock()

        def reader(path):
            conn = open_reader(path)
            with guard:
                ownership.append((threading.get_ident(), conn))
            return conn

        with patch('core.library_service.open_reader', reader):
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(list(pool.map(lambda _: self.db.count_files(), range(20))), [1] * 20)
        self.assertTrue(all(owner != self.db.service._thread.ident for owner, _ in ownership))
        self.assertEqual(len({id(conn) for _, conn in ownership}), 20)
        # Closed comprobado en el mismo hilo para no confundirlo con check_same_thread.
        with closing(open_reader(self.path)) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('DELETE FROM files')
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')

    def test_read_count_and_page_share_snapshot(self):
        original = LibraryRepository.search_files_paginated
        def snapshot(repo, *args, **kwargs):
            before = repo.count_files()
            self.assertTrue(self.db.register_files_minimal([self.observation(self.root / 'later.png')]))
            result = original(repo, *args, **kwargs)
            self.assertEqual(result[0], before)
            return result
        with patch.object(LibraryRepository, 'search_files_paginated', snapshot):
            count, rows = self.db.search_files_paginated()
        self.assertEqual((count, len(rows)), (1, 1))
        self.assertEqual(self.db.count_files(), 2)

    def test_all_fts_mutations_rollback_and_do_not_leak_to_next_commit(self):
        operations = [
            lambda: self.db.add_tag_to_file(str(self.file), 'failed-tag'),
            lambda: self.db.remove_tag_from_file(str(self.file), 'preserved'),
            lambda: self.db.update_file_metadata(str(self.file), {'positive': 'failed prompt'}),
            lambda: self.db.register_files_minimal([self.observation(self.root / 'failed.png')]),
        ]
        before = self.sql('SELECT * FROM files'), self.sql('SELECT * FROM file_tags'), self.sql('SELECT * FROM tags')
        for operation in operations:
            with self.subTest(operation=operation), patch.object(LibraryRepository, '_fts_sync_file',
                                                  side_effect=sqlite3.OperationalError('injected')):
                self.assertFalse(operation())
            self.assertTrue(self.db.update_file_rating(str(self.file), 5))
            self.assertEqual((self.sql('SELECT * FROM files'), self.sql('SELECT * FROM file_tags'),
                              self.sql('SELECT * FROM tags')), before)

    def test_lazy_registration_tag_and_fts_are_one_transaction(self):
        new = self.root / 'new-real.png'
        new.write_bytes(b'fixture')
        original = LibraryRepository._fts_sync_file
        calls = []
        def fail_second(repo, file_id, cursor=None):
            calls.append(file_id)
            if len(calls) == 2:
                raise sqlite3.OperationalError('tag FTS failed')
            return original(repo, file_id, cursor)
        with patch.object(LibraryRepository, '_fts_sync_file', fail_second):
            self.assertFalse(self.db.add_tag_to_file(str(new), 'failed-lazy'))
        self.assertIsNone(self.db.get_file_id(str(new)))
        self.assertNotIn('failed-lazy', self.db.get_all_tags())
        self.assertEqual(self.sql('SELECT count(*) FROM files_fts'), [(1,)])

    def test_invalid_foreign_key_is_rejected_and_rolled_back(self):
        def corrupt(repo, file_id, cursor=None):
            repo.conn.execute('INSERT INTO file_tags VALUES(?,?,?)', (99999, 99999, 'bad'))
        with patch.object(LibraryRepository, '_fts_sync_file', corrupt):
            self.assertFalse(self.db.add_tag_to_file(str(self.file), 'bad'))
        self.assertNotIn('bad', self.db.get_all_tags())
        self.assertEqual(self.sql('PRAGMA foreign_key_check'), [])

    def test_removal_archives_identity_links_and_fts(self):
        self.assertTrue(self.db.remove_files([str(self.file)]))
        self.assertEqual(self.sql('SELECT count(*) FROM file_tags'), [(1,)])
        self.assertEqual(self.sql('SELECT count(*) FROM files_fts'), [(1,)])
        self.assertEqual(self.sql('SELECT is_offline FROM files'), [(1,)])
        self.assertIsNone(self.db.get_file_id(str(self.file)))
        self.assertEqual(self.db.get_all_tags(), ['preserved'])

    def test_failed_watch_removal_restores_curation_and_watch(self):
        self.assertTrue(self.db.add_watched_folder(str(self.file.parent)))
        self.sql("CREATE TRIGGER fail_archive BEFORE UPDATE OF is_offline ON files BEGIN SELECT RAISE(ABORT,'fixture'); END")
        self.assertFalse(self.db.remove_watched_folder(str(self.file.parent)))
        self.assertTrue(self.db.update_file_rating(str(self.file), 4))
        self.assertEqual(self.db.get_tags_for_file(str(self.file)), ['preserved'])
        self.assertEqual(self.db.get_watched_folders(), [self.file.parent.as_posix()])

    def test_bulk_hash_failure_rolls_back_first_update(self):
        other = self.root / 'other.png'
        self.assertTrue(self.db.register_files_minimal([self.observation(other)]))
        other_id = self.db.get_file_id(str(other))
        self.sql(f"CREATE TRIGGER fail_hash BEFORE UPDATE OF hash_original ON files "
                 f"WHEN new.id={other_id} BEGIN SELECT RAISE(ABORT,'fixture'); END")
        self.assertFalse(self.db.set_hash_original_bulk([('first', self.id), ('second', other_id)]))
        self.assertTrue(self.db.update_file_rating(str(self.file), 4))
        self.assertEqual(self.sql('SELECT hash_original FROM files'), [(None,), (None,)])

    def test_rebuild_failure_restores_existing_fts(self):
        before = self.sql('SELECT * FROM files_fts')
        with patch.object(LibraryRepository, '_fts_sync_file', side_effect=sqlite3.OperationalError('fixture')):
            self.assertFalse(self.db.rebuild_fts())
        self.assertEqual(self.sql('SELECT * FROM files_fts'), before)

    def test_migration_quarantines_orphans_keeps_valid_curation_and_backup(self):
        close_library_services()
        self.sql("DELETE FROM panopticon_schema WHERE name='integrity_v1'")
        self.sql('INSERT INTO file_tags VALUES(99999,1,?)', ('historic',))
        self.db = DatabaseManager(self.path)
        self.assertEqual(self.db.get_tags_for_file(str(self.file)), ['preserved'])
        self.assertEqual(self.db.get_file_rating(str(self.file)), 5)
        self.assertEqual(self.sql('SELECT file_id,source FROM orphaned_file_tags'), [(99999, 'historic')])
        self.assertEqual(self.sql('PRAGMA foreign_key_check'), [])
        backups = list(self.root.glob('*.bak'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM file_tags').fetchone(), (2,))
        close_library_services()
        DatabaseManager(self.path)
        self.assertEqual(len(list(self.root.glob('*.bak'))), 1)

    def test_migration_failure_rolls_back_and_stops_admission(self):
        close_library_services()
        self.sql("DELETE FROM panopticon_schema WHERE name='integrity_v1'")
        with patch.object(LibraryRepository, 'run_transformations', side_effect=sqlite3.OperationalError('migration')):
            with self.assertRaises(LibraryError):
                DatabaseManager(self.path)
        self.assertEqual(self.sql("SELECT * FROM panopticon_schema WHERE name='integrity_v1'"), [])
        self.assertEqual(self.sql('SELECT rating FROM files'), [(5,)])
        self.assertFalse(any(t.name == 'PanopticonDB' for t in threading.enumerate()))

    def test_bounded_queue_freezes_payload_and_close_drains_accepted_work(self):
        service = LibraryService(self.root / 'queue.db', capacity=1)
        self.addCleanup(service.close)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = LibraryRepository.register_files_minimal
        def hold(repo, rows):
            entered.set()
            if not release.wait(3):
                raise AssertionError('fixture timeout')
            return original(repo, rows)
        with patch.object(LibraryRepository, 'register_files_minimal', hold):
            first = service.submit('register_files_minimal', [self.observation(self.root / 'one.png')])
            self.assertTrue(entered.wait(3))
            payload = [self.observation(self.root / 'two.png')]
            second = service.submit('register_files_minimal', payload)
            payload[0]['path'] = str(self.root / 'mutated.png')
            with self.assertRaisesRegex(LibraryError, 'queue_full'):
                service.submit('register_files_minimal', []).result(1)
            closer = threading.Thread(target=service.close)
            closer.start()
            try:
                with self.assertRaisesRegex(LibraryError, 'service_closed'):
                    service.submit('register_files_minimal', []).result(1)
                release.set()
                self.assertTrue(first.result(3))
                self.assertTrue(second.result(3))
            finally:
                release.set()
                closer.join(3)
            self.assertFalse(closer.is_alive())
        self.assertFalse(service._thread.is_alive())
        with closing(sqlite3.connect(service.db_path)) as conn:
            paths = {r[0] for r in conn.execute('SELECT path FROM files')}
        self.assertEqual(paths, {(self.root / 'one.png').as_posix(), (self.root / 'two.png').as_posix()})

    def test_async_submission_allows_qt_events_while_writer_waits(self):
        entered, release = threading.Event(), threading.Event()
        original = LibraryRepository.update_file_rating
        def hold(repo, *args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('fixture timeout')
            return original(repo, *args)
        ticks = []
        try:
            with patch.object(LibraryRepository, 'update_file_rating', hold):
                future = self.db.service.submit('update_file_rating', str(self.file), 2)
                self.assertTrue(entered.wait(3))
                QTimer.singleShot(0, lambda: ticks.append(True))
                self.app.processEvents()
                self.assertEqual(ticks, [True])
                self.assertFalse(future.done())
                release.set()
                self.assertTrue(future.result(3))
        finally:
            release.set()

    def test_read_error_is_distinct_from_empty_and_unknown_commands_rejected(self):
        self.sql('DROP TABLE watched_folders')
        with self.assertRaises(LibraryError):
            self.db.get_watched_folders()
        with self.assertRaises(ValueError):
            self.db.service.submit('create_schema')
        with self.assertRaises(ValueError):
            self.db.service.read('remove_files')

    def test_probe_uses_readonly_factory_without_starting_service(self):
        from core import library_probe
        with patch.object(ProjectPaths, 'root', return_value=self.root):
            self.assertEqual(library_probe.watched_folders(), [])
            self.assertFalse((self.root / 'panopticon.db').exists())

    def test_cancelled_queued_command_does_not_execute(self):
        entered, release = threading.Event(), threading.Event()
        original = LibraryRepository.update_file_rating
        def hold(repo, *args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('fixture timeout')
            return original(repo, *args)
        try:
            with patch.object(LibraryRepository, 'update_file_rating', hold):
                first = self.db.service.submit('update_file_rating', str(self.file), 2)
                self.assertTrue(entered.wait(3))
                cancelled = self.db.service.submit('update_file_rating', str(self.file), 1)
                self.assertTrue(cancelled.cancel())
                release.set()
                self.assertTrue(first.result(3))
        finally:
            release.set()
        self.db.service._queue.join()
        self.assertEqual(self.db.get_file_rating(str(self.file)), 2)

    def test_backup_failure_aborts_before_schema_changes_and_closes_handles(self):
        close_library_services()
        self.sql("DELETE FROM panopticon_schema WHERE name='integrity_v1'")
        original = sqlite3.connect
        class FailingBackup(sqlite3.Connection):
            def backup(self, *args, **kwargs):
                raise sqlite3.OperationalError('backup failed')
        def connect(*args, **kwargs):
            return original(*args, **kwargs, factory=FailingBackup)
        with patch('core.library_service.sqlite3.connect', connect):
            with self.assertRaisesRegex(LibraryError, 'backup failed'):
                DatabaseManager(self.path)
        self.assertEqual(self.sql('SELECT rating FROM files'), [(5,)])
        self.assertEqual(self.sql("SELECT * FROM panopticon_schema WHERE name='integrity_v1'"), [])
        self.assertEqual(list(self.root.glob('*.bak*')), [])

    def test_legacy_schema_upgrade_preserves_identity_and_builds_fts(self):
        path = self.root / 'legacy.db'
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, '
                         'filename TEXT, file_size INTEGER, width INTEGER, height INTEGER, '
                         'created_date TEXT, meta_tool TEXT, meta_model TEXT, meta_positive TEXT, '
                         'meta_negative TEXT, meta_seed TEXT)')
            LibraryRepository(conn).create_schema()
            conn.execute("INSERT INTO files(id,path,filename) VALUES(9,'legacy.png','legacy.png')")
            conn.execute("INSERT INTO tags VALUES(3,'legacy-curation','manual')")
            conn.execute("INSERT INTO file_tags VALUES(9,3,'manual')")
            conn.commit()
        db = DatabaseManager(path)
        self.assertEqual(db.get_file_id('legacy.png'), 9)
        self.assertEqual(db.get_tags_for_file('legacy.png'), ['legacy-curation'])
        self.assertEqual(db.get_file_rating('legacy.png'), 0)
        self.assertEqual(db.search_by_terms(['legacy-curation']), (1, ['legacy.png']))
        self.assertEqual(len(list(self.root.glob('legacy.db*.bak'))), 1)

    def test_application_shutdown_disallows_new_writer_creation(self):
        with patch('core.library_service._accepting', True):
            close_library_services(permanent=True)
            with self.assertRaisesRegex(LibraryError, 'application_closed'):
                DatabaseManager(self.path)
            self.assertFalse(self.db.service._thread.is_alive())

    def test_backfill_counts_only_confirmed_hashes_and_always_finishes(self):
        from modules.librarian.logic.hash_backfill import HashBackfillWorker
        from core.inventory import InventoryStatus
        worker = HashBackfillWorker(self.db)
        finished = []
        worker.finished_signal.connect(lambda done, failed: finished.append((done, failed)))
        with patch.object(self.db, 'set_hash_original_bulk', return_value=False):
            worker.run()
        self.assertEqual(finished, [(0, 1)])
        self.assertEqual(worker.outcome, InventoryStatus.ERROR)
        self.assertEqual(self.db.count_files_missing_hash(), 1)
        worker = HashBackfillWorker(self.db)
        with patch.object(self.db, 'count_files_missing_hash', side_effect=LibraryError('read', 'offline')):
            worker.finished_signal.connect(lambda done, failed: finished.append((done, failed)))
            worker.run()
        self.assertEqual(finished[-1], (0, 0))
        self.assertEqual(worker.outcome, InventoryStatus.ERROR)

    def test_backfill_success_stores_sha256(self):
        import hashlib
        from modules.librarian.logic.hash_backfill import HashBackfillWorker
        worker = HashBackfillWorker(self.db)
        finished = []
        worker.finished_signal.connect(lambda done, failed: finished.append((done, failed)))
        worker.run()
        self.assertEqual(finished, [(1, 0)])
        self.assertEqual(self.sql('SELECT hash_original FROM files'),
                         [(hashlib.sha256(b'fixture').hexdigest(),)])

    def test_librarian_and_gallery_use_injected_service(self):
        from modules.librarian.module import LibrarianModule
        from modules.gallery.logic.query_engine import QueryEngine
        from PySide6.QtWidgets import QMessageBox
        from core.inventory import InventoryStatus
        from types import SimpleNamespace
        module = LibrarianModule()
        self.assertIsNone(module.db)
        module.on_load({'data_service': self.db.service})
        self.assertIs(module.db.service, self.db.service)
        engine = QueryEngine(service=self.db.service)
        self.assertIs(engine.db.service, self.db.service)
        view = module.get_view()
        try:
            module.backfill_thread = SimpleNamespace(outcome=InventoryStatus.ERROR)
            with patch.object(QMessageBox, 'information') as success, patch.object(QMessageBox, 'warning') as warning:
                module.backfill_finished(0, 1)
                success.assert_not_called()
                warning.assert_called_once()
        finally:
            module._dirty_timer.stop()
            view.close()
            view.deleteLater()
            self.app.processEvents()

    def test_writer_fatal_error_resolves_active_and_queued_futures(self):
        entered, release = threading.Event(), threading.Event()
        def break_connection(repo, *args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('fixture timeout')
            repo.conn.close()
            return True
        try:
            with patch.object(LibraryRepository, 'update_file_rating', break_connection):
                first = self.db.service.submit('update_file_rating', str(self.file), 2)
                self.assertTrue(entered.wait(3))
                second = self.db.service.submit('update_file_rating', str(self.file), 1)
                release.set()
                for future in (first, second):
                    with self.assertRaisesRegex(LibraryError, 'writer_stopped'):
                        future.result(3)
        finally:
            release.set()
            self.db.service.close()
        self.assertEqual(self.sql('SELECT rating FROM files'), [(5,)])

    def test_writer_connection_is_created_and_closed_by_its_owner(self):
        original = sqlite3.connect
        operations = []
        class Tracked(sqlite3.Connection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                operations.append(('open', threading.get_ident()))
            def close(self):
                operations.append(('close', threading.get_ident()))
                return super().close()
        def connect(*args, **kwargs):
            return original(*args, **kwargs, factory=Tracked)
        with patch('core.library_service.sqlite3.connect', connect):
            service = LibraryService(self.root / 'owner.db')
            service.close()
        self.assertEqual(operations, [('open', service._thread.ident), ('close', service._thread.ident)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
