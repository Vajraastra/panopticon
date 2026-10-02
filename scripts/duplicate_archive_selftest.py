"""CAS del grupo completo en el dueño central; fixtures sin retiro físico."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
from pathlib import Path
import sys
import unittest
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.duplicate_policy import DuplicatePolicy
from core.library_connections import open_reader
from core.library_repository import LibraryRepository
import duplicate_exact_review_selftest as helpers


class Archive(helpers.Review):
    def approval(self, paths):
        current = self.service.read('get_duplicate_snapshot', [p.as_posix() for p in paths])
        return [dict(path=record['path'], copy_id=record['state']['file']['id'],
                     fingerprint=record['fingerprint']) for record in current]

    def command(self, approved, selected, **kwargs):
        return self.service.submit('archive_duplicate_copies', approved, selected,
            digest=kwargs.get('digest', hashlib.sha256(b'same bytes').hexdigest()),
            policy=kwargs.get('policy', asdict(DuplicatePolicy())), expected_scope=kwargs.get('expected_scope', [])).result(3)

    def full_state(self, approved):
        return [self.service.read('get_copy_version_state', row['copy_id']) for row in approved]

    def test_prepared_duplicate_retirement_keeps_complete_offline_identity(self):
        raw,curated=self.pair()
        self.service.backfill_path_identity()
        approved=self.approval((raw,curated))
        self.command(approved,[raw.as_posix()])
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)
        self.assertEqual(self.service.resolve_path_identity(str(curated))['file_id'],approved[1]['copy_id'])
        self.assertEqual(self.service.read('get_copy_version_state',approved[0]['copy_id'])['state']['file']['is_offline'],1)

    def test_only_raw_archived_curated_preserved_with_all_data_and_bytes(self):
        raw, curated = self.pair()
        approved = self.approval((raw, curated))
        before = self.full_state(approved)
        self.assertIs(self.command(approved, [raw.as_posix()]), True)
        after = self.full_state(approved)
        expected = deepcopy(before[0]['state'])
        expected['file']['is_offline'] = 1
        self.assertEqual(after[0]['state'], expected)
        self.assertEqual(after[1], before[1])
        self.assertTrue(raw.is_file() and curated.is_file())
        self.assertEqual(self.service.read('count_files'), 1)

    def test_cannot_archive_winner_or_entire_group(self):
        raw, curated = self.pair()
        approved = self.approval((raw, curated))
        before = self.full_state(approved)
        for selected in ([curated.as_posix()], [raw.as_posix(), curated.as_posix()]):
            with self.assertRaises(Exception):
                self.command(approved, selected)
            self.assertEqual(self.full_state(approved), before)

    def test_winner_curation_changed_rejects_even_if_raw_unchanged(self):
        raw, curated = self.pair()
        approved = self.approval((raw, curated))
        self.service.submit('edit_curation', curated.as_posix(), add_tags=['after-review']).result(3)
        before = self.full_state(approved)
        with self.assertRaisesRegex(Exception, 'duplicate_group_changed_since_review'):
            self.command(approved, [raw.as_posix()])
        self.assertEqual(self.full_state(approved), before)

    def test_raw_new_information_rejects_without_losing_it(self):
        raw, curated = self.pair()
        approved = self.approval((raw, curated))
        self.service.submit('edit_curation', raw.as_posix(), add_tags=['unique']).result(3)
        before = self.full_state(approved)
        with self.assertRaisesRegex(Exception, 'duplicate_group_changed_since_review'):
            self.command(approved, [raw.as_posix()])
        self.assertEqual(self.full_state(approved), before)

    def test_captured_winner_archived_or_replaced_rejects(self):
        raw, curated = self.pair()
        approved = self.approval((raw, curated))
        self.service.submit('archive_copy', approved[1]['copy_id'], expected_fingerprint=approved[1]['fingerprint']).result(3)
        with self.assertRaisesRegex(Exception, 'duplicate_group_changed_since_review'):
            self.command(approved, [raw.as_posix()])
        self.service.submit('make_copy_online', approved[1]['copy_id'], expected_fingerprint=self.full_state(approved)[1]['fingerprint'],
            digest='b'*64, observation=dict(filename=curated.name, size=10, created=1)).result(3)
        with self.assertRaisesRegex(Exception, 'duplicate_group_changed_since_review'):
            self.command(approved, [raw.as_posix()])
        self.assertEqual(self.full_state(approved)[0]['state']['file']['is_offline'], 0)

    def test_current_complementary_curation_never_authorizes_discard(self):
        a = self.file('a.png', tags=['alpha'])
        b = self.file('b.png', tags=['beta'])
        approved = self.approval((a, b))
        before = self.full_state(approved)
        with self.assertRaisesRegex(Exception, 'duplicate_archive_selection_not_safe'):
            self.command(approved, [a.as_posix()])
        self.assertEqual(self.full_state(approved), before)

    def test_current_conflicting_ratings_never_authorize_discard(self):
        a = self.file('a.png', rating=1)
        b = self.file('b.png', rating=5)
        approved = self.approval((a, b))
        with self.assertRaisesRegex(Exception, 'duplicate_archive_selection_not_safe'):
            self.command(approved, [a.as_posix()], policy=asdict(DuplicatePolicy(rating_weight=1000)))

    def test_stale_catalog_digest_and_tampered_id_fingerprint_reject(self):
        raw, curated = self.pair()
        approved = self.approval((raw, curated))
        for field, value in (('copy_id', -999), ('fingerprint', 'c'*64)):
            wrong = deepcopy(approved)
            wrong[1][field] = value
            with self.assertRaisesRegex(Exception, 'duplicate_group_changed_since_review'):
                self.command(wrong, [raw.as_posix()])
        with self.assertRaisesRegex(Exception, 'duplicate_archive_selection_not_safe'):
            self.command(approved, [raw.as_posix()], digest='d'*64)
        self.assertEqual(self.service.read('count_files'), 2)

    def test_id_required_even_when_fingerprint_matches(self):
        raw, curated = self.pair()
        approved = self.approval((raw, curated))
        wrong = deepcopy(approved)
        wrong[1]['copy_id'] = approved[0]['copy_id']
        with self.assertRaisesRegex(Exception, 'invalid_duplicate_archive'):
            self.command(wrong, [raw.as_posix()])

    def test_invalid_payloads_no_side_effects(self):
        raw, curated = self.pair()
        approved = self.approval((raw, curated))
        for wrong in ([], approved[:1], approved*129, {}, [dict(approved[0], extra=True), approved[1]],
                      [dict(approved[0], copy_id=True), approved[1]], [dict(approved[0], path=''), approved[1]],
                      [dict(approved[0], path=raw.as_posix().replace('/', '\\')), approved[1]]):
            with self.assertRaises(Exception):
                self.command(wrong, [raw.as_posix()])
        for selected in ([], [123], [raw.as_posix(), raw.as_posix()], ['C:/missing.png']):
            with self.assertRaises(Exception):
                self.command(approved, selected)
        for policy in ({}, asdict(DuplicatePolicy()) | {'extra': 1}, asdict(DuplicatePolicy()) | {'rating_weight': True}):
            with self.assertRaises(Exception):
                self.command(approved, [raw.as_posix()], policy=policy)
        for digest in ('', 'D'*64, 1):
            with self.assertRaises(Exception):
                self.command(approved, [raw.as_posix()], digest=digest)
        self.assertEqual(self.service.read('count_files'), 2)

    def test_subset_of_safe_losers_supported(self):
        raw, curated = self.pair()
        other = self.file('another.png')
        approved = self.approval((raw, curated, other))
        self.assertIs(self.command(approved, [raw.as_posix()]), True)
        self.assertEqual(self.service.read('count_files'), 2)
        self.assertEqual(self.full_state(approved)[2]['state']['file']['is_offline'], 0)

    def test_many_copies_use_four_shared_selects_including_scope_not_per_copy(self):
        raw = [self.file(f'{i:03}.png') for i in range(99)]
        curated = self.file('winner.png', rating=5)
        approved = self.approval((*raw, curated))
        with closing(open_reader(self.service.db_path)) as conn:
            conn.execute('BEGIN')
            statements = []
            conn.set_trace_callback(statements.append)
            # read-only fixture connection rejects UPDATE after proving shared CAS queries.
            with self.assertRaises(Exception):
                LibraryRepository(conn).archive_duplicate_copies(approved, [p.as_posix() for p in raw],
                    digest=hashlib.sha256(b'same bytes').hexdigest(), policy=asdict(DuplicatePolicy()), expected_scope=[])
            self.assertEqual(sum(statement.startswith('SELECT') for statement in statements), 4)
        self.assertEqual(self.service.read('count_files'), 100)

    def test_transaction_required_for_native_repository_entry(self):
        raw, curated = self.pair()
        with closing(open_reader(self.service.db_path)) as conn:
            with self.assertRaisesRegex(RuntimeError, 'duplicate_archive_requires_transaction'):
                LibraryRepository(conn).archive_duplicate_copies(self.approval((raw, curated)), [raw.as_posix()],
                    digest=hashlib.sha256(b'same bytes').hexdigest(), policy=asdict(DuplicatePolicy()), expected_scope=[])

    def test_watched_folder_change_between_worker_and_owner_rejects(self):
        raw, curated = self.pair()
        approved = self.approval((raw, curated))
        self.service.submit('add_watched_folder', self.root.as_posix()).result(3)
        with self.assertRaisesRegex(Exception, 'duplicate_retirement_scope_changed'):
            self.command(approved, [raw.as_posix()], expected_scope=[])
        self.assertEqual(self.service.read('count_files'), 2)


if __name__ == '__main__':
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('Archive.'+name, sys.modules[__name__])
        for name in Archive.__dict__ if name.startswith('test_'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
