"""CF8 con DB privadas: identidad, historial, reintentos y rollback; sin biblioteca real."""
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.database_manager import DatabaseManager
from core.generation_history import NAME, INDEX, MAX_RECORD, prepare, commit
from core.library_repository import LibraryRepository
from core.library_service import LibraryService, LibraryError


class History(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.service = LibraryService(self.root / 'private.db')
        self.addCleanup(self.service.close)
        self.db = DatabaseManager(service=self.service)
        self.path = str(self.root / 'imagen ñ.png')
        self.assertTrue(self.db.register_files_minimal([dict(path=self.path, filename='imagen ñ.png',
            size=10, created=1, hash_original='a'*64, origin_url='receipt', artist='Artist')]))
        self.key = self.db.get_file_id(self.path)
        self.db.edit_curation(self.path, add_tags=['curada'], rating=5)
        self.db.update_file_metadata(self.path, dict(width=16, height=32, positive='previous prompt',
            negative='previous negative', tool='Old', model='M', seed='42'))

    def sql(self, query, params=()):
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            rows = conn.execute(query, params).fetchall()
            conn.commit()
            return rows

    def state(self):
        return self.service.read('get_copy_version_state', self.key)

    def request(self, **changes):
        args = dict(expected_fingerprint=self.state()['fingerprint'], operation_id=str(uuid4()),
            source_digest='a'*64, result_digest='b'*64,
            generation=dict(width=16, height=32, tool='New', model='', positive='current', negative='', seed=''),
            observation=dict(filename='imagen ñ.png', size=20, created=2),
            source_generation=dict(positive='physical old', negative='bad', model='physical model', seed='99', sampler='Euler'))
        args.update(changes)
        return args

    def prepare(self):
        self.assertIs(self.service.submit('prepare_generation_history').result(), True)

    def apply(self, args):
        return self.service.submit('commit_metadata_edit', self.key, **args).result()

    def history(self, **kwargs):
        return self.service.read('get_generation_history_page', self.key, **kwargs)

    def test_reads_do_not_create_schema(self):
        before = self.sql('SELECT name,sql FROM sqlite_master ORDER BY name')
        self.assertFalse(self.history()['prepared'])
        self.assertFalse(self.service.read('get_generation_history_status')['prepared'])
        self.assertEqual(self.service.read('get_metadata_edit_state', self.path)['fingerprint'], self.state()['fingerprint'])
        self.assertEqual(before, self.sql('SELECT name,sql FROM sqlite_master ORDER BY name'))

    def test_explicit_prepare_idempotent(self):
        self.prepare()
        self.prepare()
        self.assertTrue(self.service.read('get_generation_history_status')['prepared'])
        self.assertEqual(len(self.sql('SELECT name FROM sqlite_master WHERE name IN (?,?)', (NAME, INDEX))), 2)

    def test_reject_unprepared_without_changes(self):
        before = self.state()
        with self.assertRaisesRegex(LibraryError, 'not_prepared'):
            self.apply(self.request())
        self.assertEqual(before, self.state())

    def test_conflicting_schema_is_preserved(self):
        self.sql(f'CREATE TABLE {NAME}(unrelated TEXT)')
        with self.assertRaisesRegex(LibraryError, 'name_conflict'):
            self.service.submit('prepare_generation_history').result()
        self.assertEqual(self.sql(f'PRAGMA table_info({NAME})')[0][1], 'unrelated')

    def test_commit_preserves_identity_curation_origin_and_original_hash(self):
        self.prepare()
        before = self.state()['state']
        args = self.request()
        self.assertIs(self.apply(args), True)
        after = self.state()['state']
        for key in ('id', 'path', 'hash_original', 'rating', 'origin_url', 'origin_site', 'artist', 'is_offline', 'phash'):
            self.assertEqual(before['file'][key], after['file'][key])
        self.assertEqual(before['tags'], after['tags'])
        self.assertEqual(after['file']['meta_positive'], 'current')
        self.assertEqual(after['file']['hash_mod'], 'b'*64)
        record = self.history()['records'][0]
        self.assertEqual(record['previous']['native']['meta_positive'], 'previous prompt')
        self.assertEqual(record['previous']['observed'], args['source_generation'])
        self.assertTrue(self.db.check_fts()['consistent'])
        self.assertEqual(self.db.search_by_terms(['previous prompt'])[0], 0)
        self.assertEqual(self.db.search_by_terms(['current'])[0], 1)

    def test_previous_phash_is_not_valid_for_changed_bytes(self):
        algorithm = 'phash8-v1/test'
        self.assertIs(self.service.submit('prepare_phash_registry').result(), True)
        self.assertIs(self.service.submit('store_phash_batch', [dict(copy_id=self.key,
            path=self.path, source_sha256='a'*64, phash='0123456789abcdef')], algorithm).result(), True)
        self.assertEqual(self.service.read('get_phash_states', [self.path], algorithm)[0]['phash'],
                         '0123456789abcdef')
        self.prepare()
        self.assertIs(self.apply(self.request()), True)
        result = self.service.read('get_phash_states', [self.path], algorithm)[0]
        self.assertEqual(result['source_sha256'], 'b'*64)
        self.assertIsNone(result['phash'])

    def test_clear_keeps_previous_generation(self):
        self.prepare()
        generation = dict(width=16, height=32, tool='', model='', positive='', negative='', seed='')
        self.apply(self.request(generation=generation))
        self.assertEqual(self.state()['state']['file']['meta_positive'], '')
        self.assertEqual(self.history()['records'][0]['previous']['native']['meta_seed'], '42')

    def test_stale_curation_rejects_whole_commit(self):
        self.prepare()
        args = self.request()
        self.db.edit_curation(self.path, add_tags=['later'])
        before = self.state()
        with self.assertRaisesRegex(LibraryError, 'changed_since_review'):
            self.apply(args)
        self.assertEqual(before, self.state())
        self.assertEqual(self.history()['records'], [])

    def test_offline_is_rejected_but_history_remains_readable(self):
        self.prepare()
        self.apply(self.request())
        self.service.submit('archive_copy', self.key, expected_fingerprint=self.state()['fingerprint']).result()
        with self.assertRaisesRegex(LibraryError, 'offline'):
            self.apply(self.request(source_digest='b'*64))
        self.assertEqual(len(self.history()['records']), 1)
        with self.assertRaisesRegex(ValueError, 'not_indexed'):
            self.service.read('get_metadata_edit_state', self.path)

    def test_wrong_source_digest_rejected(self):
        self.prepare()
        with self.assertRaisesRegex(LibraryError, 'source_hash_changed'):
            self.apply(self.request(source_digest='c'*64))
        self.assertEqual(self.history()['records'], [])

    def test_unknown_original_hash_filled_once(self):
        self.sql('UPDATE files SET hash_original=NULL WHERE id=?', (self.key,))
        self.prepare()
        self.apply(self.request())
        self.assertEqual(self.state()['state']['file']['hash_original'], 'a'*64)
        self.apply(self.request(source_digest='b'*64, result_digest='c'*64))
        self.assertEqual(self.state()['state']['file']['hash_original'], 'a'*64)

    def test_retry_receipt_does_not_reapply_to_later_state(self):
        self.prepare()
        first = self.request()
        self.apply(first)
        self.apply(self.request(source_digest='b'*64, result_digest='c'*64))
        before = self.state()
        self.assertIs(self.apply(first), True)
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.history()['records']), 2)
        with self.assertRaisesRegex(LibraryError, 'operation_conflict'):
            self.apply(dict(first, result_digest='d'*64))

    def test_approval_no_dml_and_duplicate_approval_rejected(self):
        self.prepare()
        args = self.request()
        before = self.state()
        self.assertIs(self.service.submit('approve_metadata_edit', self.key, **args).result(), True)
        self.assertEqual(before, self.state())
        self.assertEqual(self.history()['records'], [])
        self.apply(args)
        with self.assertRaisesRegex(LibraryError, 'already_committed'):
            self.service.submit('approve_metadata_edit', self.key, **args).result()

    def test_fts_failure_rolls_back_history_and_current(self):
        self.prepare()
        before = self.state()
        with patch.object(LibraryRepository, '_fts_sync_file', side_effect=RuntimeError('fixture fts failure')):
            with self.assertRaisesRegex(LibraryError, 'fixture fts failure'):
                self.apply(self.request())
        self.assertEqual(before, self.state())
        self.assertEqual(self.history()['records'], [])

    def test_invalid_and_large_payload_rejected(self):
        self.prepare()
        for changes in (dict(operation_id='bad'), dict(observation=dict(filename='other.png', size=1, created=1)),
                dict(generation=dict(self.request()['generation'], positive='x'*65537)),
                dict(source_generation={'raw': 'unknown'}), dict(observation=dict(filename='imagen ñ.png', size=True, created=1))):
            with self.assertRaises(LibraryError):
                self.apply(self.request(**changes))
        self.assertEqual(self.history()['records'], [])

    def test_history_page_bound_before_materializing(self):
        self.prepare()
        args = self.request()
        self.apply(args)
        self.sql(f'UPDATE {NAME} SET previous_json=?', ('x'*(MAX_RECORD+1),))
        with self.assertRaisesRegex(ValueError, 'page_payload_limit'):
            self.history()

    def test_keyset_pages(self):
        self.prepare()
        for source, result in (('a', 'b'), ('b', 'c'), ('c', 'd')):
            self.apply(self.request(source_digest=source*64, result_digest=result*64))
        first = self.history(limit=2)
        self.assertTrue(first['has_more'])
        last = self.history(after_id=first['next_after_id'], limit=2)
        self.assertFalse(last['has_more'])
        self.assertEqual(len(last['records']), 1)

    def test_owner_methods_never_touch_files(self):
        self.prepare()
        with patch('os.stat', side_effect=AssertionError('no FS')), patch('builtins.open', side_effect=AssertionError('no FS')):
            self.assertIs(self.apply(self.request()), True)

    def test_previous_native_generation_limit_checked_before_approval(self):
        self.prepare()
        fields = ('meta_positive', 'meta_negative', 'meta_model', 'meta_tool', 'meta_seed')
        self.sql('UPDATE files SET '+','.join(key+'=?' for key in fields)+' WHERE id=?', (*['x'*65536 for _ in fields], self.key))
        with self.assertRaisesRegex(LibraryError, 'metadata_payload_limit'):
            self.service.submit('approve_metadata_edit', self.key, **self.request()).result()
        self.assertEqual(self.history()['records'], [])

    def test_path_status_includes_offline_unicode_case_without_ddl(self):
        self.service.submit('archive_copy', self.key, expected_fingerprint=self.state()['fingerprint']).result()
        self.assertTrue(self.service.read('get_metadata_path_status', [self.path.upper(), str(self.root/'new.png')])['occupied'])
        self.assertFalse(self.service.read('get_generation_history_status')['prepared'])

    def test_prepare_budget_rolls_back_even_for_tiny_schema(self):
        with patch('core.generation_history.time.monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(LibraryError, 'prepare_budget'):
                self.service.submit('prepare_generation_history').result()
        self.assertFalse(self.service.read('get_generation_history_status')['prepared'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
