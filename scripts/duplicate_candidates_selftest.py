"""Discovery no autoriza descartes; SQL acotado y snapshot RO sin bloqueo escritor."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.duplicate_candidates import page, copies
from core.library_connections import open_reader
from core.library_repository import LibraryRepository
from core.library_service import LibraryError
import duplicate_exact_review_selftest as helpers

A, B, C = 'a'*64, 'b'*64, 'c'*64


class Queries(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT,hash_original,hash_mod,is_offline DEFAULT 0)')
        self.addCleanup(self.conn.close)
        self.counter = 0

    def row(self, digest=A, *, modified=None, offline=0, path=None):
        self.counter += 1
        self.conn.execute('INSERT INTO files(id,path,hash_original,hash_mod,is_offline) VALUES(?,?,?,?,?)',
            (self.counter, path or f'E:/collection/{self.counter}.png', digest, modified, offline))

    def test_cross_folder_candidates_only_counts_without_loading_states(self):
        self.row(path='C:/curated/a.png')
        self.row(path='E:/cherry/a.png')
        self.row(B)
        values = page(self.conn)['groups']
        self.assertEqual(values, [dict(digest=A, copies=2, extra_copies=1, reviewable=True)])
        self.assertNotIn('path', json.dumps(values))
        self.assertNotIn('remove', json.dumps(values))

    def test_current_modified_hash_precedes_original(self):
        self.row(A, modified=B)
        self.row(B)
        self.row(A)
        self.assertEqual(page(self.conn)['groups'][0]['digest'], B)
        self.assertEqual([r['copy_id'] for r in copies(self.conn, B)['copies']], [1, 2])

    def test_empty_modified_hash_falls_back_to_original(self):
        self.row(A, modified='')
        self.row(A)
        self.assertEqual(page(self.conn)['groups'][0]['digest'], A)

    def test_offline_history_excluded_and_preserved(self):
        self.row()
        self.row(offline=1)
        self.assertEqual(page(self.conn)['groups'], [])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM files').fetchone(), (2,))

    def test_unknown_invalid_and_blob_hashes_are_not_false_candidates(self):
        for digest in [None, '', 'a'*63, 'a'*65, 'g'*64, b'a'*64, 123]:
            self.row(digest)
            self.row(digest)
        self.assertEqual(page(self.conn)['groups'], [])

    def test_hash_case_normalized_without_database_rewrite(self):
        self.row(A.upper())
        self.row(A)
        before = self.conn.total_changes
        self.assertEqual(page(self.conn)['groups'][0]['copies'], 2)
        self.assertEqual(len(copies(self.conn, A)['copies']), 2)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.conn.execute('SELECT hash_original FROM files WHERE id=1').fetchone(), (A.upper(),))

    def test_keyset_pages_have_lookahead_no_missing_or_repeated_group(self):
        for index in range(45):
            self.row(f'{index:064x}')
            self.row(f'{index:064x}')
        found, cursor, requests = [], None, 0
        while True:
            values = page(self.conn, after_hash=cursor, limit=7)
            requests += 1
            found.extend(group['digest'] for group in values['groups'])
            cursor = values['next_after_hash']
            if not values['has_more']:
                break
        self.assertEqual(found, [f'{index:064x}' for index in range(45)])
        self.assertEqual(requests, 7)
        self.assertEqual(page(self.conn, after_hash=cursor)['groups'], [])

    def test_exact_last_page_does_not_require_empty_page(self):
        for digest in (A, B):
            self.row(digest)
            self.row(digest)
        first = page(self.conn, limit=1)
        second = page(self.conn, after_hash=first['next_after_hash'], limit=1)
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])
        self.assertEqual(second['next_after_hash'], B)

    def test_group_larger_than_limit_is_visible_but_never_truncated(self):
        for _ in range(257):
            self.row()
        self.assertFalse(page(self.conn)['groups'][0]['reviewable'])
        self.assertEqual(page(self.conn)['groups'][0]['copies'], 257)
        sql = []
        self.conn.set_trace_callback(sql.append)
        with self.assertRaisesRegex(ValueError, 'requires_separate_review'):
            copies(self.conn, A)
        self.assertFalse(any('substr(path' in query for query in sql))

    def test_256_member_group_returns_all_ids_and_full_paths(self):
        for _ in range(256):
            self.row()
        self.assertTrue(page(self.conn)['groups'][0]['reviewable'])
        values = copies(self.conn, A, expected_count=256)['copies']
        self.assertEqual(len(values), 256)
        self.assertEqual(values[-1]['copy_id'], 256)

    def test_changed_member_count_rejects_previous_listing(self):
        self.row()
        self.row()
        self.row()
        with self.assertRaisesRegex(ValueError, 'group_changed'):
            copies(self.conn, A, expected_count=2)

    def test_missing_or_singleton_group_is_not_returned_as_approval(self):
        with self.assertRaisesRegex(ValueError, 'group_missing'):
            copies(self.conn, A)
        self.row()
        with self.assertRaisesRegex(ValueError, 'group_missing'):
            copies(self.conn, A)

    def test_oversized_path_rejects_complete_group_without_prefix(self):
        self.row(path='E:/'+('a'*4097))
        self.row()
        with self.assertRaisesRegex(ValueError, 'payload_too_large'):
            copies(self.conn, A)

    def test_aggregate_serialization_budget_rejects_unicode_payload(self):
        for index in range(256):
            self.row(path='E:/'+('界'*4000)+f'/{index}')
        with self.assertRaisesRegex(ValueError, 'payload_too_large'):
            copies(self.conn, A)

    def test_invalid_controls_are_rejected_before_sql(self):
        sql = []
        self.conn.set_trace_callback(sql.append)
        for limit in (0, 101, True, 1.5):
            with self.assertRaises(ValueError):
                page(self.conn, limit=limit)
        for digest in ('', 'a', A.upper(), 'z'*64, 4):
            with self.assertRaises(ValueError):
                copies(self.conn, digest)
            with self.assertRaises(ValueError):
                page(self.conn, after_hash=digest)
        for count in (0, 1, True, 2.5):
            with self.assertRaises(ValueError):
                copies(self.conn, A, expected_count=count)
        self.assertEqual(sql, [])

    def test_custom_digest_column_does_not_override_group_expression(self):
        self.conn.execute('ALTER TABLE files ADD COLUMN digest TEXT')
        self.row(A)
        self.row(A)
        self.row(B)
        self.conn.execute("UPDATE files SET digest='same' WHERE id<>2")
        self.assertEqual(page(self.conn)['groups'][0]['digest'], A)
        self.assertEqual(page(self.conn)['groups'][0]['copies'], 2)

    def test_one_query_per_page_two_per_complete_group_and_no_ddl_dml(self):
        self.row()
        self.row()
        statements = []
        self.conn.set_trace_callback(statements.append)
        page(self.conn)
        copies(self.conn, A)
        self.assertEqual(len(statements), 3)
        self.assertTrue(all(query.startswith('SELECT') for query in statements))


class Service(helpers.Review):
    def test_service_reads_are_allowlisted_and_native_owner_receives_no_writes(self):
        raw, curated = self.pair()
        values = self.service.read('get_duplicate_candidates_page')
        digest = values['groups'][0]['digest']
        before = self.service.metrics_snapshot()
        members = self.service.read('get_duplicate_candidate_copies', digest, expected_count=2)
        self.assertEqual(len(members['copies']), 2)
        after = self.service.metrics_snapshot()
        for key in ('accepted', 'committed', 'write_count'):
            self.assertEqual(before[key], after[key])
        self.assertTrue(raw.is_file() and curated.is_file())

    def test_pre_cancelled_discovery_performs_no_read_sql(self):
        self.pair()
        event = threading.Event()
        event.set()
        with self.assertRaisesRegex(LibraryError, 'cancelled'):
            self.service.read('get_duplicate_candidates_page', cancel_event=event)
        self.assertEqual(self.service.read('count_files'), 2)

    def test_query_budget_fails_visibly_then_closes_reader_and_allows_next_action(self):
        self.service.submit('register_files_minimal', [dict(path=f'E:/fixture/{index}.png',
            filename=f'{index}.png', size=1, created=1, hash_original=A) for index in range(1500)]).result(5)
        with patch.dict('core.library_service.READ_BUDGET_SECONDS', get_duplicate_candidates_page=0):
            with self.assertRaisesRegex(LibraryError, 'duplicate_discovery_budget_exceeded'):
                self.service.read('get_duplicate_candidates_page')
        self.assertEqual(self.service.read('count_files'), 1500)
        self.assertEqual(self.service.read('get_duplicate_candidates_page')['groups'][0]['copies'], 1500)

    def test_count_and_members_share_ro_snapshot_while_writer_can_commit(self):
        self.pair()
        digest = self.service.read('get_duplicate_candidates_page')['groups'][0]['digest']
        native = LibraryRepository.get_duplicate_candidate_copies
        committed = []
        def intercept(repo, *args, **kwargs):
            def traced(query):
                if 'SELECT id,substr(path' in query and not committed:
                    self.service.submit('register_files_minimal', [dict(path='E:/fixture/third.png',
                        filename='third.png', size=1, created=1, hash_original=digest)]).result(3)
                    committed.append(True)
            repo.conn.set_trace_callback(traced)
            return native(repo, *args, **kwargs)
        with patch.object(LibraryRepository, 'get_duplicate_candidate_copies', intercept):
            result = self.service.read('get_duplicate_candidate_copies', digest, expected_count=2)
        self.assertEqual(len(result['copies']), 2)
        self.assertEqual(committed, [True])
        self.assertEqual(self.service.read('get_duplicate_candidates_page')['groups'][0]['copies'], 3)

    def test_sql_cancel_remains_effective_with_discovery_budget(self):
        self.service.submit('register_files_minimal', [dict(path=f'E:/fixture/{index}.png',
            filename=f'{index}.png', size=1, created=1, hash_original=A) for index in range(1500)]).result(5)
        event = threading.Event()
        native = LibraryRepository.get_duplicate_candidates_page
        def intercept(repo, **kwargs):
            repo.conn.set_trace_callback(lambda query: event.set() if 'GROUP BY 1' in query else None)
            return native(repo, **kwargs)
        with patch.object(LibraryRepository, 'get_duplicate_candidates_page', intercept):
            with self.assertRaisesRegex(LibraryError, 'cancelled'):
                self.service.read('get_duplicate_candidates_page', cancel_event=event)
        self.assertEqual(self.service.read('count_files'), 1500)


if __name__ == '__main__':
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(Queries))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromName('Service.'+name, sys.modules[__name__])
        for name in Service.__dict__ if name.startswith('test_'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
