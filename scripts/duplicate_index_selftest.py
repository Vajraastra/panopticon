"""Preparación opcional, idempotencia, schema/WAL/rollback y mantenimiento SQLite."""
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.duplicate_candidates import page, copies, DIGEST, VALID
from core.duplicate_index import NAME, DDL, status, prepare
from core.library_repository import LibraryRepository
from core.library_connections import open_reader
import duplicate_candidates_selftest as prior
import duplicate_exact_review_selftest as helpers


class Index(prior.Queries):
    def test_prepare_requires_explicit_transaction(self):
        self.conn.commit()
        with self.assertRaisesRegex(RuntimeError, 'requires_transaction'):
            prepare(self.conn)
        self.assertFalse(status(self.conn)['prepared'])

    def test_read_discovery_and_status_do_not_create_schema(self):
        self.row()
        self.row()
        before = self.conn.execute('SELECT * FROM sqlite_master').fetchall()
        page(self.conn)
        copies(self.conn, prior.A)
        self.assertFalse(status(self.conn)['prepared'])
        self.assertEqual(self.conn.execute('SELECT * FROM sqlite_master').fetchall(), before)

    def test_idempotent_prepare_without_second_ddl_or_row_changes(self):
        self.row()
        self.row()
        before = self.conn.execute('SELECT * FROM files').fetchall()
        self.assertIs(prepare(self.conn), True)
        sql = []
        self.conn.set_trace_callback(sql.append)
        self.assertIs(prepare(self.conn), True)
        self.assertFalse(any('CREATE' in query.upper() for query in sql))
        self.assertEqual(self.conn.execute('SELECT * FROM files').fetchall(), before)

    def test_custom_index_name_conflict_preserves_existing_index(self):
        self.row()
        self.conn.execute(f'CREATE INDEX {NAME} ON files(path)')
        before = self.conn.execute('SELECT * FROM sqlite_master').fetchall()
        with self.assertRaisesRegex(ValueError, 'name_conflict'):
            prepare(self.conn)
        with self.assertRaisesRegex(ValueError, 'name_conflict'):
            status(self.conn)
        self.assertEqual(self.conn.execute('SELECT * FROM sqlite_master').fetchall(), before)

    def test_custom_table_name_conflict_preserves_table(self):
        self.row()
        self.conn.execute(f'CREATE TABLE {NAME}(data TEXT)')
        with self.assertRaisesRegex(ValueError, 'name_conflict'):
            prepare(self.conn)
        self.assertEqual(self.conn.execute(f'SELECT count(*) FROM {NAME}').fetchone(), (0,))

    def test_different_literal_case_is_not_accepted_as_same_predicate(self):
        self.row()
        self.conn.execute(DDL.replace("='text'", "='TEXT'"))
        with self.assertRaisesRegex(ValueError, 'name_conflict'):
            status(self.conn)

    def test_partial_index_matches_all_candidates_and_query_planner(self):
        for digest in (prior.A, prior.B):
            self.row(digest)
            self.row(digest)
        self.row(prior.A, offline=1)
        self.row(None)
        before = page(self.conn)
        prepare(self.conn)
        self.assertEqual(page(self.conn), before)
        plan = self.conn.execute(f'EXPLAIN QUERY PLAN SELECT {DIGEST},count(*) FROM files WHERE {VALID} '
                                 'GROUP BY 1 HAVING count(*)>1 ORDER BY 1 LIMIT 26').fetchall()
        self.assertTrue(any(NAME in row[-1] for row in plan))

    def test_offline_and_hash_changes_maintain_index_without_manual_rebuild(self):
        self.row(prior.A)
        self.row(prior.A)
        self.row(prior.B)
        prepare(self.conn)
        self.conn.execute('UPDATE files SET hash_mod=? WHERE id=2', (prior.B,))
        self.assertEqual(page(self.conn)['groups'][0]['digest'], prior.B)
        self.conn.execute('UPDATE files SET is_offline=1 WHERE id=3')
        self.assertEqual(page(self.conn)['groups'], [])
        self.conn.execute('UPDATE files SET hash_mod=? WHERE id=2', ('',))
        self.assertEqual(page(self.conn)['groups'][0]['digest'], prior.A)
        self.assertEqual(self.conn.execute('PRAGMA quick_check').fetchone(), ('ok',))

    def test_forced_timeout_rolls_back_index_then_next_sql_succeeds(self):
        for _ in range(1200):
            self.row()
        self.conn.commit()
        before = self.conn.execute('SELECT count(*) FROM files').fetchone()
        self.conn.execute('BEGIN IMMEDIATE')
        with patch('core.duplicate_index.MAX_BUILD_SECONDS', 0):
            with self.assertRaisesRegex(ValueError, 'build_budget_exceeded'):
                prepare(self.conn)
        self.conn.rollback()
        self.assertFalse(status(self.conn)['prepared'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM files').fetchone(), before)
        self.conn.execute('BEGIN IMMEDIATE')
        prepare(self.conn)
        self.conn.commit()
        self.assertTrue(status(self.conn)['prepared'])

    def test_sqlite_ddl_failure_does_not_drop_or_rewrite_files(self):
        self.row()
        before = self.conn.execute('SELECT * FROM files').fetchall()
        self.conn.execute('PRAGMA query_only=ON')
        with self.assertRaises(sqlite3.OperationalError):
            prepare(self.conn)
        self.assertEqual(self.conn.execute('SELECT * FROM files').fetchall(), before)
        self.assertFalse(status(self.conn)['prepared'])


class Service(helpers.Review):
    def test_prepare_preserves_ids_curation_sequences_hashes_and_fts(self):
        raw, curated = self.pair()
        states = self.service.read('get_duplicate_snapshot', [raw.as_posix(),curated.as_posix()])
        with closing(open_reader(self.service.db_path)) as conn:
            sequence = conn.execute('SELECT * FROM sqlite_sequence').fetchall()
            fts = conn.execute('SELECT * FROM files_fts').fetchall()
        self.assertFalse(self.service.read('get_duplicate_search_status')['prepared'])
        self.assertIs(self.service.submit('prepare_duplicate_search').result(3), True)
        self.assertTrue(self.service.read('get_duplicate_search_status')['prepared'])
        self.assertEqual(self.service.read('get_duplicate_snapshot', [raw.as_posix(),curated.as_posix()]), states)
        with closing(open_reader(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM sqlite_sequence').fetchall(), sequence)
            self.assertEqual(conn.execute('SELECT * FROM files_fts').fetchall(), fts)

    def test_reader_sees_index_only_after_confirmed_commit(self):
        self.pair()
        prepared, release = threading.Event(), threading.Event()
        native = LibraryRepository.prepare_duplicate_search
        def blocked(repo):
            result = native(repo)
            prepared.set()
            self.assertTrue(release.wait(3))
            return result
        with patch.object(LibraryRepository, 'prepare_duplicate_search', blocked):
            future = self.service.submit('prepare_duplicate_search')
            try:
                self.assertTrue(prepared.wait(1))
                self.assertFalse(future.done())
                self.assertFalse(self.service.read('get_duplicate_search_status')['prepared'])
            finally:
                release.set()
            self.assertIs(future.result(3), True)
        self.assertTrue(self.service.read('get_duplicate_search_status')['prepared'])

    def test_timeout_rolls_back_owner_transaction_and_next_write_survives(self):
        self.service.submit('register_files_minimal', [dict(path=f'E:/fixture/{i}.png',
            filename=f'{i}.png',size=1,created=1,hash_original=prior.A) for i in range(1200)]).result(5)
        with patch('core.duplicate_index.MAX_BUILD_SECONDS', 0):
            with self.assertRaisesRegex(Exception, 'build_budget_exceeded'):
                self.service.submit('prepare_duplicate_search').result(3)
        self.assertFalse(self.service.read('get_duplicate_search_status')['prepared'])
        self.assertEqual(self.service.read('count_files'), 1200)
        self.assertIs(self.service.submit('prepare_duplicate_search').result(3), True)


if __name__ == '__main__':
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('Index.'+name, sys.modules[__name__])
        for name in Index.__dict__ if name.startswith('test_'))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromName('Service.'+name, sys.modules[__name__])
        for name in Service.__dict__ if name.startswith('test_'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
