"""Alta/adopción explícitas por copia; transacciones reales en SQLite temporal."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.curation_restore import adopt_identity, register_copy
from core.library_repository import LibraryRepository
import curation_snapshot_selftest as helpers
import curation_restore_selftest as previous


class Registration(helpers.Fixture):
    read = previous.Restore.read
    incoming = previous.Restore.incoming
    sql = previous.Restore.sql

    def setUp(self):
        super().setUp()
        self.paths = [(Path(self.temp.name) / name).as_posix() for name in ('one.png', 'two.png')]
        self.service.submit('register_files_minimal', [dict(path=path, filename=Path(path).name,
            size=1, created=1, hash_original='a'*64) for path in self.paths]).result(3)
        self.new = (Path(self.temp.name) / 'moved' / 'new.png').as_posix()
        self.observation = dict(filename='new.png', size=17, created=1234.5)
        self.value = self.incoming(hash_mod='b'*64, rating=4,
            tags=[dict(name='restored', source='manual', category='character')])

    def register_new(self, **changes):
        args = dict(incoming=self.value, observation=self.observation)
        args.update(changes)
        return self.service.submit('register_curation_copy', self.new, **args).result(3)

    def clear_identity(self):
        self.sql('UPDATE files SET hash_original=NULL,hash_mod=NULL WHERE path=?', (self.paths[0],))
        return self.read()

    def adopt(self, snapshot=None, value=None):
        return self.service.submit('adopt_curation_identity', self.paths[0],
            expected_fingerprint=(snapshot or self.read())['fingerprint'],
            incoming=value or self.value).result(3)

    def database(self):
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            return tuple(tuple(conn.execute(f'SELECT * FROM {table} ORDER BY 1,2'))
                for table in ('files', 'tags', 'file_tags', 'files_fts', 'sqlite_sequence'))

    def test_new_copy_keeps_other_identical_hash_copy_and_observations(self):
        old = [self.read(path) for path in self.paths]
        self.assertTrue(self.register_new())
        state = self.read(self.new)['state']
        self.assertEqual({key: state[key] for key in self.value}, self.value)
        self.assertEqual([self.read(path) for path in self.paths], old)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT filename,file_size,created_date FROM files WHERE path=?',
                (self.new,)).fetchone(), ('new.png', 17, 1234.5))

    def test_new_empty_curation_has_fts_and_tagged_copy_syncs_once(self):
        original = LibraryRepository._fts_sync_file
        calls = []
        def sync(repo, file_id, *args):
            calls.append(file_id)
            return original(repo, file_id, *args)
        with patch.object(LibraryRepository, '_fts_sync_file', sync):
            self.assertTrue(self.register_new(incoming=self.incoming(rating=0, tags=[])))
            self.assertEqual(len(calls), 1)
            self.new = (Path(self.temp.name) / 'elsewhere' / 'new.png').as_posix()
            self.assertTrue(self.register_new())
            self.assertEqual(len(calls), 2)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM files_fts').fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT count(*) FROM files_fts WHERE files_fts MATCH 'restored'").fetchone()[0], 1)

    def test_existing_route_is_never_overwritten(self):
        self.new = self.paths[0]
        self.observation['filename'] = 'one.png'
        old = self.database()
        with self.assertRaisesRegex(Exception, 'file_now_registered'):
            self.register_new()
        self.assertEqual(self.database(), old)

    def test_two_queued_registrations_only_first_commits(self):
        futures = [self.service.submit('register_curation_copy', self.new,
            incoming=self.value, observation=self.observation) for _ in range(2)]
        self.assertTrue(futures[0].result(3))
        with self.assertRaisesRegex(Exception, 'file_now_registered'):
            futures[1].result(3)
        self.assertEqual(self.service.read('count_files'), 3)

    def test_registration_shared_category_conflict_rolls_back_everything(self):
        self.service.submit('edit_curation', self.paths[1], add_tags=['restored']).result(3)
        self.sql("UPDATE tags SET category='different' WHERE name='restored'")
        old = self.database()
        with self.assertRaisesRegex(Exception, 'tag_category_conflict'):
            self.register_new()
        self.assertEqual(self.database(), old)

    def test_registration_fts_failure_rolls_back_file_tags_rating_and_sequences(self):
        old = self.database()
        with patch.object(LibraryRepository, '_fts_sync_file', side_effect=RuntimeError('forced_fts')):
            with self.assertRaisesRegex(Exception, 'forced_fts'):
                self.register_new()
        self.assertEqual(self.database(), old)

    def test_bad_observations_fail_before_dml(self):
        old = self.database()
        bad = [None, {}, dict(self.observation, extra=1), dict(self.observation, filename='wrong.png'),
            dict(self.observation, filename='x'*1025), dict(self.observation, size=True),
            dict(self.observation, size=-1), dict(self.observation, size=2**63),
            dict(self.observation, created=True), dict(self.observation, created=-1),
            dict(self.observation, created=253402300800), dict(self.observation, created=10**1000),
            dict(self.observation, created=float('nan')), dict(self.observation, created=float('inf'))]
        for observation in bad:
            with self.subTest(observation=str(observation)[:80]):
                with self.assertRaisesRegex(Exception, 'invalid_curation_observation'):
                    self.register_new(observation=observation)
                self.assertEqual(self.database(), old)

    def test_registration_missing_identity_and_invalid_payload_are_rejected(self):
        old = self.database()
        for value, reason in ((dict(self.value, hash_original=None, hash_mod=None), 'identity_missing'),
                              (dict(self.value, rating=True), 'invalid_restore_rating'),
                              (dict(self.value, tags=[dict(name='UPPER', source=None, category=None)]), 'invalid_restore_tag')):
            with self.assertRaisesRegex(Exception, reason):
                self.register_new(incoming=value)
            self.assertEqual(self.database(), old)

    def test_adoption_preserves_id_generation_and_observations(self):
        self.sql("UPDATE files SET meta_model='gen-model',meta_positive='private prompt',file_size=42 WHERE path=?", (self.paths[0],))
        snapshot = self.clear_identity()
        other = self.read(self.paths[1])
        self.assertTrue(self.adopt(snapshot))
        state = self.read()['state']
        self.assertEqual(state['id'], snapshot['state']['id'])
        self.assertEqual({key: state[key] for key in self.value}, self.value)
        self.assertEqual(self.read(self.paths[1]), other)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT meta_model,meta_positive,file_size,created_date FROM files WHERE path=?',
                (self.paths[0],)).fetchone(), ('gen-model', 'private prompt', 42, 1))

    def test_adoption_never_replaces_either_known_hash(self):
        for original, modified in (('a'*64, None), (None, 'b'*64), ('a'*64, 'b'*64)):
            self.sql('UPDATE files SET hash_original=?,hash_mod=? WHERE path=?', (original, modified, self.paths[0]))
            old = self.database()
            with self.assertRaisesRegex(Exception, 'identity_already_known'):
                self.adopt()
            self.assertEqual(self.database(), old)

    def test_changes_since_review_reject_adoption_without_refreshing_approval(self):
        self.service.submit('edit_curation', self.paths[0], add_tags=['existing']).result(3)
        self.clear_identity()
        for query in ("UPDATE files SET rating=2 WHERE filename='one.png'",
                      "UPDATE file_tags SET source='changed'", "UPDATE tags SET category='changed'",
                      "UPDATE files SET hash_mod='" + 'c'*64 + "' WHERE filename='one.png'"):
            snapshot = self.read()
            self.sql(query)
            old = self.database()
            with self.assertRaisesRegex(Exception, 'curation_changed_since_review'):
                self.adopt(snapshot)
            self.assertEqual(self.database(), old)

    def test_adoption_category_conflict_rolls_back_identity(self):
        self.service.submit('edit_curation', self.paths[1], add_tags=['restored']).result(3)
        self.sql("UPDATE tags SET category='different' WHERE name='restored'")
        snapshot = self.clear_identity()
        old = self.database()
        with self.assertRaisesRegex(Exception, 'tag_category_conflict'):
            self.adopt(snapshot)
        self.assertEqual(self.database(), old)

    def test_adoption_fts_failure_rolls_back_identity_and_curation(self):
        snapshot = self.clear_identity()
        old = self.database()
        with patch.object(LibraryRepository, '_fts_sync_file', side_effect=RuntimeError('forced_fts')):
            with self.assertRaisesRegex(Exception, 'forced_fts'):
                self.adopt(snapshot)
        self.assertEqual(self.database(), old)

    def test_two_queued_adoptions_reject_second_old_approval(self):
        snapshot = self.clear_identity()
        futures = [self.service.submit('adopt_curation_identity', self.paths[0],
            expected_fingerprint=snapshot['fingerprint'], incoming=self.value) for _ in range(2)]
        self.assertTrue(futures[0].result(3))
        with self.assertRaisesRegex(Exception, 'curation_changed_since_review'):
            futures[1].result(3)

    def test_empty_curation_and_nullable_tags_remain_explicit(self):
        snapshot = self.clear_identity()
        value = dict(self.value, tags=[dict(name='restored', source=None, category=None)], rating=0)
        self.assertTrue(self.adopt(snapshot, value))
        self.assertEqual(self.read()['state']['tags'], value['tags'])
        self.assertTrue(self.service.submit('restore_curation', self.paths[0],
            expected_fingerprint=self.read()['fingerprint'], incoming=dict(value, tags=[])).result(3))
        self.assertEqual((self.read()['state']['tags'], self.read()['state']['rating']), ([], 0))

    def test_adoption_invalid_approval_missing_identity_and_absent_row(self):
        snapshot = self.clear_identity()
        old = self.database()
        with self.assertRaisesRegex(Exception, 'invalid_restore_fingerprint'):
            self.service.submit('adopt_curation_identity', self.paths[0], expected_fingerprint='bad', incoming=self.value).result(3)
        with self.assertRaisesRegex(Exception, 'identity_missing'):
            self.adopt(snapshot, dict(self.value, hash_original=None, hash_mod=None))
        with self.assertRaisesRegex(Exception, 'file_not_registered'):
            self.service.submit('adopt_curation_identity', self.new,
                expected_fingerprint=snapshot['fingerprint'], incoming=self.value).result(3)
        self.assertEqual(self.database(), old)

    def test_no_filesystem_io_or_old_path_removal_in_core(self):
        old = self.read()
        with patch.object(Path, 'stat', side_effect=AssertionError('filesystem')):
            self.assertTrue(self.register_new())
        self.assertEqual(self.read(), old)
        self.assertFalse(Path(self.new).exists())

    def test_direct_calls_require_owner_transaction(self):
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            with self.assertRaisesRegex(RuntimeError, 'restore_requires_transaction'):
                register_copy(conn, self.new, incoming=self.value, observation=self.observation, update_fts=lambda _: None)
            with self.assertRaisesRegex(RuntimeError, 'restore_requires_transaction'):
                adopt_identity(conn, self.paths[0], expected_fingerprint=self.read()['fingerprint'],
                    incoming=self.value, update_fts=lambda _: None)


if __name__ == '__main__':
    unittest.main(verbosity=2)
