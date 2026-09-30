"""Preferencia de copia curada y snapshots por bloque; sólo SQLite temporal."""
from contextlib import closing
from copy import deepcopy
import hashlib
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.duplicate_policy import DuplicatePolicy, recommend
from core.duplicate_snapshot import encode
from core.library_repository import LibraryRepository
from core.library_connections import open_reader
import curation_snapshot_selftest as helpers

DIGEST = 'a' * 64


def copy(path, tags=(), rating=0, **metadata):
    state = dict(file=dict(id=1, path=path, is_offline=0, hash_original=DIGEST, hash_mod=None,
                           rating=rating, **metadata),
                 tags=[dict(name=name, source=source, category=category) for name, source, category in tags])
    return dict(path=path, state=state, fingerprint=hashlib.sha256(encode(state)).hexdigest())


class Preference(unittest.TestCase):
    def plan(self, *copies, policy=None, digests=None):
        return recommend(copies, verified_hashes=digests if digests is not None else
                         {record['path']: DIGEST for record in copies}, policy=policy)

    def test_curated_wins_even_when_raw_path_sorts_first(self):
        raw = copy('A/raw.png')
        curated = copy('Z/curated.png', [('documented', 'manual', 'general')], rating=5)
        self.assertEqual(self.plan(raw, curated)['keep'], ('Z/curated.png',))
        self.assertEqual(self.plan(curated, raw)['remove'], ('A/raw.png',))

    def test_generation_documented_copy_wins_without_tags_or_rating(self):
        raw = copy('A/raw.png')
        documented = copy('Z/documented.png', meta_model='Model', meta_positive='Prompt')
        self.assertEqual(self.plan(raw, documented)['keep'], ('Z/documented.png',))

    def test_zero_weights_cannot_disable_curated_safeguard(self):
        raw, curated = copy('A/raw.png'), copy('Z/curated.png', rating=1)
        policy = DuplicatePolicy(0, 0, 0, 0, 0)
        self.assertEqual(self.plan(raw, curated, policy=policy)['keep'], ('Z/curated.png',))

    def test_manual_and_automatic_tags_both_preserved_as_documentation(self):
        raw = copy('raw.png')
        for source in ('manual', 'auto', 'recognizer', None):
            curated = copy('curated.png', [('tag', source, 'general')])
            self.assertEqual(self.plan(raw, curated)['keep'], ('curated.png',))

    def test_complementary_tags_keep_both_pending(self):
        a = copy('a.png', [('a', 'manual', 'general')])
        b = copy('b.png', [('b', 'manual', 'general')])
        value = self.plan(a, b)
        self.assertEqual((value['status'], value['remove']), ('review', ()))
        self.assertEqual(value['keep'], ('a.png', 'b.png'))

    def test_tag_source_or_category_conflict_keeps_both(self):
        a = copy('a.png', [('tag', 'manual', 'general')])
        for source, category in [('auto', 'general'), ('manual', 'character')]:
            self.assertEqual(self.plan(a, copy('b.png', [('tag', source, category)]))['remove'], ())

    def test_rating_conflict_never_resolved_by_larger_rating(self):
        value = self.plan(copy('a.png', rating=3), copy('b.png', rating=5))
        self.assertEqual((value['status'], value['preferred'], value['remove']), ('review', 'b.png', ()))

    def test_curated_explicit_zero_rating_is_not_a_missing_value(self):
        a = copy('a.png', [('tag', 'manual', 'general')], rating=0)
        b = copy('b.png', [('tag', 'manual', 'general')], rating=5)
        self.assertEqual(self.plan(a, b)['status'], 'review')

    def test_generation_or_provenance_conflict_requires_review(self):
        for field in ('meta_positive', 'meta_model', 'meta_seed', 'origin_url', 'origin_site', 'artist'):
            self.assertEqual(self.plan(copy('a.png', **{field: 'A'}), copy('b.png', **{field: 'B'}))['status'], 'review')

    def test_weights_change_review_preference_without_marking_conflicts(self):
        manual = copy('manual.png', [('tag', 'manual', 'general')])
        rated = copy('rated.png', rating=5)
        a = self.plan(manual, rated, policy=DuplicatePolicy(manual_tag_weight=1000, rating_weight=0))
        b = self.plan(manual, rated, policy=DuplicatePolicy(manual_tag_weight=0, rating_weight=1000))
        self.assertEqual((a['preferred'], b['preferred']), ('manual.png', 'rated.png'))
        self.assertEqual((a['remove'], b['remove']), ((), ()))

    def test_unknown_tool_is_not_generation_documentation(self):
        a = copy('a.png', meta_tool='Unknown')
        b = copy('b.png', meta_tool='Tool')
        self.assertEqual(self.plan(a, b)['keep'], ('b.png',))

    def test_information_superset_can_be_kept_without_merging_rows(self):
        a = copy('a.png', [('tag', 'manual', 'general')])
        b = copy('b.png', [('tag', 'manual', 'general'), ('extra', 'manual', 'general')])
        before = deepcopy([a, b])
        self.assertEqual(self.plan(a, b)['keep'], ('b.png',))
        self.assertEqual([a, b], before)

    def test_equal_information_uses_stable_path_tie_not_input_order(self):
        a, b = copy('a.png', rating=1), copy('b.png', rating=1)
        self.assertEqual(self.plan(a, b)['keep'], self.plan(b, a)['keep'])

    def test_missing_hash_mixed_bytes_or_stale_catalog_cannot_remove(self):
        a, b = copy('a.png'), copy('b.png')
        self.assertEqual(self.plan(a, b, digests={'a.png': DIGEST})['remove'], ())
        self.assertEqual(self.plan(a, b, digests={'a.png': DIGEST, 'b.png': 'b' * 64})['remove'], ())
        b['state']['file']['hash_original'] = None
        self.assertEqual(self.plan(a, b)['reason'], 'catalog_identity_mismatch')

    def test_effective_modified_hash_is_required_not_immutable_original(self):
        a, b = copy('a.png'), copy('b.png')
        b['state']['file']['hash_mod'] = 'b' * 64
        self.assertEqual(self.plan(a, b)['remove'], ())
        self.assertEqual(self.plan(a, b)['reason'], 'catalog_identity_mismatch')

    def test_absent_offline_unreviewable_or_external_records_keep_all(self):
        a = copy('a.png')
        for change in ('missing', 'offline', 'fingerprint', 'external'):
            b = copy('b.png')
            if change == 'missing':
                b['state'] = None
            elif change == 'offline':
                b['state']['file']['is_offline'] = 1
            elif change == 'fingerprint':
                b['fingerprint'] = None
            else:
                b['external_documentation'] = True
            self.assertEqual(self.plan(a, b)['remove'], ())

    def test_tampered_state_cannot_reuse_approved_fingerprint(self):
        a, b = copy('a.png'), copy('b.png', rating=5)
        b['state']['file']['rating'] = 0
        self.assertEqual(self.plan(a, b)['reason'], 'state_unreviewable')

    def test_case_alias_does_not_authorize_removing_the_retained_file(self):
        a, b = copy('C:/images/Photo.png'), copy('C:/images/photo.png')
        self.assertEqual((self.plan(a, b)['reason'], self.plan(a, b)['remove']), ('ambiguous_path_identity', ()))

    def test_invalid_weights_and_group_inputs_rejected(self):
        for value in (True, -1, 1001, 1.5, '1'):
            with self.assertRaises(ValueError):
                DuplicatePolicy(rating_weight=value)
        for copies in ([], [copy('a')], [copy('a')] * 257):
            with self.assertRaises(ValueError):
                self.plan(*copies)
        with self.assertRaisesRegex(ValueError, 'path_repeated'):
            self.plan(copy('a'), copy('a'))


