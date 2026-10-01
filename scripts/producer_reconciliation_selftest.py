"""Explicit reconciliation on private producer journals; no image restoration."""
from concurrent.futures import Future
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import producer_journal_review_selftest as fixtures
from scripts import producer_journal as cli
from core import producer_reconciliation as reconciliation
from core.producer_journal_review import review
from modules.metadata.logic.file_tasks import MetadataTasks


class Reconciliation(fixtures.Review):
    def pending_metadata(self,clear=False):
        tasks=MetadataTasks(self.service,self.root/'metadata')
        captured=tasks.load(str(self.source),self.events)
        submit=self.service.submit
        def fail(operation,*args,**kwargs):
            if operation=='commit_metadata_edit':
                future=Future()
                future.set_exception(RuntimeError('private commit failed'))
                return future
            return submit(operation,*args,**kwargs)
        with patch.object(self.service,'submit',fail):
            result=tasks.edit(captured,'' if clear else 'new ñ prompt','' if clear else 'new bad')
        self.assertEqual(result['status'],'partial',result)
        return Path(result['journal'])

    def apply(self,journal,token=None,event=None):
        token=token or review(self.db_path,journal)['review_fingerprint']
        return reconciliation.reconcile(self.service,journal,expected_review_fingerprint=token,event=event)

    def test_pending_cropper_registers_one_independent_copy_and_receipt(self):
        journal=self.derived(fail_commit=True)
        before=journal.read_bytes(),self.source.read_bytes(),self.output.read_bytes()
        result=self.apply(journal)
        self.assertEqual(result['status'],'complete',result)
        self.assertEqual(result['action'],'register_derived_copy')
        self.assertTrue(result['native_confirmed'])
        report=self.inspect(journal)
        self.assertEqual(report['status'],'confirmed_current')
        self.assertEqual(report['output']['rating'],0)
        self.assertEqual(report['output']['tag_count'],0)
        self.assertEqual((journal.read_bytes(),self.source.read_bytes(),self.output.read_bytes()),before)
        record=json.loads(Path(result['record']).read_bytes())
        self.assertEqual(record['phase'],'complete')
        self.assertEqual(record['receipt'],report['receipt']['receipt'])
        self.assertFalse((journal.parent/'producer-reconcile.lock').exists())

    def test_pending_optimizer_registers_verified_generation(self):
        journal=self.derived('optimizer',fail_commit=True)
        result=self.apply(journal)
        self.assertEqual(result['status'],'complete',result)
        self.assertEqual(self.inspect(journal)['receipt']['receipt']['producer'],'image_optimizer')

    def test_pending_metadata_updates_native_history_without_touching_images(self):
        journal=self.pending_metadata()
        before=self.source.read_bytes(),journal.read_bytes()
        result=self.apply(journal)
        self.assertEqual(result['status'],'complete',result)
        self.assertEqual(result['action'],'commit_metadata_edit')
        report=self.inspect(journal)
        self.assertEqual(report['status'],'confirmed_current')
        self.assertEqual(report['output']['rating'],5)
        self.assertEqual(report['output']['tag_count'],1)
        self.assertTrue(report['backup_matches_source'])
        self.assertEqual((self.source.read_bytes(),journal.read_bytes()),before)
        self.assertEqual(len(self.service.read('get_generation_history_page',self.snapshot['id'])['records']),1)

    def test_pending_metadata_clear_is_verified_and_keeps_curation(self):
        journal=self.pending_metadata(clear=True)
        result=self.apply(journal)
        self.assertEqual(result['status'],'complete',result)
        current=self.service.read('get_copy_version_state',self.snapshot['id'])['state']
        self.assertEqual(current['file']['meta_positive'],'')
        self.assertEqual(current['file']['meta_tool'],'')
        self.assertEqual(current['file']['rating'],5)

    def test_concrete_forged_metadata_value_cannot_replace_an_absence_label(self):
        journal=self.pending_metadata()
        self.rewrite(journal,lambda data:data['request']['generation'].update(model='forged known model'))
        with self.assertRaisesRegex(ValueError,'generation_requires_review'):
            self.apply(journal)
        self.assertIsNone(self.inspect(journal)['receipt']['receipt'])

    def test_known_previous_generation_cannot_be_rewritten_as_unknown(self):
        journal=self.pending_metadata()
        self.rewrite(journal,lambda data:data['request']['source_generation'].update(model='Unknown'))
        with self.assertRaisesRegex(ValueError,'previous_generation_requires_review'):
            self.apply(journal)
        self.assertIsNone(self.inspect(journal)['receipt']['receipt'])

    def test_existing_receipt_acknowledges_without_native_write_or_replay(self):
        journal=self.derived()
        before=self.fingerprint(),journal.read_bytes(),self.output.read_bytes()
        with patch.object(self.service,'submit',side_effect=AssertionError('historical replay')):
            result=self.apply(journal)
        self.assertEqual(result['action'],'acknowledge_historical_receipt')
        self.assertEqual(result['status'],'complete')
        self.assertEqual((self.fingerprint(),journal.read_bytes(),self.output.read_bytes()),before)

    def test_acknowledge_missing_output_never_restores_it(self):
        journal=self.derived()
        self.output.unlink()
        result=self.apply(journal)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['current_status'],'confirmed_history_current_changed')
        self.assertFalse(self.output.exists())

    def test_export_review_is_recorded_without_native_write(self):
        journal=self.metadata(export=True)
        before=self.fingerprint(),journal.read_bytes(),self.output.read_bytes()
        with patch.object(self.service,'submit',side_effect=AssertionError('export registered')):
            result=self.apply(journal)
        self.assertEqual(result['action'],'record_export_review')
        self.assertFalse(result['native_confirmed'])
        self.assertEqual((self.fingerprint(),journal.read_bytes(),self.output.read_bytes()),before)

    def test_stale_review_after_curation_change_rejects_without_registration(self):
        journal=self.derived(fail_commit=True)
        token=self.inspect(journal)['review_fingerprint']
        self.service.submit('edit_curation',str(self.source),add_tags=['later review'],rating=4).result()
        with self.assertRaisesRegex(ValueError,'changed_since_review'):
            self.apply(journal,token)
        self.assertIsNone(self.inspect(journal)['receipt']['receipt'])
        self.assertFalse((journal.parent/'producer-reconcile.lock').exists())

    def test_fresh_review_cannot_override_changed_source_curation(self):
        journal=self.derived(fail_commit=True)
        self.service.submit('edit_curation',str(self.source),add_tags=['later review'],rating=4).result()
        with self.assertRaisesRegex(ValueError,'manual_review'):
            self.apply(journal)
        self.assertIsNone(self.inspect(journal)['receipt']['receipt'])

    def test_changed_parent_bytes_block_pending_derived_registration(self):
        journal=self.derived(fail_commit=True)
        self.source.write_bytes(self.original+b'changed source')
        with self.assertRaisesRegex(ValueError,'manual_review'):
            self.apply(journal)
        self.assertIsNone(self.inspect(journal)['receipt']['receipt'])

    def test_new_destination_native_identity_is_not_overwritten(self):
        journal=self.derived(fail_commit=True)
        self.service.submit('register_files_minimal',[dict(path=str(self.output.resolve()),filename=self.output.name,
            size=self.output.stat().st_size,created=self.output.stat().st_ctime)]).result()
        self.service.submit('edit_curation',str(self.output.resolve()),add_tags=['curated winner'],rating=5).result()
        before=self.fingerprint()
        with self.assertRaisesRegex(ValueError,'manual_review'):
            self.apply(journal)
        self.assertEqual(self.fingerprint(),before)

    def test_changed_backup_prevents_pending_metadata_commit(self):
        journal=self.pending_metadata()
        Path(json.loads(journal.read_bytes())['backup']).write_bytes(b'corrupt backup')
        with self.assertRaisesRegex(ValueError,'manual_review'):
            self.apply(journal)
        self.assertIsNone(self.inspect(journal)['receipt']['receipt'])

    def test_tampered_generation_without_receipt_cannot_be_registered(self):
        journal=self.derived(fail_commit=True)
        self.rewrite(journal,lambda data:data['request']['generation'].update(positive='forged prompt'))
        with self.assertRaisesRegex(ValueError,'generation_requires_review'):
            self.apply(journal)
        self.assertIsNone(self.inspect(journal)['receipt']['receipt'])

    def test_preexisting_guard_requires_review_and_is_not_deleted(self):
        journal=self.derived(fail_commit=True)
        guard=journal.parent/'producer-reconcile.lock'
        guard.write_bytes(b'previous interrupted owner')
        with self.assertRaisesRegex(ValueError,'lock_requires_review'):
            self.apply(journal)
        self.assertEqual(guard.read_bytes(),b'previous interrupted owner')

    def test_recovery_folder_inside_watched_collection_is_not_written(self):
        journal=self.derived(fail_commit=True)
        self.service.submit('add_watched_folder',str(self.root)).result()
        before=set(journal.parent.iterdir())
        with self.assertRaisesRegex(ValueError,'inside_library'):
            self.apply(journal)
        self.assertEqual(set(journal.parent.iterdir()),before)

    def test_lost_ack_confirms_once_without_resubmission(self):
        journal=self.derived(fail_commit=True)
        submit=self.service.submit
        calls=[]
        def lost(operation,*args,**kwargs):
            future=submit(operation,*args,**kwargs)
            if operation=='register_derived_copy':
                future.result()
                calls.append(True)
                reply=Future()
                reply.set_exception(RuntimeError('lost acknowledgement'))
                return reply
            return future
        with patch.object(self.service,'submit',lost):
            result=self.apply(journal)
        self.assertEqual(result['status'],'complete',result)
        self.assertTrue(result['native_confirmed'])
        self.assertEqual(len(calls),1)

    def test_database_failure_keeps_intent_journal_and_output_without_retry(self):
        journal=self.derived(fail_commit=True)
        submit=self.service.submit
        calls=[]
        def fail(operation,*args,**kwargs):
            if operation=='register_derived_copy':
                calls.append(True)
                reply=Future()
                reply.set_exception(RuntimeError('private writer failed'))
                return reply
            return submit(operation,*args,**kwargs)
        before=journal.read_bytes(),self.output.read_bytes()
        with patch.object(self.service,'submit',fail):
            result=self.apply(journal)
        self.assertEqual(result['status'],'review_required')
        self.assertFalse(result['native_confirmed'])
        self.assertEqual(len(calls),1)
        self.assertEqual((journal.read_bytes(),self.output.read_bytes()),before)
        self.assertEqual(json.loads(Path(result['record']).read_bytes())['phase'],'review_required')

    def test_cancel_before_action_does_not_create_guard_or_record(self):
        journal=self.derived(fail_commit=True)
        before=set(journal.parent.iterdir())
        event=threading.Event()
        event.set()
        with self.assertRaisesRegex(ValueError,'cancelled'):
            self.apply(journal,event=event)
        self.assertEqual(set(journal.parent.iterdir()),before)

    def test_cancel_pending_native_command_preserves_intent_and_cancels_once(self):
        journal=self.derived(fail_commit=True)
        event=threading.Event()
        future=Future()
        submit=self.service.submit
        def pending(operation,*args,**kwargs):
            if operation=='register_derived_copy':
                event.set()
                return future
            return submit(operation,*args,**kwargs)
        with patch.object(self.service,'submit',pending):
            result=self.apply(journal,event=event)
        self.assertEqual(result['status'],'review_required')
        self.assertTrue(future.cancelled())
        self.assertTrue(self.output.exists())
        self.assertFalse(result['native_confirmed'])

    def test_cancel_after_admitted_commit_keeps_receipt_confirmation(self):
        journal=self.derived(fail_commit=True)
        event=threading.Event()
        submit=self.service.submit
        def admitted(operation,*args,**kwargs):
            future=submit(operation,*args,**kwargs)
            if operation=='register_derived_copy':
                future.result()
                event.set()
            return future
        with patch.object(self.service,'submit',admitted):
            result=self.apply(journal,event=event)
        self.assertEqual(result['status'],'complete',result)
        self.assertTrue(result['native_confirmed'])
        self.assertTrue(self.output.exists())

    def test_record_failure_before_admission_does_not_write_native(self):
        journal=self.derived(fail_commit=True)
        before=self.fingerprint()
        with patch.object(reconciliation,'_save',side_effect=OSError('record disk failure')):
            with self.assertRaisesRegex(OSError,'record disk failure'):
                self.apply(journal)
        self.assertEqual(self.fingerprint(),before)
        self.assertIsNone(self.inspect(journal)['receipt']['receipt'])

    def test_record_failure_after_commit_keeps_historical_confirmation(self):
        journal=self.derived(fail_commit=True)
        original=reconciliation._save
        def failed(path,data,**kwargs):
            if not kwargs.get('create'):
                raise OSError('final record disk failure')
            return original(path,data,**kwargs)
        with patch.object(reconciliation,'_save',failed):
            result=self.apply(journal)
        self.assertEqual(result['status'],'review_required')
        self.assertTrue(result['native_confirmed'])
        self.assertEqual(json.loads(Path(result['record']).read_bytes())['phase'],'prepared')
        self.assertEqual(self.inspect(journal)['status'],'confirmed_current')

    def test_curation_changed_after_durable_intent_rejects_before_commit(self):
        journal=self.derived(fail_commit=True)
        original=reconciliation._save
        def change(path,data,**kwargs):
            result=original(path,data,**kwargs)
            if kwargs.get('create'):
                self.service.submit('edit_curation',str(self.source),add_tags=['after intent'],rating=4).result()
            return result
        with patch.object(reconciliation,'_save',change):
            result=self.apply(journal)
        self.assertEqual(result['status'],'review_required')
        self.assertFalse(result['native_confirmed'])
        self.assertIn('changed_since_review',result['error'])
        self.assertIsNone(self.inspect(journal)['receipt']['receipt'])

    def test_recovery_root_newly_watched_during_validation_blocks_intent(self):
        journal=self.derived(fail_commit=True)
        original=reconciliation._metadata
        def watch(*args):
            original(*args)
            self.service.submit('add_watched_folder',str(self.root)).result()
        before=set(journal.parent.iterdir())
        with patch.object(reconciliation,'_metadata',watch):
            with self.assertRaisesRegex(ValueError,'inside_library'):
                self.apply(journal)
        self.assertEqual(set(journal.parent.iterdir()),before)
        self.assertIsNone(self.inspect(journal)['receipt']['receipt'])

    def test_cli_manual_reconciliation_uses_existing_ready_native_owner(self):
        journal=self.derived(fail_commit=True)
        token=self.inspect(journal)['review_fingerprint']
        self.service.close()
        output=io.StringIO()
        with redirect_stdout(output):
            code=cli.main(['--db',str(self.db_path),'--journal',str(journal),'--reconcile',
                           '--expected-review-fingerprint',token])
        self.assertEqual(code,0,output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['status'],'complete')
        self.assertEqual(self.inspect(journal)['status'],'confirmed_current')


if __name__=='__main__':
    unittest.main(verbosity=2)
