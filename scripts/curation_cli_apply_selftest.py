"""CLI por copia con revisión previa, procesos reales y SQLite temporal Windows."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.curation_sidecar import export_curation, NAME
from core.library_connections import open_reader
from core.library_repository import LibraryRepository
from core.library_service import LibraryService, LibraryError
import curation_snapshot_selftest as helpers

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/curation_sidecar.py'


class CLIApply(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.folder = Path(self.temp.name) / 'colección ñ'
        self.folder.mkdir()
        self.paths = [(self.folder / name).as_posix() for name in ('one.png', 'two.png')]
        data = b'portable fixture bytes'
        for path in self.paths:
            Path(path).write_bytes(data)
        self.service.submit('register_files_minimal', [dict(path=path, filename=Path(path).name,
            size=len(data), created=1, hash_original=hashlib.sha256(data).hexdigest()) for path in self.paths]).result(3)
        self.service.submit('edit_curation', self.paths[0], add_tags=['exported'], rating=5).result(3)
        self.sql("UPDATE files SET meta_model='preserved model',meta_positive='preserved prompt' WHERE filename='one.png'")
        export_curation(self.service.db_path, self.folder)
        self.original = self.state(self.paths[0])
        self.other = self.state(self.paths[1])
        self.artifact = (self.folder / NAME).read_bytes()

    def sql(self, query, args=()):
        with closing(sqlite3.connect(self.service.db_path)) as conn, conn:
            conn.execute('PRAGMA foreign_keys=ON')
            return conn.execute(query, args).fetchall()

    def state(self, path):
        with closing(open_reader(self.service.db_path)) as conn:
            conn.execute('BEGIN')
            return LibraryRepository(conn).get_curation_restore_state(path)

    def compare(self):
        result = self.run_cli('compare')
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        reports = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(reports[-1]['report_complete'])
        return reports[0]

    def params(self, report, mode):
        result = ['--mode', mode, '--copy-id', str(report['copy_id']), '--relative-path', report['relative_path'],
            '--export-id', report['export_id']]
        if report['fingerprint'] is not None:
            result += ['--expected-fingerprint', report['fingerprint']]
        return result

    def command(self, operation, extras=(), db=None):
        return [sys.executable, str(SCRIPT), operation, '--db', str(db or self.service.db_path),
            '--folder', str(self.folder), *extras]

    def run_cli(self, operation, extras=(), db=None):
        return subprocess.run(self.command(operation, extras, db), cwd=ROOT, capture_output=True, timeout=10)

    def apply(self, report, mode='restore', close=True, db=None):
        if close:
            self.service.close()
        return self.run_cli('apply', self.params(report, mode), db)

    def conflict(self):
        self.service.submit('edit_curation', self.paths[0], remove_tags=['exported'], rating=0).result(3)
        return self.compare()

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertTrue(json.loads(result.stdout)['applied'])
        self.assertEqual((self.folder / NAME).read_bytes(), self.artifact)

    def assert_error(self, result, reason):
        self.assertEqual(result.returncode, 1, result.stdout.decode(errors='replace'))
        self.assertEqual(json.loads(result.stderr)['error'], reason)
        self.assertEqual(result.stdout, b'')

    def test_compare_emits_token_copy_and_fingerprint_of_shown_state_without_writes(self):
        report = self.compare()
        self.assertEqual(report['fingerprint'], self.original['fingerprint'])
        self.assertEqual(report['central'], self.original['state'])
        self.assertEqual(report['copy_id'], report['sidecar']['id'])
        self.assertEqual(len(report['export_id']), 32)
        self.assertEqual(self.state(self.paths[0]), self.original)
        self.assertEqual((self.folder / NAME).read_bytes(), self.artifact)

    def test_explicit_restore_preserves_other_identical_copy_generation_and_bytes(self):
        report = self.conflict()
        before = [Path(path).read_bytes() for path in self.paths]
        self.assert_success(self.apply(report))
        self.assertEqual(self.state(self.paths[0]), self.original)
        self.assertEqual(self.state(self.paths[1]), self.other)
        self.assertEqual([Path(path).read_bytes() for path in self.paths], before)
        self.assertEqual(self.sql('SELECT meta_model,meta_positive FROM files WHERE filename=?', ('one.png',)),
            [('preserved model', 'preserved prompt')])

    def test_exported_zero_and_empty_tags_are_restored_explicitly(self):
        self.service.submit('edit_curation', self.paths[0], remove_tags=['exported'], rating=0).result(3)
        export_curation(self.service.db_path, self.folder)
        self.artifact = (self.folder / NAME).read_bytes()
        self.service.submit('edit_curation', self.paths[0], add_tags=['newer'], rating=4).result(3)
        self.assert_success(self.apply(self.compare()))
        state = self.state(self.paths[0])['state']
        self.assertEqual((state['rating'], state['tags']), (0, []))

    def test_moved_folder_registers_one_copy_and_keeps_old_paths(self):
        old_paths = list(self.paths)
        moved = Path(self.temp.name) / 'moved ñ'
        self.folder.rename(moved)
        self.folder = moved
        new_path = (moved / 'one.png').as_posix()
        report = self.compare()
        self.assertEqual(report['status'], 'unregistered')
        self.assertIsNone(report['fingerprint'])
        self.assert_success(self.apply(report, 'register'))
        after = self.state(new_path)['state']
        self.assertEqual((after['rating'], after['tags']), (5, self.original['state']['tags']))
        self.assertNotEqual(after['id'], self.original['state']['id'])
        self.assertEqual(self.state(old_paths[0]), self.original)
        self.assertEqual(self.state(old_paths[1]), self.other)
        self.assertEqual(self.sql('SELECT count(*) FROM files'), [(3,)])
        self.assertEqual(self.sql('SELECT meta_model,meta_positive FROM files WHERE path=?', (new_path,)), [(None, None)])

    def test_adoption_completes_only_null_hash_pair_and_preserves_record(self):
        self.conflict()
        self.sql('UPDATE files SET hash_original=NULL,hash_mod=NULL WHERE path=?', (self.paths[0],))
        report = self.compare()
        self.assertEqual(report['status'], 'central_identity_missing')
        self.assert_success(self.apply(report, 'adopt'))
        self.assertEqual(self.state(self.paths[0]), self.original)
        self.assertEqual(self.state(self.paths[1]), self.other)

    def test_active_application_owner_rejects_cli_before_file_hash(self):
        report = self.conflict()
        Path(self.paths[0]).unlink()
        self.assert_error(self.apply(report, close=False), 'database_writer_active')
        self.assertEqual(self.state(self.paths[0])['state']['rating'], 0)

    def test_stale_native_approval_is_never_refreshed(self):
        report = self.conflict()
        self.service.submit('edit_curation', self.paths[0], add_tags=['newer'], rating=2).result(3)
        before = self.state(self.paths[0])
        self.assert_error(self.apply(report), 'curation_changed_since_review')
        self.assertEqual(self.state(self.paths[0]), before)

    def test_changed_export_token_rejects_prior_review(self):
        report = self.conflict()
        export_curation(self.service.db_path, self.folder)
        before = self.state(self.paths[0])
        self.assert_error(self.apply(report), 'sidecar_changed')
        self.assertEqual(self.state(self.paths[0]), before)

    def test_changed_bytes_do_not_adopt_identity_or_curation(self):
        self.conflict()
        self.sql('UPDATE files SET hash_original=NULL,hash_mod=NULL WHERE path=?', (self.paths[0],))
        report = self.compare()
        before = self.state(self.paths[0])
        Path(self.paths[0]).write_bytes(b'changed bytes')
        self.assert_error(self.apply(report, 'adopt'), 'curation_hash_mismatch')
        self.assertEqual(self.state(self.paths[0]), before)

    def test_shared_category_conflict_is_left_unresolved(self):
        report = self.conflict()
        self.service.submit('edit_curation', self.paths[1], add_tags=['exported'], rating=3).result(3)
        self.sql("UPDATE tags SET category='different' WHERE name='exported'")
        before = [self.state(path) for path in self.paths]
        self.assert_error(self.apply(report), 'tag_category_conflict')
        self.assertEqual([self.state(path) for path in self.paths], before)

    def test_missing_db_is_not_created(self):
        path = Path(self.temp.name) / 'missing.db'
        result = self.apply(self.conflict(), db=path)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(path.exists())
        self.assertFalse(Path(str(path) + '.panopticon-writer.guard').exists())
        self.assertFalse(list(Path(self.temp.name).glob('*.pre-integrity-*.bak')))

    def test_foreign_sqlite_is_not_migrated_or_modified(self):
        path = Path(self.temp.name) / 'foreign.db'
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('CREATE TABLE foreign_data(value)')
            conn.execute("INSERT INTO foreign_data VALUES('preserve')")
        before = path.read_bytes()
        self.assert_error(self.apply(self.conflict(), db=path), 'library_not_ready')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path(str(path) + '.panopticon-writer.guard').exists())
        self.assertFalse(list(Path(self.temp.name).glob('*.pre-integrity-*.bak')))

    def test_initialized_non_wal_database_is_not_converted(self):
        report = self.conflict()
        self.service.close()
        self.assertEqual(self.sql('PRAGMA journal_mode=DELETE'), [('delete',)])
        before = Path(self.service.db_path).read_bytes()
        self.assert_error(self.apply(report), 'library_not_ready')
        self.assertEqual(Path(self.service.db_path).read_bytes(), before)
        self.assertEqual(self.sql('PRAGMA journal_mode'), [('delete',)])

    def test_require_ready_api_never_migrates_and_cannot_create_missing_database(self):
        self.service.close()
        with patch('core.library_service._migrate', side_effect=AssertionError('migration')):
            service = LibraryService(self.service.db_path, require_ready=True)
            try:
                self.assertEqual(service.read('count_files'), 2)
            finally:
                service.close()
            path = Path(self.temp.name) / 'missing.db'
            with self.assertRaises(LibraryError):
                LibraryService(path, require_ready=True)
            self.assertFalse(path.exists())

    def test_unknown_marker_and_invalid_policy_are_rejected(self):
        self.service.close()
        self.sql("UPDATE panopticon_schema SET value='unknown' WHERE name='integrity_v1'")
        with self.assertRaisesRegex(LibraryError, 'library_not_ready'):
            LibraryService(self.service.db_path, require_ready=True)
        with patch('core.library_service.threading.Thread', side_effect=AssertionError('thread')):
            for value in (None, 1, 'true'):
                with self.assertRaisesRegex(ValueError, 'invalid_ready_policy'):
                    LibraryService(self.service.db_path, require_ready=value)

    def test_adopt_command_cannot_replace_known_identity(self):
        report = self.conflict()
        before = self.state(self.paths[0])
        self.assert_error(self.apply(report, 'adopt'), 'identity_already_known')
        self.assertEqual(self.state(self.paths[0]), before)

    def test_required_cli_approval_fields_are_not_inferred(self):
        report = self.conflict()
        result = self.run_cli('apply')
        self.assertEqual(result.returncode, 2)
        result = self.run_cli('apply', self.params(report, 'register'))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.state(self.paths[0])['state']['rating'], 0)

    def test_failed_copy_verification_closes_owner_and_allows_reviewed_retry(self):
        report = self.conflict()
        invalid = dict(report, relative_path='another.png')
        self.assert_error(self.apply(invalid), 'sidecar_changed')
        service = LibraryService(self.service.db_path, require_ready=True)
        service.close()
        self.assert_success(self.apply(report))

    def test_two_processes_with_same_review_only_one_applies(self):
        report = self.conflict()
        self.service.close()
        processes = [subprocess.Popen(self.command('apply', self.params(report, 'restore')),
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        try:
            results = [process.communicate(timeout=10) for process in processes]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual(sorted(process.returncode for process in processes), [0, 1])
        for process, (output, error) in zip(processes, results):
            if process.returncode == 0:
                self.assertTrue(json.loads(output)['applied'])
            else:
                self.assertIn(json.loads(error)['error'], ('database_writer_active', 'curation_changed_since_review'))
        self.assertEqual(self.state(self.paths[0]), self.original)
        self.assertEqual(self.state(self.paths[1]), self.other)
        self.assertFalse(any(thread.name == 'PanopticonDB' for thread in threading.enumerate()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
