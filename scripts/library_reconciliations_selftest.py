"""Private Native fixtures for atomic, idempotent content reconciliation."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.library_service import LibraryService, LibraryError
from core.library_repository import LibraryRepository
from core.library_reconciliations import NAME, request

OLD = hashlib.sha256(b'old recorded content').hexdigest()
NEW = hashlib.sha256(b'current verified content').hexdigest()


class Reconciliations(unittest.TestCase):
    def setUp(self):
        work = ROOT / '.work' / 'copy-reconciliation-tests'; work.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=work); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.db = self.root / 'private.db'
        self.service = LibraryService(self.db); self.addCleanup(self.service.close)
        self.paths = [(self.root / f'image_{i}.png').as_posix() for i in range(2)]
        self.service.submit('register_files_minimal', [dict(path=p, filename=Path(p).name,
            size=20, created=1, hash_original=OLD, origin_url='old receipt', artist='Artist')
            for p in self.paths]).result(5)
        self.ids = [self.service.read('get_file_id', p) for p in self.paths]
        for key, path in zip(self.ids, self.paths):
            self.service.submit('edit_curation', path, add_tags=['curated'], rating=5).result(5)
        self.before = [self.state(key) for key in self.ids]
        self.service.submit('prepare_copy_reconciliations').result(5)

    def state(self, key):
        return self.service.read('get_copy_version_state', key)

    def requests(self):
        return [dict(copy_id=key, expected_fingerprint=before['fingerprint'], digest=NEW,
                     observation=dict(filename=Path(path).name, size=24, created=2), identity=None)
                for key, path, before in zip(self.ids, self.paths, self.before)]

    def apply(self, operation_id=None, requests=None):
        operation_id = operation_id or str(uuid4())
        self.service.submit('reconcile_copy_versions_batch', operation_id,
                            self.requests() if requests is None else requests).result(5)
        return operation_id, self.receipt(operation_id)

    def receipt(self, operation_id):
        return self.service.read('get_copy_reconciliation_receipt', operation_id)['receipt']

    def count(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT count(*) FROM files').fetchone()[0]

    def test_batch_preserves_complete_history_and_creates_clean_ids(self):
        operation_id, receipt = self.apply()
        self.assertEqual(receipt['request_fingerprint'], request(operation_id, self.requests())[1])
        self.assertEqual(receipt['phase'], 'complete'); self.assertEqual(len(receipt['records']), 2)
        for before, record in zip(self.before, receipt['records']):
            old = self.state(record['old_id'])
            expected = dict(before['state']['file'], is_offline=1)
            self.assertEqual(old['state'], dict(file=expected, tags=before['state']['tags']))
            new = self.state(record['new_id'])
            self.assertEqual(new['fingerprint'], record['current_fingerprint'])
            self.assertEqual(old['fingerprint'], record['historical_fingerprint'])
            self.assertEqual(new['state']['file']['hash_original'], NEW)
            self.assertEqual(new['state']['file']['rating'], 0); self.assertEqual(new['state']['tags'], [])
            for field in ('origin_url', 'artist', 'hash_mod', 'phash', 'meta_positive'):
                self.assertIsNone(new['state']['file'][field])
        self.assertEqual(self.count(), 4); self.assertTrue(self.service.read('check_fts')['consistent'])

    def test_stale_second_source_rolls_back_whole_batch(self):
        values = self.requests(); values[1]['expected_fingerprint'] = '0' * 64
        operation_id = str(uuid4())
        with self.assertRaisesRegex(LibraryError, 'source_changed'):
            self.apply(operation_id, values)
        self.assertIsNone(self.receipt(operation_id)); self.assertEqual(self.count(), 2)
        self.assertEqual([self.state(key) for key in self.ids], self.before)

    def test_fts_failure_rolls_back_ids_rows_and_receipt(self):
        sync = LibraryRepository._fts_sync_file; calls = []
        def fail_second(repo, *args):
            calls.append(args)
            if len(calls) == 2: raise RuntimeError('injected second FTS failure')
            return sync(repo, *args)
        operation_id = str(uuid4())
        with patch.object(LibraryRepository, '_fts_sync_file', fail_second):
            with self.assertRaisesRegex(LibraryError, 'injected second FTS'):
                self.apply(operation_id)
        self.assertIsNone(self.receipt(operation_id)); self.assertEqual(self.count(), 2)
        self.assertEqual([self.state(key) for key in self.ids], self.before)
        self.assertTrue(self.service.read('check_fts')['consistent'])

    def test_historical_ack_does_not_replay_curation_after_ambiguous_transport(self):
        operation_id, receipt = self.apply()
        key = receipt['records'][0]['new_id']; before = self.state(key)
        self.service.submit('edit_curation', self.paths[0], add_tags=['later'], rating=2,
            expected_copy_id=key, expected_fingerprint=before['fingerprint']).result(5)
        changed = self.state(key)
        self.apply(operation_id)
        self.assertEqual(self.state(key), changed); self.assertEqual(self.count(), 4)
        self.assertEqual(self.receipt(operation_id), receipt)

    def test_uuid_payload_conflict_never_repeats_transition(self):
        operation_id, before = self.apply()
        values = self.requests(); values[0]['digest'] = 'b' * 64
        with self.assertRaisesRegex(LibraryError, 'operation_conflict'):
            self.apply(operation_id, values)
        self.assertEqual(self.receipt(operation_id), before); self.assertEqual(self.count(), 4)

    def test_invalid_duplicate_oversized_and_unchanged_requests_are_rejected(self):
        for values, reason in [([self.requests()[0]] * 2, 'duplicate'),
                               ([self.requests()[0]] * 101, 'invalid_copy_reconciliation_batch')]:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(LibraryError, reason): self.apply(requests=values)
        values = self.requests(); values[0]['digest'] = OLD
        with self.assertRaisesRegex(LibraryError, 'source_changed'): self.apply(requests=values)
        values = self.requests(); values[0]['observation']['created'] = float('nan')
        with self.assertRaisesRegex(LibraryError, 'invalid_copy_reconciliation_observation'):
            self.apply(requests=values)
        self.assertEqual(self.count(), 2)

    def test_offline_source_and_prepared_registry_require_review(self):
        self.service.submit('archive_copy', self.ids[0], expected_fingerprint=self.before[0]['fingerprint']).result(5)
        values = self.requests(); values[0]['expected_fingerprint'] = self.state(self.ids[0])['fingerprint']
        with self.assertRaisesRegex(LibraryError, 'source_changed'): self.apply(requests=values)
        self.service.submit('prepare_path_identity').result(5)
        with self.assertRaisesRegex(LibraryError, 'prepared_identity_requires_review'): self.apply()
        self.assertEqual(self.count(), 2)

    def test_pending_transfer_blocks_transition_and_retains_reservation(self):
        self.service.submit('prepare_transfers').result(5)
        transfer = dict(operation_id=str(uuid4()), source_copy_id=self.ids[0], action='move',
            source_path=self.paths[0], result_path=(self.root/'out.png').as_posix(), digest=OLD,
            caption_digest=None, source_fingerprint=self.before[0]['fingerprint'],
            destination_paths=[(self.root/'out.png').as_posix()])
        self.service.submit('approve_transfer', transfer).result(5)
        with self.assertRaisesRegex(LibraryError, 'pending|reserved'):
            self.apply()
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.service.read('get_transfer_receipt', transfer['operation_id'])['receipt']['phase'], 'approved')

    def test_new_case_alias_invalidates_owner_cache_and_blocks_ambiguous_source(self):
        operation_id, _ = self.apply(requests=self.requests()[:1])
        alias = self.paths[1].upper()
        self.service.submit('register_files_minimal', [dict(path=alias, filename=Path(alias).name,
            size=20, created=1, hash_original=OLD)]).result(5)
        before = self.state(self.ids[1]); count = self.count()
        # Historical acknowledgement must not seal a cache invalidated by
        # this intervening unrelated write.
        self.apply(operation_id, self.requests()[:1])
        with self.assertRaisesRegex(LibraryError, 'source_ambiguous'):
            self.apply(requests=self.requests()[1:])
        self.assertEqual(self.state(self.ids[1]), before); self.assertEqual(self.count(), count)

    def test_own_commits_reuse_alias_scan_without_repeated_global_walks(self):
        from core import library_reconciliations as module
        original = module._active_aliases; sql = []
        def traced(repo):
            repo.conn.set_trace_callback(sql.append)
            return original(repo)
        with patch.object(module, '_active_aliases', traced):
            self.apply(requests=self.requests()[:1])
            self.apply(requests=self.requests()[1:])
        scans = [value for value in sql if value.startswith('SELECT reconciliation_path_key_v1(path) FROM files')]
        self.assertEqual(len(scans), 1)

    def test_external_sql_commit_invalidates_alias_snapshot(self):
        self.apply(requests=self.requests()[:1])
        alias = self.paths[1].upper()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('INSERT INTO files(path,filename,file_size,hash_original) VALUES(?,?,?,?)',
                         (alias, Path(alias).name, 20, OLD))
            conn.commit()
        before = self.state(self.ids[1]); operation_id = str(uuid4())
        with self.assertRaisesRegex(LibraryError, 'source_ambiguous'):
            self.apply(operation_id, self.requests()[1:])
        self.assertEqual(self.state(self.ids[1]), before); self.assertIsNone(self.receipt(operation_id))

    def test_unknown_trigger_is_rejected_before_any_transition(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TRIGGER unsafe_rewrite AFTER INSERT ON files BEGIN '
                         'UPDATE files SET rating=1 WHERE id<>new.id; END')
        with self.assertRaisesRegex(LibraryError, 'triggers_require_review'):
            self.apply()
        self.assertEqual([self.state(key) for key in self.ids], self.before)
        self.assertEqual(self.count(), 2)

    def test_explicit_verified_provenance_is_independent_of_historical_copy(self):
        values = self.requests()
        values[0]['provenance'] = dict(origin_url='patreon://media/fresh', origin_site='patreon', artist='Current Artist')
        operation_id, receipt = self.apply(requests=values)
        first, second = receipt['records']
        current = self.state(first['new_id'])['state']['file']
        for key, value in values[0]['provenance'].items(): self.assertEqual(current[key], value)
        old = self.state(first['old_id'])['state']['file']
        self.assertEqual(old['origin_url'], 'old receipt'); self.assertEqual(old['artist'], 'Artist')
        self.assertIsNone(self.state(second['new_id'])['state']['file']['artist'])
        values[0]['provenance']['artist'] = 'Unrelated'
        with self.assertRaisesRegex(LibraryError, 'operation_conflict'):
            self.apply(operation_id, values)
        self.assertTrue(self.service.read('check_fts')['consistent'])

    def test_batch_limit_accepts_100_and_receipt_survives_reopen(self):
        paths = [(self.root/f'item_{i}.png').as_posix() for i in range(100)]
        self.service.submit('register_files_minimal', [dict(path=p, filename=Path(p).name,
            size=20, created=1, hash_original=OLD) for p in paths]).result(5)
        values = []
        for path in paths:
            key = self.service.read('get_file_id', path)
            values.append(dict(copy_id=key, expected_fingerprint=self.state(key)['fingerprint'], digest=NEW,
                observation=dict(filename=Path(path).name, size=24, created=2), identity=None))
        operation_id, receipt = self.apply(requests=values)
        self.assertEqual(len(receipt['records']), 100)
        self.service.close()
        self.service = LibraryService(self.db, require_ready=True); self.addCleanup(self.service.close)
        self.assertEqual(self.receipt(operation_id), receipt)
        self.apply(operation_id, values); self.assertEqual(self.count(), 202)


if __name__ == '__main__': unittest.main(verbosity=2)
