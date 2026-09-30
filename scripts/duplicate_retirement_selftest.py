"""Retiro real en fixtures Windows: bytes recuperables, CAS y recibos intactos."""
from copy import deepcopy
from dataclasses import asdict
from concurrent.futures import Future
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.duplicate_policy import DuplicatePolicy
from modules.duplicate_finder.logic.retirement import DuplicateRetirementTasks, _write_journal
from modules.duplicate_finder.logic.exact_review import _probe
import duplicate_exact_review_selftest as helpers


class Retirement(helpers.Review):
    def setUp(self):
        super().setUp()
        self.recovery = (Path(self.temp.name)/'persistent-recovery').resolve()
        self.tasks = DuplicateRetirementTasks(self.service)

    def review(self, folder=None, policy=None):
        from modules.duplicate_finder.logic.exact_review import ExactDuplicateReview
        weights = policy or DuplicatePolicy()
        result = ExactDuplicateReview(self.service, weights).scan(str(folder or self.root))
        result.update(mode='hash', folder=str(folder or self.root), policy=asdict(weights), token=1, cancelled=False, error=None)
        return result

    def retire(self, review, *paths, event=None, recovery=None):
        return self.tasks.read('retire_duplicates', review=review, selected=[path.as_posix() for path in paths],
            recovery_folder=str(recovery or self.recovery), cancel_event=event)

    def states(self, review):
        return [self.service.read('get_copy_version_state', record['state']['file']['id'])
                for report in review['reports'].values() for record in report['copies']]

    def test_raw_moved_recoverably_curated_and_cherry_receipts_untouched(self):
        raw, curated = self.pair()
        catalog, history = self.root/'catalog.db', self.root/'history.db'
        catalog.write_bytes(b'portable catalog and URL receipts')
        history.write_bytes(b'portable history')
        review = self.review()
        before = self.states(review)
        result = self.retire(review, raw)
        self.assertTrue(result['complete'])
        self.assertEqual(result['moved'], 1)
        self.assertFalse(raw.exists())
        self.assertEqual(curated.read_bytes(), b'same bytes')
        entry = result['groups'][0]
        destination = Path(entry['moved'][0]['destination'])
        self.assertEqual(destination.read_bytes(), b'same bytes')
        self.assertNotIn(self.root, destination.parents)
        journal = json.loads(Path(entry['journal']).read_text(encoding='utf-8'))
        self.assertEqual(journal['phase'], 'complete')
        self.assertEqual(journal['moves'][0]['source'], raw.as_posix())
        self.assertEqual(journal['copies'][1]['state'], before[1]['state'])
        after = self.states(review)
        expected = deepcopy(before[0]['state'])
        expected['file']['is_offline'] = 1
        self.assertEqual(after[0]['state'], expected)
        self.assertEqual(after[1], before[1])
        self.assertEqual(catalog.read_bytes(), b'portable catalog and URL receipts')
        self.assertEqual(history.read_bytes(), b'portable history')

    def test_changed_bytes_on_kept_copy_reject_before_commit(self):
        raw, curated = self.pair()
        review = self.review()
        before = self.states(review)
        curated.write_bytes(b'changed')
        result = self.retire(review, raw)
        self.assertEqual(result['groups'][0]['error'], 'duplicate_bytes_changed_since_review')
        self.assertEqual((result['moved'], result['groups'][0]['archived']), (0, False))
        self.assertEqual(self.states(review), before)
        self.assertTrue(raw.exists())

    def test_documentation_created_after_review_blocks_retirement(self):
        raw, curated = self.pair()
        review = self.review()
        for path in (raw.with_suffix('.txt'), Path(str(raw)+'.json'), self.root.parent/'panopticon.curation.db'):
            path.write_bytes(b'documentation')
            result = self.retire(review, raw)
            self.assertEqual(result['groups'][0]['error'], 'external_documentation_requires_review')
            self.assertTrue(raw.exists() and curated.exists())
            self.assertEqual(self.service.read('count_files'), 2)
            path.unlink()

    def test_scanner_respects_sidecar_above_selected_scope_and_extended_json(self):
        raw, curated = self.pair()
        sidecar = self.root.parent/'panopticon.curation.db'
        sidecar.write_bytes(b'opaque')
        self.assertEqual(self.report(self.review())['reason'], 'external_documentation_requires_review')
        sidecar.unlink()
        Path(str(raw)+'.json').write_bytes(b'opaque')
        self.assertEqual(self.report(self.review())['reason'], 'external_documentation_requires_review')

    def test_winner_curation_changes_between_reader_and_owner_rejected_atomically(self):
        raw, curated = self.pair()
        review = self.review()
        submit = self.service.submit
        def racing(operation, *args, **kwargs):
            if operation == 'archive_duplicate_copies':
                submit('edit_curation', curated.as_posix(), add_tags=['new evidence']).result(3)
            return submit(operation, *args, **kwargs)
        with patch.object(self.service, 'submit', racing):
            result = self.retire(review, raw)
        self.assertIn('duplicate_group_changed_since_review', result['groups'][0]['error'])
        self.assertFalse(result['groups'][0]['archived'])
        self.assertEqual(self.service.read('count_files'), 2)
        self.assertTrue(raw.exists() and curated.exists())
        journal = json.loads(Path(result['groups'][0]['journal']).read_text(encoding='utf-8'))
        self.assertEqual(journal['phase'], 'rejected')

    def test_winner_changes_after_commit_retains_source_and_reports_partial(self):
        raw, curated = self.pair()
        review = self.review()
        submit = self.service.submit
        def racing(operation, *args, **kwargs):
            future = submit(operation, *args, **kwargs)
            future.result(3)
            curated.write_bytes(b'changed after commit')
            return future
        with patch.object(self.service, 'submit', racing):
            result = self.retire(review, raw)
        self.assertFalse(result['complete'])
        self.assertTrue(result['groups'][0]['archived'])
        self.assertEqual(result['groups'][0]['error'], 'duplicate_bytes_changed_after_commit')
        self.assertEqual(result['moved'], 0)
        self.assertEqual(raw.read_bytes(), b'same bytes')
        self.assertEqual(self.states(review)[0]['state']['file']['is_offline'], 1)

    def test_rename_failure_retains_bytes_and_durable_plan(self):
        raw, curated = self.pair()
        review = self.review()
        with patch('modules.duplicate_finder.logic.retirement.os.rename', side_effect=OSError('fixture busy file')):
            result = self.retire(review, raw)
        self.assertEqual((result['complete'], result['moved']), (False, 0))
        self.assertEqual(result['groups'][0]['error'], 'fixture busy file')
        journal = json.loads(Path(result['groups'][0]['journal']).read_text(encoding='utf-8'))
        self.assertEqual(journal['phase'], 'partial')
        self.assertTrue(raw.exists() and curated.exists())
        self.assertEqual(journal['moves'][0]['status'], 'pending')

    def test_commit_must_be_confirmed_true_before_any_movement(self):
        raw, curated = self.pair()
        review = self.review()
        future = Future()
        future.set_result(False)
        with patch.object(self.service, 'submit', return_value=future):
            result = self.retire(review, raw)
        self.assertEqual(result['groups'][0]['error'], 'duplicate_retirement_commit_unconfirmed')
        self.assertTrue(raw.exists() and curated.exists())
        self.assertEqual(self.service.read('count_files'), 2)

    def test_prepared_journal_write_failure_never_archives_or_moves(self):
        raw, curated = self.pair()
        review = self.review()
        with patch('modules.duplicate_finder.logic.retirement._write_journal', side_effect=OSError('journal full')):
            result = self.retire(review, raw)
        self.assertEqual(result['groups'][0]['error'], 'journal full')
        self.assertFalse(result['groups'][0]['archived'])
        self.assertEqual(self.service.read('count_files'), 2)
        self.assertTrue(raw.exists() and curated.exists())

    def test_post_commit_journal_error_stops_before_move_keeps_prepared_plan(self):
        raw, curated = self.pair()
        review = self.review()
        def failing(job, value):
            if value['phase'] != 'prepared':
                raise OSError('journal unavailable')
            _write_journal(job, value)
        with patch('modules.duplicate_finder.logic.retirement._write_journal', failing):
            result = self.retire(review, raw)
        self.assertTrue(result['groups'][0]['archived'])
        self.assertEqual(result['moved'], 0)
        journal = json.loads(Path(result['groups'][0]['journal']).read_text(encoding='utf-8'))
        self.assertEqual(journal['phase'], 'prepared')
        self.assertTrue(raw.exists() and curated.exists())

    def test_occupied_recovery_destination_never_overwritten(self):
        raw, curated = self.pair()
        review = self.review()
        occupied = []
        def create_occupied(job, value):
            _write_journal(job, value)
            if value['phase'] == 'archived':
                path = Path(value['moves'][0]['destination'])
                path.write_bytes(b'preexisting unrelated data')
                occupied.append(path)
        with patch('modules.duplicate_finder.logic.retirement._write_journal', create_occupied):
            result = self.retire(review, raw)
        self.assertEqual(result['groups'][0]['error'], 'duplicate_recovery_destination_occupied')
        self.assertEqual(occupied[0].read_bytes(), b'preexisting unrelated data')
        self.assertTrue(raw.exists() and curated.exists())

    def test_post_move_verification_failure_preserves_recovered_bytes(self):
        raw, curated = self.pair()
        review = self.review()
        def failing(path, event):
            if self.recovery in Path(path).parents:
                return None, 'fixture verification failure'
            return _probe(path, event)
        with patch('modules.duplicate_finder.logic.retirement._probe', failing):
            result = self.retire(review, raw)
        self.assertEqual(result['groups'][0]['error'], 'duplicate_recovered_bytes_require_review')
        self.assertEqual(result['moved'], 1)
        self.assertEqual(Path(result['groups'][0]['moved'][0]['destination']).read_bytes(), b'same bytes')
        self.assertTrue(curated.exists())

    def test_cancellation_before_admission_never_moves_or_archives(self):
        raw, curated = self.pair()
        review = self.review()
        event = threading.Event()
        read = self.service.read
        def cancelled(*args, **kwargs):
            value = read(*args, **kwargs)
            event.set()
            return value
        with patch.object(self.service, 'read', cancelled):
            result = self.retire(review, raw, event=event)
        self.assertEqual(result['groups'][0]['error'], 'curation_cancelled')
        self.assertFalse(result['groups'][0]['archived'])
        self.assertTrue(raw.exists() and curated.exists())
        self.assertEqual(self.service.read('count_files'), 2)

    def test_cancellation_after_acceptance_finishes_recoverable_group(self):
        raw, curated = self.pair()
        review = self.review()
        event = threading.Event()
        submit = self.service.submit
        def accepted(*args, **kwargs):
            future = submit(*args, **kwargs)
            future.result(3)
            event.set()
            return future
        with patch.object(self.service, 'submit', accepted):
            result = self.retire(review, raw, event=event)
        self.assertTrue(result['complete'])
        self.assertEqual(result['moved'], 1)
        self.assertFalse(raw.exists())
        self.assertTrue(curated.exists())

    def test_pre_cancelled_request_has_no_filesystem_or_db_effects(self):
        raw, curated = self.pair()
        review = self.review()
        event = threading.Event()
        event.set()
        with self.assertRaisesRegex(RuntimeError, 'curation_cancelled'):
            self.retire(review, raw, event=event)
        self.assertFalse(self.recovery.exists())
        self.assertEqual(self.service.read('count_files'), 2)

    def test_recovery_inside_collection_or_cherry_ancestor_rejected(self):
        raw = self.file('project/raw.png')
        curated = self.file('project/curated.png', rating=5)
        review = self.review(self.root/'project')
        with self.assertRaisesRegex(ValueError, 'duplicate_recovery_inside_collection'):
            self.retire(review, raw, recovery=self.root/'project'/'retired')
        (self.root/'catalog.db').write_bytes(b'portable Cherry catalog')
        result = self.retire(review, raw, recovery=self.root/'retired')
        self.assertEqual(result['groups'][0]['error'], 'duplicate_recovery_inside_cherry_project')
        self.assertEqual(self.service.read('count_files'), 2)
        self.assertTrue(raw.exists() and curated.exists())

    def test_cross_volume_detected_before_admission_no_copy_unlink_fallback(self):
        raw, curated = self.pair()
        review = self.review()
        original = Path.stat
        def different_volume(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path == self.recovery:
                fields = {name: getattr(value, name) for name in dir(value) if name.startswith('st_')}
                fields['st_dev'] += 1
                return SimpleNamespace(**fields)
            return value
        with patch.object(Path, 'stat', different_volume):
            result = self.retire(review, raw)
        self.assertEqual(result['groups'][0]['error'], 'duplicate_recovery_requires_same_volume')
        self.assertEqual(self.service.read('count_files'), 2)
        self.assertTrue(raw.exists() and curated.exists())

    def test_selected_winner_conflict_or_unsupported_mode_rejected(self):
        raw, curated = self.pair()
        review = self.review()
        with self.assertRaisesRegex(ValueError, 'duplicate_archive_selection_not_safe'):
            self.retire(review, curated)
        for field, value in (('mode', 'visual'), ('complete', False), ('cancelled', True), ('error', 'failed')):
            wrong = deepcopy(review)
            wrong[field] = value
            with self.assertRaisesRegex(ValueError, 'invalid_duplicate_retirement_review'):
                self.retire(wrong, raw)
        self.assertTrue(raw.exists() and curated.exists())
        self.assertEqual(self.service.read('count_files'), 2)

    def test_second_group_failure_preserves_first_group_result_and_stops(self):
        raw, curated = self.pair()
        a = self.file('B/raw.png', data=b'different equal bytes')
        b = self.file('Y/curated.png', data=b'different equal bytes', rating=4)
        review = self.review()
        # First result order is inventory deterministic; force it in review explicitly.
        first = next(digest for digest, paths in review['groups'].items() if raw.as_posix() in paths)
        review['groups'] = dict(sorted(review['groups'].items(), key=lambda entry: entry[0] != first))
        review['reports'] = {digest: review['reports'][digest] for digest in review['groups']}
        a.write_bytes(b'changed')
        result = self.retire(review, raw, a)
        self.assertEqual((result['complete'], result['moved'], len(result['groups'])), (False, 1, 2))
        self.assertTrue(result['groups'][0]['archived'])
        self.assertFalse(result['groups'][1]['archived'])
        self.assertFalse(raw.exists())
        self.assertTrue(a.exists() and b.exists() and curated.exists())

    def test_truncated_native_snapshot_never_authorizes_incomplete_group(self):
        raw, curated = self.pair()
        review = self.review()
        read = self.service.read
        with patch.object(self.service, 'read', side_effect=lambda *args, **kwargs: read(*args, **kwargs)[:1]):
            result = self.retire(review, raw)
        self.assertEqual(result['groups'][0]['error'], 'duplicate_group_changed_since_review')
        self.assertEqual(self.service.read('count_files'), 2)

    def test_zero_weights_cannot_prefer_raw(self):
        raw, curated = self.pair()
        weights = DuplicatePolicy(**dict.fromkeys(DuplicatePolicy.__dataclass_fields__, 0))
        result = self.retire(self.review(policy=weights), raw)
        self.assertTrue(result['complete'])
        self.assertTrue(curated.exists())

    def test_recovery_inside_another_watched_folder_rejects_before_filesystem_mutation(self):
        raw, curated = self.pair()
        self.service.submit('add_watched_folder', Path(self.temp.name).resolve().as_posix()).result(3)
        with self.assertRaisesRegex(ValueError, 'duplicate_recovery_inside_library'):
            self.retire(self.review(), raw)
        self.assertFalse(self.recovery.exists())
        self.assertTrue(raw.exists() and curated.exists())
        self.assertEqual(self.service.read('count_files'), 2)

    def test_watched_scope_budget_rejects_instead_of_using_incomplete_prefix(self):
        raw, curated = self.pair()
        for index in range(257):
            self.service.submit('add_watched_folder', (self.root/f'watch-{index}').as_posix()).result(3)
        with self.assertRaisesRegex(ValueError, 'duplicate_retirement_scope_limit'):
            self.retire(self.review(), raw)
        self.assertFalse(self.recovery.exists())
        self.assertTrue(raw.exists() and curated.exists())


if __name__ == '__main__':
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('Retirement.'+name, sys.modules[__name__])
        for name in Retirement.__dict__ if name.startswith('test_'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
