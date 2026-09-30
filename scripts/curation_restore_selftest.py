"""Restore por copia, CAS y rollback con SQLite temporal; nunca biblioteca real."""
from contextlib import closing
import copy
from pathlib import Path
import re
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.curation_restore import FIELDS, restore
from core.curation_sidecar import export_curation, read_curation_page
from core.library_repository import LibraryRepository
import curation_snapshot_selftest as helpers


class Restore(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.paths = [(Path(self.temp.name) / name).as_posix() for name in ('one.png', 'two.png')]
        self.service.submit('register_files_minimal', [dict(path=path, filename=Path(path).name,
            size=1, created=1, hash_original='a'*64) for path in self.paths]).result(3)

    def read(self, path=None):
        return self.service.read('get_curation_restore_state', path or self.paths[0])

    def incoming(self, state=None, **changes):
        value = {key: copy.deepcopy((state or self.read()['state'])[key]) for key in FIELDS}
        value.update(changes)
        return value

    def apply(self, value, snapshot=None, path=None):
        path = path or self.paths[0]
        return self.service.submit('restore_curation', path,
            expected_fingerprint=(snapshot or self.read(path))['fingerprint'], incoming=value).result(3)

    def sql(self, query, args=()):
        with closing(sqlite3.connect(self.service.db_path)) as conn, conn:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute(query, args)

    def test_restore_preserves_each_copy_and_tag_sources_and_categories(self):
        other = self.read(self.paths[1])
        value = self.incoming(rating=5, tags=[dict(name='new', source='model', category='character')])
        self.assertTrue(self.apply(value))
        state = self.read()['state']
        self.assertEqual(state['rating'], 5)
        self.assertEqual(state['tags'], value['tags'])
        self.assertEqual(self.read(self.paths[1]), other)

    def test_explicit_empty_tags_and_zero_rating_are_authoritative(self):
        for path in self.paths:
            self.service.submit('edit_curation', path, add_tags=['shared'], rating=5).result(3)
        other = self.read(self.paths[1])
        self.assertTrue(self.apply(self.incoming(rating=0, tags=[])))
        self.assertEqual(self.read()['state']['tags'], [])
        self.assertEqual(self.read()['state']['rating'], 0)
        self.assertEqual(self.read(self.paths[1]), other)

    def test_noop_has_no_dml_and_does_not_touch_fts(self):
        statements = []
        original = LibraryRepository.restore_curation
        def traced(repo, *args, **kwargs):
            repo.conn.set_trace_callback(statements.append)
            try:
                return original(repo, *args, **kwargs)
            finally:
                repo.conn.set_trace_callback(None)
        with patch.object(LibraryRepository, 'restore_curation', traced), patch.object(
                LibraryRepository, '_fts_sync_file', side_effect=AssertionError('FTS')):
            self.assertTrue(self.apply(self.incoming()))
        self.assertFalse(any(re.match(r'\s*(INSERT|UPDATE|DELETE|REPLACE)', line) for line in statements))

    def test_stale_rating_approval_is_rejected(self):
        old = self.read()
        value = self.incoming(old['state'], rating=4)
        self.service.submit('edit_curation', self.paths[0], rating=2).result(3)
        with self.assertRaisesRegex(Exception, 'curation_changed_since_review'):
            self.apply(value, old)
        self.assertEqual(self.read()['state']['rating'], 2)

    def test_source_and_global_category_changes_invalidate_approval(self):
        self.service.submit('edit_curation', self.paths[0], add_tags=['existing']).result(3)
        for query in ("UPDATE file_tags SET source='changed'", "UPDATE tags SET category='changed'"):
            old = self.read()
            self.sql(query)
            current = self.read()
            with self.assertRaisesRegex(Exception, 'curation_changed_since_review'):
                self.apply(self.incoming(old['state'], rating=4), old)
            self.assertEqual(self.read(), current)

    def test_delete_recreate_with_same_path_and_hash_invalidates_approval(self):
        old = self.read()
        self.service.submit('remove_files', [self.paths[0]]).result(3)
        self.service.submit('register_files_minimal', [dict(path=self.paths[0], filename='one.png',
            size=1, created=1, hash_original='a'*64)]).result(3)
        self.assertNotEqual(self.read()['state']['id'], old['state']['id'])
        with self.assertRaisesRegex(Exception, 'curation_changed_since_review'):
            self.apply(self.incoming(old['state'], rating=4), old)

    def test_different_hash_mod_is_not_restored_even_with_fresh_approval(self):
        value = self.incoming(rating=4)
        self.sql('UPDATE files SET hash_mod=? WHERE path=?', ('b'*64, self.paths[0]))
        with self.assertRaisesRegex(Exception, 'identity_changed'):
            self.apply(value)
        self.assertEqual(self.read()['state']['hash_mod'], 'b'*64)

    def test_missing_hash_never_authorizes_restore(self):
        self.sql('UPDATE files SET hash_original=NULL WHERE path=?', (self.paths[0],))
        with self.assertRaisesRegex(Exception, 'identity_missing'):
            self.apply(self.incoming(rating=4))
        self.assertEqual(self.read()['state']['rating'], 0)

    def test_unregistered_copy_is_not_created_by_read_or_restore(self):
        absent = (Path(self.temp.name) / 'absent.png').as_posix()
        with self.assertRaisesRegex(Exception, 'file_not_registered'):
            self.read(absent)
        with self.assertRaisesRegex(Exception, 'file_not_registered'):
            self.apply(self.incoming(rating=4), self.read(), absent)
        self.assertEqual(self.service.read('count_files'), 2)

    def test_shared_category_conflict_preserves_both_copies_and_global_tag(self):
        self.service.submit('edit_curation', self.paths[1], add_tags=['shared'], rating=5).result(3)
        before = [self.read(path) for path in self.paths]
        value = self.incoming(rating=4, tags=[dict(name='new', source='manual', category='manual'),
                                            dict(name='shared', source='model', category='other')])
        with self.assertRaisesRegex(Exception, 'tag_category_conflict'):
            self.apply(value)
        self.assertEqual([self.read(path) for path in self.paths], before)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertIsNone(conn.execute("SELECT id FROM tags WHERE name='new'").fetchone())

    def test_nullable_category_and_source_are_preserved(self):
        value = self.incoming(tags=[dict(name='nullable', source=None, category=None)])
        self.apply(value)
        self.assertEqual(self.read()['state']['tags'], value['tags'])

    def test_payload_rejects_duplicates_normalization_and_invalid_fields(self):
        cases = [dict(rating=True), dict(rating=6), dict(hash_original='bad'),
                 dict(tags=[dict(name='TAG', source='manual', category='manual')]),
                 dict(tags=[dict(name=' tag', source='manual', category='manual')]),
                 dict(tags=[dict(name='ok', source=1, category='manual')]),
                 dict(tags=[dict(name='ok', source='x'*257, category='manual')]),
                 dict(tags=[dict(name='duplicate', source=None, category=None)]*2)]
        before = self.read()
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(Exception):
                self.apply(self.incoming(**changes), before)
            self.assertEqual(self.read(), before)

    def test_payload_more_than_256_tags_is_rejected_before_writes(self):
        before = self.read()
        value = self.incoming(tags=[dict(name=f'tag{i}', source=None, category=None) for i in range(257)])
        with self.assertRaisesRegex(Exception, 'restore_payload_too_large'):
            self.apply(value, before)
        self.assertEqual(self.read(), before)

    def test_oversized_native_field_is_visible_and_not_clipped(self):
        self.service.submit('edit_curation', self.paths[0], add_tags=['existing']).result(3)
        self.sql('UPDATE tags SET name=?', ('x'*400,))
        with self.assertRaisesRegex(Exception, 'invalid_restore_tag'):
            self.read()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT length(name) FROM tags').fetchone()[0], 400)

    def test_fts_failure_rolls_back_tags_links_rating_and_sequence(self):
        self.service.submit('edit_curation', self.paths[0], add_tags=['old'], rating=3).result(3)
        before = self.read()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            sequence = conn.execute('SELECT * FROM sqlite_sequence ORDER BY name').fetchall()
        value = self.incoming(rating=5, tags=[dict(name='new', source='model', category='character')])
        with patch.object(LibraryRepository, '_fts_sync_file', side_effect=sqlite3.OperationalError('forced_fts')):
            with self.assertRaisesRegex(Exception, 'forced_fts'):
                self.apply(value)
        self.assertEqual(self.read(), before)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM sqlite_sequence ORDER BY name').fetchall(), sequence)
            self.assertIsNone(conn.execute("SELECT id FROM tags WHERE name='new'").fetchone())
        self.assertFalse(self.service.read('check_fts')['stale'])

    def test_tag_names_update_search_once_source_and_rating_do_not_sync_fts(self):
        value = self.incoming(tags=[dict(name='findable', source='model', category='general')])
        self.apply(value)
        self.assertIn(self.paths[0], self.service.read('search_by_terms', ['findable'])[1])
        value['rating'] = 4
        value['tags'][0]['source'] = 'manual'
        with patch.object(LibraryRepository, '_fts_sync_file', side_effect=AssertionError('FTS')):
            self.assertTrue(self.apply(value))
        self.assertEqual(self.read()['state']['tags'][0]['source'], 'manual')

    def test_state_reader_uses_two_selects_and_has_no_dml(self):
        statements = []
        original = LibraryRepository.get_curation_restore_state
        def traced(repo, path):
            repo.conn.set_trace_callback(statements.append)
            return original(repo, path)
        with patch.object(LibraryRepository, 'get_curation_restore_state', traced):
            value = self.read()
        self.assertEqual(len([s for s in statements if s.startswith('SELECT')]), 2)
        self.assertEqual(len(value['fingerprint']), 64)
        self.assertFalse(any(re.match(r'\s*(INSERT|UPDATE|DELETE|REPLACE)', line) for line in statements))

    def test_two_queued_approvals_only_first_can_commit(self):
        old = self.read()
        first = self.service.submit('restore_curation', self.paths[0], expected_fingerprint=old['fingerprint'],
                                    incoming=self.incoming(old['state'], rating=3))
        second = self.service.submit('restore_curation', self.paths[0], expected_fingerprint=old['fingerprint'],
                                     incoming=self.incoming(old['state'], rating=4))
        self.assertTrue(first.result(3))
        with self.assertRaisesRegex(Exception, 'curation_changed_since_review'):
            second.result(3)
        self.assertEqual(self.read()['state']['rating'], 3)

    def test_sidecar_per_copy_payload_can_be_applied_explicitly(self):
        self.service.submit('edit_curation', self.paths[0], add_tags=['portable'], rating=5).result(3)
        export_curation(self.service.db_path, self.temp.name)
        saved = read_curation_page(self.temp.name)['copies'][0]
        self.service.submit('edit_curation', self.paths[0], remove_tags=['portable'], rating=0).result(3)
        self.assertTrue(self.apply({key: saved[key] for key in FIELDS}))
        self.assertEqual(self.read()['state']['rating'], 5)
        self.assertEqual(self.read(self.paths[1])['state']['rating'], 0)

    def test_direct_restore_without_transaction_is_refused(self):
        with closing(sqlite3.connect(self.service.db_path, isolation_level=None)) as conn:
            with self.assertRaisesRegex(RuntimeError, 'restore_requires_transaction'):
                restore(conn, self.paths[0], expected_fingerprint=self.read()['fingerprint'],
                        incoming=self.incoming(rating=4), update_fts=lambda _: None)


if __name__ == '__main__':
    unittest.main(verbosity=2)
