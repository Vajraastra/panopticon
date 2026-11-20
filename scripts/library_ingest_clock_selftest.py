"""Peer regressions for clock-before-classification and historical acknowledgements."""
import hashlib
import threading
import unittest
from uuid import uuid4

from library_ingest_review_selftest import IngestReview
from core.library_path_ingest import register
from core.copy_version_tasks import CopyVersionTasks
from core.library_service import LibraryError


class Peer(IngestReview):
    def capture(self,digest):
        self.service.submit('prepare_path_identity').result(5)
        self.service.backfill_path_identity()
        captured=[]
        register(self.service,[dict(self.data,hash_original=digest)],threading.Event(),
                 commit=lambda values: captured.extend(values) or True)
        return captured

    def test_stale_target_now_offline_retries_instead_of_acknowledging_a_false_conflict(self):
        digest=hashlib.sha256(self.path.read_bytes()).hexdigest()
        captured=self.capture(digest)
        before=self.state()
        CopyVersionTasks(self.service).read('make_copy_online',copy_id=self.key,expected_fingerprint=before['fingerprint'])
        active=self.service.read('get_file_id',str(self.path))
        current=self.service.read('get_copy_version_state',active)
        self.assertNotEqual(active,self.key)
        self.assertEqual(current['state']['file']['hash_original'],digest)
        operation=str(uuid4())
        with self.assertRaisesRegex(LibraryError,'path_identity_sql_state_changed'):
            self.service.submit('register_files_observed',captured,operation_id=operation).result(5)
        self.assertIsNone(self.receipt(operation));self.assertEqual(self.status()['open_count'],0)
        self.assertEqual(self.service.read('get_copy_version_state',active),current)

    def test_historical_receipt_replay_is_checked_before_current_clock(self):
        captured=self.capture('a'*64);operation=str(uuid4())
        self.service.submit('register_files_observed',captured,operation_id=operation).result(5)
        receipt=self.receipt(operation)
        self.service.submit('update_file_rating',str(self.path),4).result(5)
        before=self.state()
        self.service.submit('register_files_observed',captured,operation_id=operation).result(5)
        self.assertEqual(self.state(),before);self.assertEqual(self.receipt(operation),receipt)
        self.assertEqual(self.status()['open_count'],0)


if __name__=='__main__':
    names=[name for name in Peer.__dict__ if name.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(Peer(name) for name in names))
    raise SystemExit(not result.wasSuccessful())
