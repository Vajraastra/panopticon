"""SHA/inventario y revisión reales; archivos/SQLite temporales sin retirar bytes."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.duplicate_finder.logic.exact_review import ExactDuplicateReview, ReviewLimits, _probe
from core.duplicate_policy import DuplicatePolicy
import curation_snapshot_selftest as helpers


class Review(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.root = (Path(self.temp.name) / 'collection').resolve()
        self.root.mkdir()

    def file(self, name, data=b'same bytes', rating=0, tags=(), register=True):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if register:
            self.service.submit('register_files_minimal', [dict(path=path.as_posix(), filename=path.name,
                size=len(data), created=1, hash_original=hashlib.sha256(data).hexdigest())]).result(3)
            if tags or rating:
                self.service.submit('edit_curation', path.as_posix(), add_tags=tags, rating=rating).result(3)
        return path

    def pair(self):
        raw = self.file('A/raw.png')
        curated = self.file('Z/curated.png', rating=5, tags=['documented'])
        return raw, curated

    def scan(self, **kwargs):
        return ExactDuplicateReview(self.service, **kwargs).scan(str(self.root))

    def report(self, result):
        self.assertEqual(len(result['reports']), 1)
        return next(iter(result['reports'].values()))

    def test_current_sha_keeps_documented_copy_and_does_no_writes(self):
        raw, curated = self.pair()
        before = self.service.read('get_duplicate_snapshot', [raw.as_posix(), curated.as_posix()])
        with patch.object(self.service, 'submit', side_effect=AssertionError('scan writes')):
            result = self.scan()
        self.assertTrue(result['complete'])
        digest = hashlib.sha256(raw.read_bytes()).hexdigest()
        self.assertEqual(list(result['groups']), [digest])
        self.assertEqual(self.report(result)['keep'], (curated.as_posix(),))
        self.assertEqual(self.report(result)['remove'], (raw.as_posix(),))
        self.assertEqual(self.service.read('get_duplicate_snapshot', [raw.as_posix(), curated.as_posix()]), before)
        self.assertEqual(raw.read_bytes(), curated.read_bytes())

    def test_distinct_current_bytes_not_grouped_even_with_same_catalog_hash(self):
        a = self.file('a.png', b'AAAA')
        b = self.file('b.png', b'BBBB')
        result = self.scan()
        self.assertEqual((result['groups'], result['reports']), ({}, {}))
        self.assertTrue(result['complete'])
        self.assertNotEqual(a.read_bytes(), b.read_bytes())

    def test_catalog_changed_bytes_require_review_even_when_current_bytes_equal(self):
        raw, curated = self.pair()
        raw.write_bytes(b'new content')
        curated.write_bytes(b'new content')
        value = self.report(self.scan())
        self.assertEqual((value['reason'], value['remove']), ('catalog_identity_mismatch', ()))

    def test_conflicting_curation_retains_all_and_uses_configured_review_preference(self):
        manual = self.file('manual.png', tags=['manual'])
        rated = self.file('rated.png', rating=5)
        a = self.report(self.scan(policy=DuplicatePolicy(manual_tag_weight=1000, rating_weight=0)))
        b = self.report(self.scan(policy=DuplicatePolicy(manual_tag_weight=0, rating_weight=1000)))
        self.assertEqual((a['preferred'], b['preferred']), (manual.as_posix(), rated.as_posix()))
        self.assertEqual((a['remove'], b['remove']), ((), ()))

    def test_unregistered_and_offline_copies_cannot_be_assumed_undocumented(self):
        raw, _ = self.pair()
        self.service.submit('remove_files', [raw.as_posix()]).result(3)
        self.assertEqual(self.report(self.scan())['reason'], 'copy_not_registered')
        self.file('unknown.png', register=False)
        self.assertEqual(self.report(self.scan())['remove'], ())

    def test_caption_and_embedded_name_caption_require_review_and_are_preserved(self):
        raw, curated = self.pair()
        for caption in (curated.with_suffix('.txt'), Path(str(curated) + '.txt')):
            caption.write_text('curated training caption', encoding='utf-8')
            value = self.report(self.scan())
            self.assertEqual((value['reason'], value['remove']), ('external_documentation_requires_review', ()))
            self.assertEqual(caption.read_text(), 'curated training caption')
            caption.unlink()
        self.assertTrue(raw.is_file())

    def test_portable_sidecar_in_ancestor_blocks_automatic_discard_without_reading_it(self):
        paths = self.pair()
        sidecar = self.root / 'panopticon.curation.db'
        sidecar.write_bytes(b'foreign-or-unreviewed')
        native = self.service.read('get_duplicate_snapshot', [path.as_posix() for path in paths])
        with (patch.object(self.service, 'read', return_value=deepcopy(native)),
              patch('sqlite3.connect', side_effect=AssertionError('sidecar SQL'))):
            value = self.report(self.scan())
        self.assertEqual(value['reason'], 'external_documentation_requires_review')
        self.assertEqual(sidecar.read_bytes(), b'foreign-or-unreviewed')

    def test_missing_data_service_cannot_publish_safe_discard(self):
        self.pair()
        result = ExactDuplicateReview(None).scan(str(self.root))
        self.assertFalse(result['complete'])
        self.assertEqual(self.report(result)['reason'], 'central_snapshot_failed')
        self.assertEqual(self.report(result)['remove'], ())

    def test_snapshot_failure_keeps_group_and_reports_cause(self):
        self.pair()
        with patch.object(self.service, 'read', side_effect=ValueError('fixture-snapshot-error')):
            result = self.scan()
        self.assertEqual(self.report(result)['remove'], ())
        self.assertFalse(result['complete'])
        self.assertIn('fixture-snapshot-error', result['issues'][0]['reason'])

    def test_hash_failure_is_visible_and_never_reported_as_complete(self):
        raw, _ = self.pair()
        def probe(path, event):
            return (None, 'fixture-read-error') if path == raw.as_posix() else _probe(path, event)
        with patch('modules.duplicate_finder.logic.exact_review._probe', probe):
            result = self.scan()
        self.assertFalse(result['complete'])
        self.assertEqual(result['groups'], {})
        self.assertEqual(result['issue_count'], 1)

    def test_file_changed_after_hash_is_rejected_before_recommendation(self):
        raw, _ = self.pair()
        read = self.service.read
        def changed(*args, **kwargs):
            value = read(*args, **kwargs)
            raw.write_bytes(b'changed after hash')
            return value
        with patch.object(self.service, 'read', changed):
            result = self.scan()
        self.assertFalse(result['complete'])
        self.assertEqual(self.report(result)['reason'], 'current_bytes_unverified')
        self.assertEqual(self.report(result)['remove'], ())

    def test_hardlinks_are_marked_ambiguous_to_protect_path_aliases(self):
        import os
        raw, _ = self.pair()
        alias = self.root / 'alias.png'
        os.link(raw, alias)
        self.service.submit('register_files_minimal', [dict(path=alias.as_posix(), filename=alias.name,
            size=raw.stat().st_size, created=1, hash_original=hashlib.sha256(raw.read_bytes()).hexdigest())]).result(3)
        self.assertEqual(self.report(self.scan())['reason'], 'ambiguous_path_identity')
        self.assertTrue(raw.is_file())

    def test_symlink_leaf_is_reported_and_never_hashed(self):
        self.pair()
        leaf = self.root / 'linked.png'
        try:
            leaf.symlink_to(self.root / 'A/raw.png')
        except OSError:
            self.skipTest('symlinks unavailable in this fixture environment')
        result = self.scan()
        self.assertFalse(result['complete'])
        self.assertNotIn(leaf.as_posix(), result['proofs'])
        self.assertTrue(leaf.is_symlink())

    def test_link_marker_branch_skips_probe_even_without_symlink_privileges(self):
        from modules.duplicate_finder.logic.exact_review import _linked
        self.pair()
        leaf = self.file('marked-link.png')
        def linked(path):
            return path == leaf or _linked(path)
        def probe(path, event):
            self.assertNotEqual(path, leaf.as_posix())
            return _probe(path, event)
        with (patch('modules.duplicate_finder.logic.exact_review._linked', linked),
              patch('modules.duplicate_finder.logic.exact_review._probe', probe)):
            result = self.scan()
        self.assertFalse(result['complete'])
        self.assertEqual(result['issue_count'], 1)
        self.assertNotIn(leaf.as_posix(), result['proofs'])

    def test_pre_cancelled_or_invalid_folder_does_no_inventory(self):
        event = threading.Event()
        event.set()
        review = ExactDuplicateReview(self.service)
        with patch('os.walk', side_effect=AssertionError('inventory')):
            with self.assertRaisesRegex(RuntimeError, 'curation_cancelled'):
                review.scan(str(self.root), cancel_event=event)
            for folder in ('relative', '', None):
                with self.assertRaisesRegex(ValueError, 'invalid_duplicate_review_folder'):
                    review.scan(folder)

    def test_cancellation_during_hash_returns_no_partial_review(self):
        self.pair()
        event = threading.Event()
        def progress(*args):
            event.set()
        with self.assertRaisesRegex(RuntimeError, 'curation_cancelled'):
            ExactDuplicateReview(self.service).scan(str(self.root), progress, event)

    def test_inventory_group_copy_and_payload_limits_are_explicit_no_prefix_result(self):
        self.pair()
        for limits, reason in ((ReviewLimits(files=1), 'inventory_limit'),
                               (ReviewLimits(copies=1), 'copy_limit'),
                               (ReviewLimits(payload_bytes=100), 'payload_limit')):
            with self.assertRaisesRegex(ValueError, reason):
                self.scan(limits=limits)
        self.file('second-a.png', b'other bytes')
        self.file('second-b.png', b'other bytes')
        with self.assertRaisesRegex(ValueError, 'group_limit'):
            self.scan(limits=ReviewLimits(groups=1))

    def test_large_group_keeps_every_copy_for_review(self):
        paths = []
        data = b'same'
        for index in range(257):
            path = self.root / (str(index) + '.png')
            path.write_bytes(data)
            paths.append(path.as_posix())
        self.service.submit('register_files_minimal', [dict(path=path, filename=Path(path).name,
            size=4, created=1, hash_original=hashlib.sha256(data).hexdigest()) for path in paths]).result(3)
        report = self.report(self.scan())
        self.assertEqual((report['reason'], report['remove']), ('duplicate_group_limit', ()))
        self.assertEqual(set(report['keep']), set(paths))

    def test_snapshot_paths_are_batched_instead_of_reading_each_group(self):
        for index in range(100):
            data = index.to_bytes(4, 'little')
            self.file(str(index) + 'a.png', data)
            self.file(str(index) + 'b.png', data)
        read = self.service.read
        calls = []
        def tracked(operation, *args, **kwargs):
            calls.append((operation, len(args[0])))
            return read(operation, *args, **kwargs)
        with patch.object(self.service, 'read', tracked):
            result = self.scan()
        self.assertEqual(calls, [('get_duplicate_snapshot', 200)])
        self.assertEqual(sum(len(paths) for paths in result['groups'].values()), 200)

    def test_thread_pool_admission_is_bounded_to_256_files(self):
        for index in range(257):
            self.file(str(index) + '.png')
        admitted = []
        class Pool:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def map(self, function, paths):
                self_test.assertLessEqual(len(paths), 256)
                admitted.append(len(paths))
                return map(function, paths)
        self_test = self
        with patch('modules.duplicate_finder.logic.exact_review.ThreadPoolExecutor', return_value=Pool()):
            result = self.scan()
        self.assertEqual(self.report(result)['status'], 'review')
        self.assertEqual(admitted, [256, 1])

    def test_issue_samples_are_bounded_and_total_remains_exact(self):
        for index in range(30):
            self.file(str(index) + '.png')
        with patch('modules.duplicate_finder.logic.exact_review._probe', return_value=(None, 'read-error')):
            result = self.scan()
        self.assertEqual((result['issue_count'], len(result['issues']), result['complete']), (30, 20, False))

    def test_unique_sizes_skip_hash_and_metadata_reads(self):
        self.file('a.png', b'a')
        self.file('b.png', b'bb')
        with (patch('modules.duplicate_finder.logic.exact_review._probe', side_effect=AssertionError('hash')),
              patch.object(self.service, 'read', side_effect=AssertionError('DB read'))):
            result = self.scan()
        self.assertTrue(result['complete'])
        self.assertEqual(result['groups'], {})

    def test_invalid_limits_and_configuration_rejected_without_work(self):
        for value in (True, 0, -1, 100001):
            with self.assertRaises(ValueError):
                ReviewLimits(files=value)
        with self.assertRaises(ValueError):
            ExactDuplicateReview(self.service, policy={})


if __name__ == '__main__':
    unittest.main(verbosity=2)
