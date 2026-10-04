"""Exclusión del dueño central entre procesos Windows, sólo bases temporales."""
from contextlib import chdir, closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.library_service import LibraryService, LibraryError
from core.library_writer_guard import SUFFIX, MAGIC
from core.library_repository import LibraryRepository
from core.library_startup import create_library_window, MESSAGES
import curation_snapshot_selftest as helpers

ROOT = Path(__file__).resolve().parents[1]


class WriterGuard(helpers.Fixture):
    def new(self, path):
        service = LibraryService(path)
        self.addCleanup(service.close)
        return service

    def test_second_owner_is_rejected_before_sqlite_or_migration(self):
        before = {thread.ident for thread in threading.enumerate()}
        start = time.monotonic()
        with patch('core.library_service.sqlite3.connect', side_effect=AssertionError('SQLite opened')):
            with self.assertRaisesRegex(LibraryError, 'database_writer_active'):
                LibraryService(self.service.db_path)
        self.assertLess(time.monotonic() - start, .2)
        self.assertEqual({thread.ident for thread in threading.enumerate()}, before)
        self.assertTrue(self.service.submit('register_files_minimal', []).result(3))

    def test_another_process_is_rejected_while_reader_remains_usable(self):
        self.register('C:/fixture.png')
        code = 'import sys; from core.library_service import LibraryService,LibraryError\ntry:\n LibraryService(sys.argv[1])\nexcept LibraryError as error:\n print(error.reason); sys.exit(7)'
        result = subprocess.run([sys.executable, '-c', code, self.service.db_path], cwd=ROOT,
            capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 7, result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout.decode().strip(), 'database_writer_active')
        with closing(sqlite3.connect(f'file:{Path(self.service.db_path).as_posix()}?mode=ro', uri=True)) as reader:
            self.assertEqual(reader.execute('SELECT count(*) FROM files').fetchone()[0], 1)

    def test_release_on_close_allows_new_owner_without_deleting_guard(self):
        self.register('C:/fixture.png')
        path = self.service.db_path
        guard = Path(path + SUFFIX)
        inode = guard.stat().st_ino
        self.service.close()
        other = self.new(path)
        self.assertEqual(other.read('count_files'), 1)
        self.assertEqual(guard.stat().st_ino, inode)
        other.close()
        self.assertEqual(guard.read_bytes(), MAGIC)

    def test_different_databases_have_independent_owners(self):
        other = self.new(Path(self.temp.name) / 'other.db')
        self.register('C:/one.png')
        other.submit('register_files_minimal', [dict(path='C:/two.png', filename='two.png', size=1, created=1)]).result(3)
        self.assertEqual(self.service.read('get_file_id', 'C:/two.png'), None)
        self.assertEqual(other.read('get_file_id', 'C:/one.png'), None)

    def test_failed_migration_releases_ownership_and_stops_thread(self):
        path = Path(self.temp.name) / 'failed.db'
        before = {thread.ident for thread in threading.enumerate()}
        with patch('core.library_service._migrate', side_effect=RuntimeError('forced_migration')):
            with self.assertRaisesRegex(LibraryError, 'forced_migration'):
                LibraryService(path)
        self.assertEqual({thread.ident for thread in threading.enumerate()}, before)
        self.assertEqual(self.new(path).read('count_files'), 0)

    def test_fatal_writer_failure_releases_ownership_after_pending_errors(self):
        self.register('C:/fixture.png')
        def broken(repo, *args):
            repo.conn.close()
            raise RuntimeError('forced_fatal')
        with patch.object(LibraryRepository, 'update_file_rating', broken):
            future = self.service.submit('update_file_rating', 'C:/fixture.png', 4)
            with self.assertRaisesRegex(LibraryError, 'writer_stopped'):
                future.result(3)
        self.service.close()
        other = self.new(self.service.db_path)
        self.assertEqual(other.read('get_file_rating', 'C:/fixture.png'), 0)

    def test_close_drains_accepted_write_before_releasing_ownership(self):
        self.register('C:/fixture.png')
        entered, release = threading.Event(), threading.Event()
        original = LibraryRepository.update_file_rating
        def blocked(repo, *args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('fixture_timeout')
            return original(repo, *args)
        closer = threading.Thread(target=self.service.close)
        try:
            with patch.object(LibraryRepository, 'update_file_rating', blocked):
                future = self.service.submit('update_file_rating', 'C:/fixture.png', 4)
                self.assertTrue(entered.wait(3))
                closer.start()
                with self.assertRaisesRegex(LibraryError, 'database_writer_active'):
                    LibraryService(self.service.db_path)
                release.set()
                self.assertTrue(future.result(3))
                closer.join(3)
                self.assertFalse(closer.is_alive())
        finally:
            release.set()
            if closer.ident is not None:
                closer.join(3)
        self.assertEqual(self.new(self.service.db_path).read('get_file_rating', 'C:/fixture.png'), 4)

    def test_foreign_guard_is_preserved_and_no_database_is_created(self):
        path = Path(self.temp.name) / 'foreign.db'
        guard = Path(str(path) + SUFFIX)
        guard.write_bytes(b'foreign control file')
        with patch('core.library_service.sqlite3.connect', side_effect=AssertionError('SQLite opened')):
            with self.assertRaisesRegex(LibraryError, 'invalid_database_guard'):
                LibraryService(path)
        self.assertFalse(path.exists())
        self.assertEqual(guard.read_bytes(), b'foreign control file')

    def test_hardlinked_database_alias_is_rejected_before_creating_second_guard(self):
        alias = Path(self.temp.name) / 'alias.db'
        os.link(self.service.db_path, alias)
        try:
            with patch('core.library_service.sqlite3.connect', side_effect=AssertionError('SQLite opened')):
                with self.assertRaisesRegex(LibraryError, 'ambiguous_database_alias'):
                    LibraryService(alias)
            self.assertFalse(Path(str(alias) + SUFFIX).exists())
        finally:
            alias.unlink()

    def test_relative_and_windows_case_aliases_share_guard(self):
        # A relative Windows path cannot cross drives (checkout E:, temp C:).
        with chdir(self.temp.name):
            aliases = [os.path.relpath(self.service.db_path)]
            if os.name == 'nt':
                aliases.append(self.service.db_path.upper())
            for alias in aliases:
                with self.assertRaisesRegex(LibraryError, 'database_writer_active'):
                    LibraryService(alias)

    def test_ownership_remains_held_until_sqlite_connection_is_closed(self):
        path = Path(self.temp.name) / 'closing.db'
        entered, release = threading.Event(), threading.Event()
        original = sqlite3.connect
        class Closing(sqlite3.Connection):
            def close(conn):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('fixture_timeout')
                return super().close()
        def connect(*args, **kwargs):
            return original(*args, **kwargs, factory=Closing)
        with patch('core.library_service.sqlite3.connect', connect):
            service = self.new(path)
            closer = threading.Thread(target=service.close)
            try:
                closer.start()
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(LibraryError, 'database_writer_active'):
                    LibraryService(path)
            finally:
                release.set()
                closer.join(3)
            self.assertFalse(closer.is_alive())
        self.assertEqual(self.new(path).read('count_files'), 0)

    def test_actual_process_death_releases_guard_and_keeps_committed_state(self):
        path = Path(self.temp.name) / 'child.db'
        marker = Path(self.temp.name) / 'ready'
        code = 'import sys; from pathlib import Path; from core.library_service import LibraryService\ns=LibraryService(sys.argv[1]); s.submit("register_files_minimal",[dict(path="C:/child.png",filename="child.png",size=1,created=1)]).result(); s.submit("update_file_rating","C:/child.png",4).result(); Path(sys.argv[2]).write_text("ready"); sys.stdin.buffer.read(1)'
        child = subprocess.Popen([sys.executable, '-c', code, str(path), str(marker)], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(marker.exists())
            with self.assertRaisesRegex(LibraryError, 'database_writer_active'):
                LibraryService(path)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=3)
        self.assertEqual(self.new(path).read('get_file_rating', 'C:/child.png'), 4)

    def test_actual_process_death_during_transaction_rolls_back_before_new_owner(self):
        path = Path(self.temp.name) / 'interrupted.db'
        code = 'import os,sys; from unittest.mock import patch; from core.library_service import LibraryService; from core.library_repository import LibraryRepository\ns=LibraryService(sys.argv[1]); s.submit("register_files_minimal",[dict(path="C:/child.png",filename="child.png",size=1,created=1)]).result(); s.submit("update_file_rating","C:/child.png",1).result()\ndef crash(repo,*args):\n repo.conn.execute("UPDATE files SET rating=5"); os._exit(19)\nwith patch.object(LibraryRepository,"update_file_rating",crash):\n s.submit("update_file_rating","C:/child.png",5).result()'
        result = subprocess.run([sys.executable, '-c', code, str(path)], cwd=ROOT, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 19, result.stderr.decode(errors='replace'))
        self.assertEqual(self.new(path).read('get_file_rating', 'C:/child.png'), 1)

    def test_startup_presents_actual_busy_owner_in_both_languages(self):
        for language in ('en', 'es'):
            values = json.loads((ROOT / 'locales' / (language + '.json')).read_bytes())
            messages = []
            result = create_library_window(lambda: LibraryService(self.service.db_path),
                lambda key, default='': values.get(key, default), lambda *args: messages.append(args))
            self.assertIsNone(result)
            self.assertEqual(messages, [(values['app.library_unavailable'], values['app.library_error.database_writer_active'])])
        self.assertTrue(self.service.submit('register_files_minimal', []).result(3))

    def test_other_library_errors_are_presented_without_claiming_success(self):
        for language in ('en', 'es'):
            values = json.loads((ROOT / 'locales' / (language + '.json')).read_bytes())
            for reason in (*MESSAGES, 'disk failure'):
                messages = []
                def failed():
                    raise LibraryError('startup', reason)
                self.assertIsNone(create_library_window(failed, lambda key, default='': values.get(key, default),
                    lambda *args: messages.append(args)))
                key = 'app.library_error.' + reason
                self.assertEqual(messages[0][1], values[key] if key in values else values['app.library_error'].format(reason=reason))

    def test_successful_window_factory_returns_window_without_notification(self):
        window = object()
        self.assertIs(create_library_window(lambda: window, lambda *args: '',
            lambda *args: self.fail('unexpected notification')), window)

    def test_programming_error_is_not_hidden_as_database_startup_failure(self):
        def failed():
            raise TypeError('programming error')
        with self.assertRaisesRegex(TypeError, 'programming error'):
            create_library_window(failed, lambda *args: '', lambda *args: self.fail('hidden error'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
