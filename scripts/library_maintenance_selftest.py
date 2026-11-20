"""Private Windows/SQLite contracts for reviewed maintenance relocations."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.database_manager import DatabaseManager
from core.library_service import LibraryService, LibraryError


class Maintenance(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.work' / 'maintenance-tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = LibraryService(self.root / 'private.db')
        self.addCleanup(self.service.close)
        self.db = DatabaseManager(service=self.service)
        self.source = (self.root / 'artist' / 'Artist_00005.png').as_posix()
        self.target = (self.root / 'artist' / 'Artist_00001.png').as_posix()
        self.key = self.add(self.source)
        self.db.edit_curation(self.source, rating=5, add_tags=['curada'])
        self.db.update_file_metadata(self.source, dict(width=12, height=8, tool='Tool',
            model='Model', positive='prompt', negative='negative', seed='42'))

    def add(self, path, digest='a' * 64):
        self.db.register_files_minimal([dict(path=path, filename=Path(path).name,
            size=10, created=1, hash_original=digest, origin_url='receipt', artist='Artist')])
        return self.db.get_file_id(path)

    def sql(self, sql, parameters=()):
        with closing(sqlite3.connect(self.service.db_path, isolation_level=None)) as conn:
            return conn.execute(sql, parameters).fetchall()

    def state(self, key=None):
        return self.service.read('get_copy_version_state', self.key if key is None else key)

    def observation(self, key=None, source=None, target=None, **changes):
        key = self.key if key is None else key
        source, target = source or self.source, target or self.target
        value = dict(id=key, old_path=source, new_path=target,
            expected_fingerprint=self.state(key)['fingerprint'], size=10, digest='a' * 64,
            identity=dict(canonical=target, physical=[1, 2, key, 0, 10, 6, 7]))
        value.update(changes)
        return value

    def commit(self, *values):
        return self.service.submit('commit_maintenance_relocation', list(values)).result(10)

    def test_preserves_id_curation_provenance_and_fts(self):
        before = self.state()['state']
        self.assertTrue(self.commit(self.observation()))
        after = self.state()['state']
        self.assertEqual(after['tags'], before['tags'])
        for column in before['file']:
            if column not in ('path', 'filename'):
                self.assertEqual(before['file'][column], after['file'][column], column)
        self.assertEqual(after['file']['path'], self.target)
        self.assertEqual(self.db.get_file_id(self.target), self.key)
        self.assertIsNone(self.db.get_file_id(self.source))
        self.assertTrue(self.db.check_fts()['consistent'])

    def test_reuses_name_without_reviving_or_inheriting_offline_history(self):
        old = self.add(self.target, 'b' * 64)
        self.db.edit_curation(self.target, rating=2, add_tags=['historia'])
        self.service.submit('archive_copy', old,
            expected_fingerprint=self.state(old)['fingerprint']).result(10)
        historical = self.state(old)
        self.commit(self.observation())
        self.assertEqual(historical, self.state(old))
        self.assertEqual(self.db.get_file_id(self.target), self.key)
        self.assertEqual(self.db.get_tags_for_file(self.target), ['curada'])

    def test_cycles_are_atomic_and_keep_both_ids(self):
        other = self.add(self.target)
        values = [self.observation(), self.observation(key=other, source=self.target, target=self.source)]
        self.commit(*values)
        self.assertEqual(self.db.get_file_id(self.target), self.key)
        self.assertEqual(self.db.get_file_id(self.source), other)
        self.assertTrue(self.db.check_fts()['consistent'])

    def test_active_destination_and_case_alias_are_rejected(self):
        other = self.add(self.target.upper())
        before = self.state()
        with self.assertRaisesRegex(LibraryError, 'destination_occupied'):
            self.commit(self.observation())
        self.assertEqual(before, self.state())
        self.assertEqual(self.db.get_file_id(self.target.upper()), other)

    def test_stale_curation_review_rolls_back_entire_batch(self):
        other_path = (self.root / 'Artist_00006.png').as_posix()
        other = self.add(other_path)
        first = self.observation()
        second = self.observation(key=other, source=other_path,
            target=(self.root / 'Artist_00002.png').as_posix())
        self.db.edit_curation(other_path, rating=3)
        before = self.state()
        with self.assertRaisesRegex(LibraryError, 'source_changed'):
            self.commit(first, second)
        self.assertEqual(before, self.state())

    def test_hash_size_and_offline_source_are_rejected(self):
        for update in (dict(digest='b' * 64), dict(size=11)):
            with self.subTest(update=update), self.assertRaises(LibraryError):
                self.commit(self.observation(**update))
        before = self.observation()
        self.service.submit('archive_copy', self.key,
            expected_fingerprint=self.state()['fingerprint']).result(10)
        with self.assertRaisesRegex(LibraryError, 'source_changed'):
            self.commit(before)

    def test_unknown_hash_is_filled_without_copying_curation(self):
        self.sql('UPDATE files SET hash_original=NULL WHERE id=?', (self.key,))
        before = self.state()['state']
        self.commit(self.observation())
        after = self.state()['state']
        self.assertEqual(after['file']['hash_original'], 'a' * 64)
        self.assertEqual(before['tags'], after['tags'])

    def test_pending_source_transfer_prevents_relocation(self):
        self.service.submit('prepare_transfers').result(10)
        request = dict(operation_id=str(uuid4()), source_copy_id=self.key, action='move',
            source_path=self.source, result_path=(self.root / 'reserved.png').as_posix(),
            destination_paths=[(self.root / 'reserved.png').as_posix()], digest='a' * 64,
            caption_digest=None, source_fingerprint=self.state()['fingerprint'])
        self.service.submit('approve_transfer', request).result(10)
        with self.assertRaisesRegex(LibraryError, 'transfer_pending'):
            self.commit(self.observation())

    def test_pending_destination_transfer_prevents_relocation(self):
        path = (self.root / 'another.png').as_posix()
        other = self.add(path)
        self.service.submit('prepare_transfers').result(10)
        request = dict(operation_id=str(uuid4()), source_copy_id=other, action='move',
            source_path=path, result_path=self.target, destination_paths=[self.target],
            digest='a' * 64, caption_digest=None, source_fingerprint=self.state(other)['fingerprint'])
        self.service.submit('approve_transfer', request).result(10)
        with self.assertRaisesRegex(LibraryError, 'transfer_ingestion_pending'):
            self.commit(self.observation())

    def test_failure_during_final_write_restores_source_and_fts(self):
        self.sql("CREATE TRIGGER fixture_failure BEFORE UPDATE OF filename ON files "
                 "WHEN NEW.filename='Artist_00001.png' BEGIN SELECT RAISE(ABORT,'fixture_failure'); END")
        before = self.state()
        with self.assertRaisesRegex(LibraryError, 'fixture_failure'):
            self.commit(self.observation())
        self.assertEqual(before, self.state())
        self.assertTrue(self.db.check_fts()['consistent'])

    def test_foreign_trigger_cannot_silently_change_curation(self):
        self.sql("CREATE TRIGGER fixture_mutation AFTER UPDATE OF filename ON files "
                 "BEGIN UPDATE files SET rating=1 WHERE id=NEW.id; END")
        before = self.state()
        with self.assertRaisesRegex(LibraryError, 'result_changed'):
            self.commit(self.observation())
        self.assertEqual(before, self.state())

    def test_prepared_identity_is_updated_without_new_ids(self):
        self.service.submit('prepare_path_identity').result(10)
        value = self.observation()
        self.commit(value)
        records = self.sql('SELECT file_id,stored_path,is_offline,status,canonical,physical '
                           'FROM path_identity_records WHERE file_id=?', (self.key,))
        self.assertEqual(records[0][:5], (self.key, self.target, 0, 'resolved', self.target))

    def test_reads_include_every_active_and_historical_id_by_case_alias(self):
        other = self.add(self.target)
        self.service.submit('archive_copy', other,
            expected_fingerprint=self.state(other)['fingerprint']).result(10)
        self.commit(self.observation())
        before = self.sql('SELECT name,sql FROM sqlite_master ORDER BY name')
        rows = self.service.read('get_maintenance_copies', [self.target.upper()])['copies']
        self.assertEqual({row['state']['file']['id'] for row in rows}, {other, self.key})
        self.assertEqual(before, self.sql('SELECT name,sql FROM sqlite_master ORDER BY name'))

    def test_result_overflow_is_rejected_instead_of_truncated(self):
        for number in range(257):
            self.sql('INSERT INTO files(path,filename,is_offline) VALUES(?,?,1)',
                     (self.target, str(number)))
        with self.assertRaisesRegex((LibraryError, ValueError), 'copy_limit'):
            self.service.read('get_maintenance_copies', [self.target])

    def test_invalid_names_payloads_and_duplicate_targets_do_not_mutate(self):
        before = self.state()
        for update in (dict(new_path='relative.png'), dict(new_path='C:/folder/../file.png'),
                       dict(new_path='C:/file.png:stream'), dict(size=True),
                       dict(identity={'canonical':self.source, 'physical':[1]*7}),
                       dict(identity={'canonical':self.target, 'physical':[1]*6}),
                       dict(extra='unexpected')):
            value = self.observation(); value.update(update)
            with self.subTest(update=update), self.assertRaises(LibraryError):
                self.commit(value)
        with self.assertRaises(LibraryError):
            self.commit(self.observation(), self.observation())
        self.assertEqual(before, self.state())

    def test_repeated_old_review_never_overwrites_later_curation(self):
        value = self.observation()
        self.commit(value)
        self.db.edit_curation(self.target, rating=1)
        before = self.state()
        with self.assertRaisesRegex(LibraryError, 'source_changed'):
            self.commit(value)
        self.assertEqual(before, self.state())


if __name__ == '__main__':
    unittest.main(verbosity=2)
