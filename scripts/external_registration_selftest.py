"""Verified registration in private Windows fixtures; no collection/network."""
from contextlib import closing
from pathlib import Path
import hashlib
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from core.external_registration import register
from core.library_service import LibraryService
from core.library_repository import LibraryRepository
from core.metadata.bundle import MetadataBundle
from core.metadata.stamper import MetadataStamper


class Registration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.service = LibraryService(self.root / 'private.db')
        self.addCleanup(self.service.close)
        self.source = self.root / 'external.png'
        with Image.new('RGB', (24, 20), 'red') as image:
            image.save(self.source)
        bundle = MetadataBundle(positive_prompt='original prompt', model='fixture-model',
                                seed='19', tags=['embedded'], rating=4)
        self.assertTrue(MetadataStamper.stamp(self.source, bundle, prepared=True))
        self.event = threading.Event()

    def apply(self):
        return register(self.service, self.source, self.event)

    def sql(self, sql, params=()):
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            return conn.execute(sql, params).fetchall()

    def seed(self, path=None):
        path = path or self.source
        self.assertTrue(self.service.submit('register_files_minimal', [dict(path=str(path),
            filename=path.name, size=1, created=1, hash_original='a'*64)]).result(3))
        return self.service.read('get_file_id', str(path))

    def test_hash_generation_curation_fts_and_original_are_preserved(self):
        before = self.source.read_bytes()
        key = self.apply()
        state = self.service.read('get_copy_version_state', key)['state']
        self.assertEqual(state['file']['hash_original'], hashlib.sha256(before).hexdigest())
        self.assertIsNone(state['file']['hash_mod'])
        self.assertEqual(state['file']['meta_positive'], 'original prompt')
        self.assertEqual(state['file']['meta_model'], 'fixture-model')
        self.assertEqual(state['file']['rating'], 4)
        self.assertEqual([tag['name'] for tag in state['tags']], ['embedded'])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertTrue(self.service.read('check_fts')['consistent'])
        self.assertEqual(self.sql("SELECT count(*) FROM files_fts WHERE files_fts MATCH 'original'"), [(1,)])

    def test_registration_obeys_prepared_folder_identity_and_resolves_new_id(self):
        self.service.backfill_path_identity()
        key = self.apply()
        self.assertEqual(self.service.resolve_path_identity(str(self.source))['file_id'], key)
        self.assertTrue(self.service.read('get_path_identity_status')['prepared'])
        self.assertTrue(self.service.read('check_fts')['consistent'])

    def test_existing_curated_identity_is_rejected_unchanged(self):
        key = self.seed()
        self.service.submit('edit_curation', str(self.source), add_tags=['keep'], rating=5).result(3)
        before = self.service.read('get_copy_version_state', key)
        with self.assertRaisesRegex(Exception, 'has_native_history'):
            self.apply()
        self.assertEqual(self.service.read('get_copy_version_state', key), before)

    def test_prepared_case_alias_cannot_register_second_id(self):
        key = self.seed()
        self.service.backfill_path_identity()
        self.source = self.root / 'EXTERNAL.png'
        before = self.service.read('get_copy_version_state', key)
        with self.assertRaisesRegex(Exception, 'has_native_history'):
            self.apply()
        self.assertEqual(self.service.read('count_files'), 1)
        self.assertEqual(self.service.read('get_copy_version_state', key), before)

    def test_distinct_hardlink_is_independent_copy_in_prepared_library(self):
        key = self.seed()
        self.service.backfill_path_identity()
        other = self.root / 'other.png'
        os.link(self.source, other)
        self.source = other
        new = self.apply()
        self.assertNotEqual(new, key)
        self.assertEqual(self.service.read('count_files'), 2)
        self.assertEqual(self.service.resolve_path_identity(str(other))['file_id'], new)

    def test_offline_history_is_never_restored_by_registration(self):
        key = self.seed()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('UPDATE files SET is_offline=1 WHERE id=?', (key,))
            conn.commit()
        before = self.service.read('get_copy_version_state', key)
        with self.assertRaisesRegex(Exception, 'has_native_history'):
            self.apply()
        self.assertEqual(self.service.read('get_copy_version_state', key), before)

    def test_native_registration_race_cannot_overwrite_winner(self):
        real = self.service.submit
        keys = []
        def race(operation, *args, **kwargs):
            if operation == 'register_external_copy_observed':
                keys.append(self.seed())
                real('edit_curation', str(self.source), add_tags=['winner'], rating=5).result(3)
            return real(operation, *args, **kwargs)
        with patch.object(self.service, 'submit', side_effect=race):
            with self.assertRaisesRegex(Exception, 'has_native_history'):
                self.apply()
        state = self.service.read('get_copy_version_state', keys[0])['state']
        self.assertEqual(state['file']['rating'], 5)
        self.assertEqual([tag['name'] for tag in state['tags']], ['winner'])

    def test_fts_failure_rolls_back_identity_generation_curation_and_sequence(self):
        before = self.sql('SELECT * FROM sqlite_sequence')
        real = LibraryRepository._fts_sync_file
        calls = 0
        def fail_after_metadata(repo, *args, **kwargs):
            nonlocal calls
            calls += 1
            real(repo, *args, **kwargs)
            if calls == 2:
                raise OSError('private FTS failure')
        with patch.object(LibraryRepository, '_fts_sync_file', fail_after_metadata):
            with self.assertRaisesRegex(Exception, 'private FTS failure'):
                self.apply()
        self.assertEqual(calls, 2)
        self.assertEqual(self.service.read('count_files'), 0)
        self.assertEqual(self.sql('SELECT * FROM sqlite_sequence'), before)
        self.assertEqual(self.sql('SELECT * FROM tags'), [])

    def test_invalid_metadata_and_cancel_do_not_register(self):
        self.event.set()
        with self.assertRaisesRegex(Exception, 'cancelled'):
            self.apply()
        self.assertEqual(self.service.read('count_files'), 0)
        self.event.clear()
        with patch('core.external_registration.MetadataExtractor.extract', side_effect=ValueError('invalid metadata')):
            with self.assertRaisesRegex(Exception, 'invalid metadata'):
                self.apply()
        self.assertEqual(self.service.read('count_files'), 0)

    def test_source_handle_prevents_changes_until_admitted_registration_finishes(self):
        real = self.service.submit
        def checked(operation, *args, **kwargs):
            if operation == 'register_external_copy_observed':
                with self.assertRaises(OSError):
                    self.source.write_bytes(b'changed')
            return real(operation, *args, **kwargs)
        with patch.object(self.service, 'submit', side_effect=checked):
            self.assertIsInstance(self.apply(), int)

    def test_late_cancel_finishes_already_admitted_write(self):
        real = self.service.submit
        def submitted(operation, *args, **kwargs):
            value = real(operation, *args, **kwargs)
            if operation == 'register_external_copy_observed':
                self.assertTrue(value.result(3))
                self.event.set()
            return value
        with patch.object(self.service, 'submit', side_effect=submitted):
            key = self.apply()
        self.assertIsInstance(key, int)
        self.assertEqual(self.service.read('count_files'), 1)


if __name__ == '__main__':
    unittest.main()