class Snapshot(helpers.Fixture):
    def register_identity(self, *paths):
        self.service.submit('register_files_minimal', [dict(path=path, filename=Path(path).name,
            size=1, created=1, hash_original=DIGEST) for path in paths]).result(3)

    def sql(self, statement, args=()):
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute(statement, args)
            conn.commit()

    def test_block_state_and_fingerprint_equal_version_review_with_no_writes(self):
        paths = ['C:/images/a.png', 'C:/images/b.png']
        self.register_identity(*paths)
        self.service.submit('edit_curation', paths[0], add_tags=['documented'], rating=5).result(3)
        with closing(open_reader(self.service.db_path)) as conn:
            trace = []
            conn.set_trace_callback(trace.append)
            values = LibraryRepository(conn).get_duplicate_snapshot(paths)
        for value in values:
            self.assertEqual({key: value[key] for key in ('state', 'fingerprint')},
                             self.service.read('get_copy_version_state', value['state']['file']['id']))
        self.assertFalse(any(statement.startswith(('INSERT', 'UPDATE', 'DELETE')) for statement in trace))

    def test_256_copies_use_three_selects_not_queries_per_copy(self):
        paths = [f'C:/images/{i}.png' for i in range(256)]
        self.register_identity(*paths)
        with closing(open_reader(self.service.db_path)) as conn:
            trace = []
            conn.set_trace_callback(trace.append)
            values = LibraryRepository(conn).get_duplicate_snapshot(paths)
        self.assertEqual(len(values), 256)
        self.assertEqual(sum(statement.startswith('SELECT') for statement in trace), 3)

    def test_normalized_unique_paths_missing_and_offline_are_not_raw_copies(self):
        a, b = 'C:/images/a.png', 'C:/images/b.png'
        self.register_identity(a, b)
        self.service.submit('remove_files', [b]).result(3)
        values = self.service.read('get_duplicate_snapshot', ['C:\\images\\a.png', a, b, 'C:/missing.png'])
        self.assertEqual([v['path'] for v in values], [a, b, 'C:/missing.png'])
        self.assertIsNotNone(values[0]['state'])
        self.assertEqual([v['state'] for v in values[1:]], [None, None])

    def test_invalid_and_empty_inputs_do_no_sql(self):
        with closing(open_reader(self.service.db_path)) as conn:
            trace = []
            conn.set_trace_callback(trace.append)
            repo = LibraryRepository(conn)
            for paths in ('path', [None], [''], ['x' * 4097], ['a'] * 257):
                with self.assertRaises(ValueError):
                    repo.get_duplicate_snapshot(paths)
            self.assertEqual(repo.get_duplicate_snapshot([]), [])
            self.assertEqual(trace, [])

    def test_overlarge_field_is_rejected_before_materialization(self):
        path = 'C:/images/a.png'
        self.register_identity(path)
        self.sql('UPDATE files SET meta_positive=?', ('x' * 65537,))
        with self.assertRaisesRegex(ValueError, 'payload_too_large'):
            self.service.read('get_duplicate_snapshot', [path])

    def test_total_payload_is_bounded_even_for_individually_small_fields(self):
        paths = [f'C:/images/{i}.png' for i in range(32)]
        self.register_identity(*paths)
        self.sql('UPDATE files SET meta_positive=?', ('x' * 40000,))
        with self.assertRaisesRegex(ValueError, 'payload_too_large'):
            self.service.read('get_duplicate_snapshot', paths)

    def test_tag_sources_categories_and_generation_complete_and_conflict_preserved(self):
        paths = ['C:/images/a.png', 'C:/images/b.png']
        self.register_identity(*paths)
        for index, path in enumerate(paths):
            self.service.submit('edit_curation', path, add_tags=['manual' + str(index)], rating=3).result(3)
        values = self.service.read('get_duplicate_snapshot', paths)
        self.assertEqual(values[0]['state']['tags'], [dict(name='manual0', source='manual', category='manual')])
        plan = recommend(values, verified_hashes=dict.fromkeys(paths, DIGEST))
        self.assertEqual((plan['status'], plan['remove']), ('review', ()))

    def test_tag_per_copy_limit_rejects_without_returning_partial_state(self):
        path = 'C:/images/a.png'
        self.register_identity(path)
        self.service.submit('edit_curation', path, add_tags=[str(i) for i in range(256)]).result(3)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            key = conn.execute('SELECT id FROM files').fetchone()[0]
            conn.execute("INSERT INTO tags(name) VALUES('one-extra')")
            conn.execute('INSERT INTO file_tags VALUES(?,last_insert_rowid(),?)', (key, 'manual'))
            conn.commit()
        with self.assertRaisesRegex(ValueError, 'payload_too_large'):
            self.service.read('get_duplicate_snapshot', [path])

    def test_tag_field_limit_rejects_without_truncating(self):
        path = 'C:/images/a.png'
        self.register_identity(path)
        self.service.submit('edit_curation', path, add_tags=['tag']).result(3)
        self.sql('UPDATE file_tags SET source=?', ('s' * 257,))
        with self.assertRaisesRegex(ValueError, 'payload_too_large'):
            self.service.read('get_duplicate_snapshot', [path])

    def test_block_tag_limit_rejects_even_when_each_copy_is_within_limit(self):
        paths = [f'C:/images/{i}.png' for i in range(17)]
        self.register_identity(*paths)
        for path in paths:
            self.service.submit('edit_curation', path, add_tags=[str(i) for i in range(241)]).result(3)
        with self.assertRaisesRegex(ValueError, 'payload_too_large'):
            self.service.read('get_duplicate_snapshot', paths)

    def test_custom_blob_is_preserved_for_review_without_partial_snapshot(self):
        paths = ['C:/images/a.png', 'C:/images/b.png']
        self.register_identity(*paths)
        self.sql('ALTER TABLE files ADD COLUMN custom BLOB')
        self.sql("UPDATE files SET custom=X'0011' WHERE path=?", (paths[1],))
        with self.assertRaisesRegex(ValueError, 'unsupported_duplicate_snapshot'):
            self.service.read('get_duplicate_snapshot', paths)
        with closing(open_reader(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT custom FROM files WHERE path=?', (paths[1],)).fetchone(), (b'\x00\x11',))

    def test_concurrent_commit_cannot_mix_file_rating_and_tags_in_snapshot(self):
        path = 'C:/images/a.png'
        self.register_identity(path)
        from core import library_service
        original = library_service.open_reader
        # Quoted columns are used; trigger after the budget SELECT established the snapshot.
        def reader(db):
            conn = original(db)
            def trace(statement):
                if statement.startswith('SELECT "id",'):
                    self.service.submit('edit_curation', path, add_tags=['later'], rating=5).result(3)
            conn.set_trace_callback(trace)
            return conn
        with patch.object(library_service, 'open_reader', reader):
            before = self.service.read('get_duplicate_snapshot', [path])[0]['state']
        self.assertEqual((before['file']['rating'], before['tags']), (0, []))
        after = self.service.read('get_duplicate_snapshot', [path])[0]['state']
        self.assertEqual(after['file']['rating'], 5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
