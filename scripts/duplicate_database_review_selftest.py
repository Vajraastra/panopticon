"""Grupos entre carpetas: SHA actual, IDs, documentación y snapshot completos."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.duplicate_policy import DuplicatePolicy
from modules.duplicate_finder.logic.database_review import review_group
from modules.duplicate_finder.logic.exact_review import _probe
import duplicate_exact_review_selftest as helpers

DIGEST=hashlib.sha256(b'same bytes').hexdigest()


class Review(helpers.Review):
    def review(self, **kwargs):
        return review_group(self.service,DIGEST,**kwargs)

    def report(self, value):
        return value['reports'][DIGEST]

    def test_cross_folder_curated_preferred_without_any_write(self):
        raw,curated=self.pair()
        before=self.service.read('get_duplicate_snapshot',[raw.as_posix(),curated.as_posix()])
        with patch.object(self.service,'submit',side_effect=AssertionError('review wrote')):
            value=self.review(expected_count=2)
        self.assertTrue(value['complete'])
        self.assertEqual(value['scope'],'database')
        self.assertEqual(self.report(value)['keep'],(curated.as_posix(),))
        self.assertEqual(self.report(value)['remove'],(raw.as_posix(),))
        self.assertEqual(self.service.read('get_duplicate_snapshot',[raw.as_posix(),curated.as_posix()]),before)

    def test_zero_weights_do_not_choose_raw_over_documented_copy(self):
        raw,curated=self.pair()
        weights=DuplicatePolicy(**dict.fromkeys(DuplicatePolicy.__dataclass_fields__,0))
        self.assertEqual(self.report(self.review(policy=weights))['keep'],(curated.as_posix(),))

    def test_stored_hash_is_not_proof_of_current_file_content(self):
        raw,curated=self.pair()
        raw.write_bytes(b'changed bytes')
        value=self.review()
        self.assertFalse(value['complete'])
        self.assertEqual(self.report(value)['remove'],())
        self.assertEqual(value['issues'][0]['reason'],'catalog_identity_mismatch')

    def test_identical_new_bytes_do_not_turn_stale_stored_hash_into_approval(self):
        raw,curated=self.pair()
        raw.write_bytes(b'new')
        curated.write_bytes(b'new')
        value=self.review()
        self.assertFalse(value['complete'])
        self.assertEqual(value['issue_count'],2)
        self.assertEqual(self.report(value)['remove'],())

    def test_missing_file_is_incomplete_and_never_a_retirement_candidate(self):
        raw,curated=self.pair()
        raw.unlink()
        value=self.review()
        self.assertFalse(value['complete'])
        self.assertEqual(self.report(value)['remove'],())
        self.assertTrue(curated.exists())

    def test_complementary_or_conflicting_data_retains_all(self):
        raw,curated=self.pair()
        self.service.submit('edit_curation',raw.as_posix(),add_tags=['other-data']).result(3)
        value=self.review()
        self.assertTrue(value['complete'])
        self.assertEqual(self.report(value)['remove'],())
        self.assertEqual(set(self.report(value)['keep']),{raw.as_posix(),curated.as_posix()})

    def test_caption_and_ancestor_sidecar_remain_unread_and_manual(self):
        raw,curated=self.pair()
        raw.with_suffix('.txt').write_text('manual caption')
        self.assertEqual(self.report(self.review())['reason'],'external_documentation_requires_review')
        raw.with_suffix('.txt').unlink()
        sidecar=self.root/'panopticon.curation.db'
        sidecar.write_bytes(b'opaque portable database')
        with patch('sqlite3.connect',side_effect=AssertionError('portable SQLite opened')):
            # Native SQL requires sqlite3, so intercept only the scanner's documentation probe.
            from modules.duplicate_finder.logic.file_guards import documented
            self.assertTrue(documented(raw))
        self.assertEqual(self.report(self.review())['remove'],())
        self.assertEqual(sidecar.read_bytes(),b'opaque portable database')

    def test_replaced_identity_after_member_listing_is_not_assumed_raw(self):
        raw,curated=self.pair()
        native=self.service.read
        def read(operation,*args,**kwargs):
            value=native(operation,*args,**kwargs)
            if operation=='get_duplicate_candidate_copies':
                state=native('get_duplicate_snapshot',[raw.as_posix()])[0]
                self.service.submit('archive_copy',state['state']['file']['id'],expected_fingerprint=state['fingerprint']).result(3)
            return value
        with patch.object(self.service,'read',read):
            value=self.review()
        self.assertFalse(value['complete'])
        self.assertEqual(self.report(value)['remove'],())

    def test_snapshot_prefix_or_reordered_records_fail_without_partial_result(self):
        self.pair()
        native=self.service.read
        for replacement in (lambda value:value[:1],lambda value:list(reversed(value))):
            def read(operation,*args,**kwargs):
                value=native(operation,*args,**kwargs)
                return replacement(value) if operation=='get_duplicate_snapshot' else value
            with patch.object(self.service,'read',read):
                with self.assertRaisesRegex(ValueError,'group_changed'):
                    self.review()

    def test_group_count_changed_before_verification_rejects_listing(self):
        self.pair()
        self.file('third.png')
        with self.assertRaisesRegex(ValueError,'group_changed'):
            self.review(expected_count=2)

    def test_257_member_group_fails_before_any_hashing(self):
        for index in range(257):
            self.file(f'{index}.png')
        with patch('modules.duplicate_finder.logic.database_review._probe',side_effect=AssertionError('prefix hashed')):
            with self.assertRaisesRegex(ValueError,'requires_separate_review'):
                self.review()

    def test_file_changed_after_probe_fails_final_observation(self):
        raw,curated=self.pair()
        def probe(path,event):
            value=_probe(path,event)
            if path==curated.as_posix():
                curated.write_bytes(b'after probe')
            return value
        with patch('modules.duplicate_finder.logic.database_review._probe',probe):
            value=self.review()
        self.assertFalse(value['complete'])
        self.assertEqual(self.report(value)['remove'],())

    def test_hardlink_aliases_are_manual(self):
        raw=self.file('a.png')
        other=self.root/'b.png'
        os.link(raw,other)
        self.service.submit('register_files_minimal',[dict(path=other.as_posix(),filename=other.name,
            size=other.stat().st_size,created=1,hash_original=DIGEST)]).result(3)
        value=self.review()
        self.assertEqual(self.report(value)['reason'],'ambiguous_path_identity')
        self.assertEqual(self.report(value)['remove'],())

    def test_cancel_before_and_during_hash_returns_no_partial_approval(self):
        self.pair()
        event=threading.Event()
        event.set()
        with self.assertRaisesRegex(RuntimeError,'cancelled'):
            self.review(cancel_event=event)
        event.clear()
        def probe(path,event):
            event.set()
            return _probe(path,event)
        with patch('modules.duplicate_finder.logic.database_review._probe',probe):
            with self.assertRaisesRegex(RuntimeError,'cancelled'):
                self.review(cancel_event=event)

    def test_progress_counts_complete_group_and_issues_are_sampled(self):
        for index in range(25):
            path=self.file(f'{index}.png')
            path.unlink()
        progress=[]
        value=self.review(progress=lambda current,total,path:progress.append((current,total)))
        self.assertEqual(progress[-1],(25,25))
        self.assertEqual(value['issue_count'],25)
        self.assertEqual(len(value['issues']),20)
        self.assertEqual(self.report(value)['remove'],())


if __name__=='__main__':
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('Review.'+name,sys.modules[__name__])
        for name in Review.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
