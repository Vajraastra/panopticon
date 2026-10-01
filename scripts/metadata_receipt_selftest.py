"""Historical acknowledgements with private Native DB/images and lost replies."""
from concurrent.futures import Future
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4, uuid1

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import metadata_files_selftest as fixtures
from core.generation_history import NAME
from core.library_connections import open_reader


class Receipts(fixtures.Files):
    def completed(self):
        result = self.edit()
        self.assertEqual(result['status'],'complete')
        journal = json.loads(Path(result['journal']).read_bytes())
        receipt = self.service.read('get_metadata_edit_receipt',journal['operation_id'])
        return result,journal,receipt

    def mutate(self,sql,parameters):
        with closing(sqlite3.connect(self.root/'private.db')) as conn:
            conn.execute(sql,parameters)
            conn.commit()

    def test_receipt_read_does_not_prepare_or_change_schema(self):
        with closing(open_reader(self.root/'private.db')) as conn:
            before = conn.execute('SELECT name,type,sql FROM sqlite_master ORDER BY name').fetchall()
        self.assertEqual(self.service.read('get_metadata_edit_receipt',str(uuid4())),
                         dict(prepared=False,receipt=None))
        with closing(open_reader(self.root/'private.db')) as conn:
            self.assertEqual(conn.execute('SELECT name,type,sql FROM sqlite_master ORDER BY name').fetchall(),before)
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_prepared_schema_without_operation_has_no_receipt(self):
        self.service.submit('prepare_generation_history').result()
        self.assertEqual(self.service.read('get_metadata_edit_receipt',str(uuid4())),
                         dict(prepared=True,receipt=None))

    def test_receipt_confirms_exact_operation_and_is_saved_in_journal(self):
        result,journal,reply = self.completed()
        receipt = reply['receipt']
        self.assertTrue(reply['prepared'])
        self.assertEqual(receipt,journal['receipt'])
        self.assertEqual(receipt['copy_id'],self.key)
        self.assertEqual(receipt['source_sha256'],hashlib.sha256(self.original).hexdigest())
        self.assertEqual(receipt['result_sha256'],hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(receipt['operation_id'],journal['operation_id'])
        self.assertEqual(len(receipt['request_fingerprint']),64)
        self.assertNotIn('previous',receipt)
        self.assertNotIn('generation',receipt)

    def test_receipt_stays_historical_after_curation_changes(self):
        result,journal,reply = self.completed()
        before = self.state()['fingerprint']
        self.service.submit('edit_curation',str(self.source),add_tags=['later review'],rating=3).result()
        self.assertNotEqual(self.state()['fingerprint'],before)
        self.assertEqual(self.service.read('get_metadata_edit_receipt',journal['operation_id']),reply)

    def test_receipt_does_not_reapply_previous_generation_after_later_edit(self):
        result,journal,reply = self.completed()
        captured = self.tasks.load(str(self.source), self.event)
        second = self.tasks.edit(captured,'later prompt','later bad')
        self.assertEqual(second['status'],'complete')
        current = self.state()
        self.assertEqual(self.service.read('get_metadata_edit_receipt',journal['operation_id']),reply)
        self.assertEqual(self.state(),current)
        self.assertEqual(current['state']['file']['meta_positive'],'later prompt')
        self.assertNotEqual(reply['receipt']['result_sha256'],current['state']['file']['hash_mod'])

    def test_receipt_does_not_materialize_large_previous_generation(self):
        result,journal,reply = self.completed()
        self.mutate(f'UPDATE {NAME} SET previous_json=? WHERE operation_id=?',
                    ('x'*2*1024*1024,journal['operation_id']))
        with self.assertRaises(ValueError):
            self.service.read('get_generation_history_page',self.key)
        self.assertEqual(self.service.read('get_metadata_edit_receipt',journal['operation_id']),reply)
        self.assertLess(len(json.dumps(reply).encode()),1024)

    def test_malformed_receipt_digest_is_rejected(self):
        result,journal,reply = self.completed()
        self.mutate(f'UPDATE {NAME} SET source_sha256=? WHERE operation_id=?',
                    ('invalid',journal['operation_id']))
        with self.assertRaisesRegex(ValueError,'generation_history_invalid_receipt'):
            self.service.read('get_metadata_edit_receipt',journal['operation_id'])

    def test_oversized_receipt_fingerprint_is_bounded_and_rejected(self):
        result,journal,reply = self.completed()
        self.mutate(f'UPDATE {NAME} SET request_fingerprint=? WHERE operation_id=?',
                    ('a'*2*1024*1024,journal['operation_id']))
        with self.assertRaisesRegex(ValueError,'generation_history_invalid_receipt'):
            self.service.read('get_metadata_edit_receipt',journal['operation_id'])

    def test_oversized_receipt_timestamp_is_rejected(self):
        result,journal,reply = self.completed()
        self.mutate(f'UPDATE {NAME} SET recorded_at=? WHERE operation_id=?',
                    ('x'*65537,journal['operation_id']))
        with self.assertRaisesRegex(ValueError,'generation_history_invalid_receipt'):
            self.service.read('get_metadata_edit_receipt',journal['operation_id'])

    def test_invalid_operation_ids_fail_without_preparing(self):
        for job in ('bad',str(uuid1()),str(uuid4()).upper(),None,7):
            with self.assertRaisesRegex(ValueError,'invalid_metadata_operation_id'):
                self.service.read('get_metadata_edit_receipt',job)
        self.assertFalse(self.service.read('get_generation_history_status')['prepared'])

    def test_lost_commit_ack_is_confirmed_without_resubmission(self):
        submit = self.service.submit
        commits = []
        def lost(operation,*args,**kwargs):
            future = submit(operation,*args,**kwargs)
            if operation=='commit_metadata_edit':
                self.assertTrue(future.result())
                commits.append(kwargs['operation_id'])
                reply = Future()
                reply.set_exception(RuntimeError('lost acknowledgement'))
                return reply
            return future
        with patch.object(self.service,'submit',lost):
            result,journal,reply = self.completed()
        self.assertEqual(commits,[journal['operation_id']])
        self.assertTrue(result['native_committed'])
        self.assertEqual(len(self.service.read('get_generation_history_page',self.key)['records']),1)
        self.assertEqual(reply['receipt'],journal['receipt'])

    def test_lost_ack_with_later_curation_preserves_later_review(self):
        submit = self.service.submit
        def lost(operation,*args,**kwargs):
            future = submit(operation,*args,**kwargs)
            if operation=='commit_metadata_edit':
                future.result()
                submit('edit_curation',str(self.source),add_tags=['later user tag'],rating=4).result()
                reply = Future()
                reply.set_exception(RuntimeError('lost acknowledgement'))
                return reply
            return future
        with patch.object(self.service,'submit',lost):
            result,journal,reply = self.completed()
        current = self.state()['state']
        self.assertEqual(current['file']['rating'],4)
        self.assertIn('later user tag',[tag['name'] for tag in current['tags']])
        self.assertEqual(len(self.service.read('get_generation_history_page',self.key)['records']),1)

    def test_true_reply_without_historical_receipt_remains_partial(self):
        submit = self.service.submit
        commits = []
        def unconfirmed(operation,*args,**kwargs):
            if operation=='commit_metadata_edit':
                commits.append(kwargs['operation_id'])
                reply = Future()
                reply.set_result(True)
                return reply
            return submit(operation,*args,**kwargs)
        with patch.object(self.service,'submit',unconfirmed):
            result = self.edit()
        self.assertEqual(result['status'],'partial')
        self.assertTrue(result['published'])
        self.assertFalse(result['native_committed'])
        self.assertEqual(result['error'],'metadata_receipt_unconfirmed')
        self.assertEqual(len(commits),1)
        journal = json.loads(Path(result['journal']).read_bytes())
        self.assertTrue(Path(journal['backup']).exists())
        self.assertNotEqual(self.source.read_bytes(),self.original)

    def test_lost_ack_and_missing_receipt_does_not_assume_rollback_or_resubmit(self):
        submit,read = self.service.submit,self.service.read
        commits = []
        def lost(operation,*args,**kwargs):
            future = submit(operation,*args,**kwargs)
            if operation=='commit_metadata_edit':
                future.result()
                commits.append(kwargs['operation_id'])
                reply = Future()
                reply.set_exception(RuntimeError('lost acknowledgement'))
                return reply
            return future
        def missing(operation,*args,**kwargs):
            if operation=='get_metadata_edit_receipt':
                return dict(prepared=True,receipt=None)
            return read(operation,*args,**kwargs)
        with patch.object(self.service,'submit',lost),patch.object(self.service,'read',missing):
            result = self.edit()
        self.assertEqual(result['status'],'partial')
        self.assertFalse(result['native_committed'])
        self.assertTrue(result['published'])
        self.assertEqual(result['error'],'lost acknowledgement')
        self.assertEqual(len(commits),1)
        self.assertIsNotNone(read('get_metadata_edit_receipt',commits[0])['receipt'])
        self.assertEqual(self.state()['state']['file']['hash_mod'],hashlib.sha256(self.source.read_bytes()).hexdigest())


if __name__=='__main__':
    unittest.main(verbosity=2)
