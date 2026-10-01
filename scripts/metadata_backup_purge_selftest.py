"""Real Windows handles and private producer files; never uses an actual collection."""
from concurrent.futures import Future
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import metadata_backup_purge as purge
from core.library_connections import open_reader
from core.generation_history import NAME,MAX_RECORD
from core.pinned_file import PinnedFile
from modules.metadata.logic.file_tasks import MetadataTasks
from scripts import producer_journal_review_selftest as fixtures
from scripts import producer_reconciliation_selftest as pending


class Backups(unittest.TestCase):
    setUp=fixtures.Review.setUp
    metadata=fixtures.Review.metadata
    derived=fixtures.Review.derived
    inspect=fixtures.Review.inspect
    rewrite=fixtures.Review.rewrite
    fingerprint=fixtures.Review.fingerprint
    pending_metadata=pending.Reconciliation.pending_metadata

    def evidence(self,journal):
        return purge.preview(self.service,journal,
            expected_review_fingerprint=self.inspect(journal)['review_fingerprint'])

    def apply(self,journal,evidence=None,**kwargs):
        evidence=evidence or self.evidence(journal)
        return purge.purge(self.service,journal,
            expected_review_fingerprint=evidence['evidence']['review_fingerprint'],
            expected_preview_fingerprint=evidence['preview_fingerprint'],confirmed=True,**kwargs)

    def mutate(self,sql,params=()):
        import sqlite3
        # Connection's transaction context commits but does not close Windows handles.
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(sql,params)

    def test_preview_is_read_only_and_purge_deletes_only_one_private_backup(self):
        journal=self.metadata()
        backup=Path(json.loads(journal.read_bytes())['backup'])
        original=self.source.read_bytes(),journal.read_bytes(),self.fingerprint()
        report=self.inspect(journal)
        evidence=self.evidence(journal)
        self.assertEqual((self.source.read_bytes(),journal.read_bytes(),self.fingerprint()),original)
        self.assertTrue(backup.exists())
        self.assertFalse(list(journal.parent.glob('backup-purge-*.json')))
        result=self.apply(journal,evidence)
        self.assertEqual(result['status'],'complete',result)
        self.assertTrue(result['approved'])
        self.assertTrue(result['delete_marked'])
        self.assertTrue(result['deleted'])
        self.assertFalse(backup.exists())
        self.assertEqual((self.source.read_bytes(),journal.read_bytes(),self.fingerprint()),original)
        self.assertEqual(self.inspect(journal)['receipt'],report['receipt'])
        self.assertEqual(self.inspect(journal)['output']['rating'],5)
        self.assertEqual(len(self.service.read('get_generation_history_page',self.snapshot['id'])['records']),1)
        record=json.loads(Path(result['record']).read_bytes())
        self.assertEqual(record['phase'],'complete')
        self.assertFalse((journal.parent/'producer-reconcile.lock').exists())

    def test_older_operation_can_purge_its_backup_after_verified_later_edit(self):
        journal=self.metadata()
        tasks=MetadataTasks(self.service,self.root/'metadata')
        captured=tasks.load(str(self.source),self.events)
        later=tasks.edit(captured,'later generation','later bad')
        self.assertEqual(later['status'],'complete',later)
        later_backup=Path(json.loads(Path(later['journal']).read_bytes())['backup'])
        before=self.source.read_bytes(),self.fingerprint(),later_backup.read_bytes()
        self.assertEqual(self.inspect(journal)['status'],'confirmed_history_current_changed')
        result=self.apply(journal)
        self.assertEqual(result['status'],'complete',result)
        self.assertEqual((self.source.read_bytes(),self.fingerprint(),later_backup.read_bytes()),before)
        self.assertEqual(len(self.service.read('get_generation_history_page',self.snapshot['id'])['records']),2)

    def test_curation_after_original_edit_is_preserved(self):
        journal=self.metadata()
        self.service.submit('edit_curation',self.snapshot['path'],add_tags=['later'],rating=4).result()
        result=self.apply(journal)
        self.assertEqual(result['status'],'complete',result)
        report=self.inspect(journal)
        self.assertEqual(report['output']['rating'],4)
        self.assertEqual(report['output']['tag_count'],2)

    def test_confirmation_is_explicit_and_bound_to_one_preview(self):
        journal=self.metadata()
        evidence=self.evidence(journal)
        for confirmation in (False,None,1,'yes'):
            with self.subTest(confirmation=confirmation), self.assertRaisesRegex(ValueError,'confirmation_required'):
                purge.purge(self.service,journal,expected_review_fingerprint=evidence['evidence']['review_fingerprint'],
                    expected_preview_fingerprint=evidence['preview_fingerprint'],confirmed=confirmation)
        with self.assertRaisesRegex(ValueError,'preview_changed'):
            purge.purge(self.service,journal,expected_review_fingerprint=evidence['evidence']['review_fingerprint'],
                expected_preview_fingerprint='0'*64,confirmed=True)
        self.assertFalse(list(journal.parent.glob('backup-purge-*.json')))

    def test_changed_source_revision_after_preview_rejects_before_delete(self):
        journal=self.metadata()
        evidence=self.evidence(journal)
        self.service.submit('edit_curation',self.snapshot['path'],rating=4).result()
        with self.assertRaisesRegex(ValueError,'review_changed'):
            self.apply(journal,evidence)
        self.assertTrue(Path(evidence['evidence']['backup']).exists())

    def test_changed_backup_after_preview_rejects_before_delete(self):
        journal=self.metadata()
        evidence=self.evidence(journal)
        Path(evidence['evidence']['backup']).write_bytes(b'changed backup')
        with self.assertRaisesRegex(ValueError,'review_changed'):
            self.apply(journal,evidence)
        self.assertTrue(Path(evidence['evidence']['backup']).exists())

    def test_changed_current_bytes_cannot_release_old_backup(self):
        journal=self.metadata()
        self.source.write_bytes(b'externally changed')
        with self.assertRaisesRegex(ValueError,'current_copy_changed'):
            self.evidence(journal)

    def test_pending_metadata_without_receipt_keeps_its_recovery_backup(self):
        journal=self.pending_metadata()
        with self.assertRaisesRegex(ValueError,'confirmed_history_and_backup_required'):
            self.evidence(journal)
        self.assertTrue(Path(json.loads(journal.read_bytes())['backup']).exists())

    def test_exports_and_derived_journals_are_not_original_backup_purge(self):
        journal=self.metadata(export=True)
        with self.assertRaisesRegex(ValueError,'original_edit_required'):
            self.evidence(journal)
        self.output=self.root/'derived-output.png'
        other=self.derived()
        with self.assertRaisesRegex(ValueError,'original_edit_required'):
            self.evidence(other)

    def test_offline_original_is_not_a_verified_current_keeper(self):
        journal=self.metadata()
        current=self.service.read('get_copy_version_state',self.snapshot['id'])
        self.service.submit('archive_copy',self.snapshot['id'],expected_fingerprint=current['fingerprint']).result()
        with self.assertRaisesRegex(ValueError,'current_copy_requires_review'):
            self.evidence(journal)

    def test_corrupted_history_metadata_is_not_only_receipt_acknowledgement(self):
        journal=self.metadata()
        job=json.loads(journal.read_bytes())['operation_id']
        record=self.service.read('get_metadata_edit_history_record',job)['record']
        record['previous']['observed']['positive']='forged history'
        self.mutate(f'UPDATE {NAME} SET previous_json=? WHERE operation_id=?',
                    (json.dumps(record['previous']),job))
        with self.assertRaisesRegex(ValueError,'history_requires_review'):
            self.evidence(journal)

    def test_historical_record_bound_checks_invalid_json_and_oversized_blob(self):
        journal=self.metadata()
        job=json.loads(journal.read_bytes())['operation_id']
        for value,reason in [('x'*(MAX_RECORD+1),'payload_limit'),
                             ('{"native":{},"native":{},"observed":{}}','invalid_previous'),
                             ('{"native":{},"observed":{"positive":NaN}}','invalid_previous')]:
            with self.subTest(reason=reason):
                self.mutate(f'UPDATE {NAME} SET previous_json=? WHERE operation_id=?',(value,job))
                with self.assertRaisesRegex(ValueError,reason):
                    self.service.read('get_metadata_edit_history_record',job)

    def test_backup_with_native_history_or_external_documentation_is_protected(self):
        journal=self.metadata()
        backup=Path(json.loads(journal.read_bytes())['backup'])
        self.service.submit('register_files_minimal',[dict(path=str(backup),filename=backup.name,
            size=backup.stat().st_size,created=backup.stat().st_ctime)]).result()
        with self.assertRaisesRegex(ValueError,'backup_has_native_identity'):
            self.evidence(journal)

    def test_documented_backup_is_protected(self):
        journal=self.metadata()
        backup=Path(json.loads(journal.read_bytes())['backup'])
        backup.with_suffix('.txt').write_text('private caption')
        with self.assertRaisesRegex(ValueError,'documented_backup'):
            self.evidence(journal)

    def test_new_watched_recovery_folder_or_portable_catalog_blocks_purge(self):
        journal=self.metadata()
        self.service.submit('add_watched_folder',str(journal.parent.resolve())).result()
        with self.assertRaisesRegex(ValueError,'inside_library'):
            self.evidence(journal)

    def test_portable_catalog_blocks_even_reading_its_backup_for_purge(self):
        journal=self.metadata()
        (journal.parent/'catalog.db').write_bytes(b'protected catalog')
        with self.assertRaisesRegex(ValueError,'cherry_original_protected'):
            self.evidence(journal)

    def test_hardlink_backup_is_not_a_deletable_victim(self):
        journal=self.metadata()
        backup=Path(json.loads(journal.read_bytes())['backup'])
        alias=self.root/'backup-alias.png'
        os.link(backup,alias)
        with self.assertRaisesRegex(ValueError,'handle_identity_changed'):
            self.evidence(journal)
        self.assertTrue(alias.exists())

    def test_cancel_before_preview_or_delete_keeps_backup(self):
        journal=self.metadata()
        event=threading.Event();event.set()
        token=self.inspect(journal)['review_fingerprint']
        with self.assertRaisesRegex(ValueError,'cancelled'):
            purge.preview(self.service,journal,expected_review_fingerprint=token,event=event)
        self.assertTrue(Path(json.loads(journal.read_bytes())['backup']).exists())

    def test_intent_write_failure_has_no_database_admission_or_delete(self):
        journal=self.metadata()
        evidence=self.evidence(journal)
        with patch.object(purge,'_save',side_effect=OSError('private disk failure')), \
                patch.object(self.service,'submit',side_effect=AssertionError('admission before intent')):
            result=self.apply(journal,evidence)
        self.assertFalse(result['deleted'])
        self.assertFalse(result['approved'])
        self.assertTrue(Path(evidence['evidence']['backup']).exists())

    def test_native_approval_failure_or_lost_reply_never_deletes_backup(self):
        journal=self.metadata()
        evidence=self.evidence(journal)
        submit=self.service.submit
        writes=[]
        def lose_reply(operation,*args,**kwargs):
            writes.append(operation)
            submit(operation,*args,**kwargs).result()
            future=Future();future.set_exception(RuntimeError('private lost approval reply'))
            return future
        with patch.object(self.service,'submit',lose_reply):
            result=self.apply(journal,evidence)
        self.assertEqual(writes,['approve_metadata_backup_purge'])
        self.assertFalse(result['approved'])
        self.assertFalse(result['deleted'])
        self.assertTrue(Path(evidence['evidence']['backup']).exists())

    def test_curation_after_durable_intent_is_rejected_by_native_owner(self):
        journal=self.metadata()
        evidence=self.evidence(journal)
        save=purge._save
        def changed(path,data,**kwargs):
            save(path,data,**kwargs)
            if data['phase']=='prepared':
                self.service.submit('edit_curation',self.snapshot['path'],rating=4).result()
        with patch.object(purge,'_save',changed):
            result=self.apply(journal,evidence)
        self.assertFalse(result['approved'])
        self.assertFalse(result['deleted'])
        self.assertTrue(Path(evidence['evidence']['backup']).exists())

    def test_history_after_durable_intent_is_rejected_by_native_owner(self):
        journal=self.metadata()
        evidence=self.evidence(journal)
        save=purge._save
        def changed(path,data,**kwargs):
            save(path,data,**kwargs)
            if data['phase']=='prepared':
                self.mutate(f'UPDATE {NAME} SET recorded_at=? WHERE operation_id=?',
                    ('private changed record',data['evidence']['operation_id']))
        with patch.object(purge,'_save',changed):
            result=self.apply(journal,evidence)
        self.assertFalse(result['approved'])
        self.assertFalse(result['deleted'])

    def test_cancel_after_accepted_approval_prevents_deletion(self):
        journal=self.metadata()
        evidence=self.evidence(journal)
        event=threading.Event()
        submit=self.service.submit
        def cancel_after(operation,*args,**kwargs):
            future=submit(operation,*args,**kwargs)
            future.result()
            event.set()
            return future
        with patch.object(self.service,'submit',cancel_after):
            result=self.apply(journal,evidence,event=event)
        self.assertTrue(result['approved'])
        self.assertFalse(result['delete_marked'])
        self.assertFalse(result['deleted'])

    def test_delete_failure_keeps_backup_and_separate_review_record(self):
        journal=self.metadata()
        with patch.object(PinnedFile,'mark_delete',side_effect=OSError('private delete failure')):
            result=self.apply(journal)
        self.assertTrue(result['approved'])
        self.assertFalse(result['deleted'])
        self.assertTrue(Path(json.loads(journal.read_bytes())['backup']).exists())
        self.assertEqual(json.loads(Path(result['record']).read_bytes())['phase'],'review_required')

    def test_failed_final_record_reports_actual_deletion_honestly(self):
        journal=self.metadata()
        save=purge._save
        def fail_last(path,data,**kwargs):
            if data['phase'] in ('complete','review_required'):
                raise OSError('private final evidence failure')
            return save(path,data,**kwargs)
        with patch.object(purge,'_save',fail_last):
            result=self.apply(journal)
        self.assertEqual(result['status'],'review_required')
        self.assertTrue(result['deleted'])
        self.assertTrue(result['delete_marked'])
        self.assertFalse(Path(json.loads(journal.read_bytes())['backup']).exists())
        self.assertEqual(json.loads(Path(result['record']).read_bytes())['phase'],'approved')

    def test_cancellation_after_delete_mark_finishes_evidence_without_rollback_claim(self):
        journal=self.metadata()
        event=threading.Event()
        mark=PinnedFile.mark_delete
        def cancel_mark(worker):
            mark(worker)
            event.set()
        with patch.object(PinnedFile,'mark_delete',cancel_mark):
            result=self.apply(journal,event=event)
        self.assertEqual(result['status'],'complete',result)
        self.assertTrue(result['deleted'])

    def test_exclusive_victim_and_keeper_handles_block_external_replace(self):
        journal=self.metadata()
        save=purge._save
        blocked=[]
        def try_replace(path,data,**kwargs):
            save(path,data,**kwargs)
            if data['phase']=='approved':
                for key in ('backup','current'):
                    replacement=self.root/('replacement-'+key+'.png')
                    replacement.write_bytes(b'foreign bytes')
                    try:
                        os.replace(replacement,data['evidence'][key])
                    except PermissionError:
                        blocked.append(key)
                    else:
                        raise AssertionError('protected handle permitted replacement')
        with patch.object(purge,'_save',try_replace):
            result=self.apply(journal)
        self.assertEqual(result['status'],'complete',result)
        self.assertEqual(blocked,['backup','current'])

    def test_existing_guard_remains_for_manual_review(self):
        journal=self.metadata()
        evidence=self.evidence(journal)
        guard=journal.parent/'producer-reconcile.lock'
        guard.write_bytes(b'prior uncertain action')
        with self.assertRaisesRegex(ValueError,'lock_requires_review'):
            self.apply(journal,evidence)
        self.assertEqual(guard.read_bytes(),b'prior uncertain action')

    def test_clear_metadata_can_purge_verified_backup(self):
        tasks=MetadataTasks(self.service,self.root/'metadata')
        captured=tasks.load(str(self.source),self.events)
        edited=tasks.edit(captured,'','')
        self.assertEqual(edited['status'],'complete',edited)
        result=self.apply(Path(edited['journal']))
        self.assertEqual(result['status'],'complete',result)
        current=self.service.read('get_copy_version_state',self.snapshot['id'])['state']['file']
        self.assertEqual(current['meta_positive'],'')
        self.assertEqual(current['rating'],5)

    def test_native_generation_inconsistent_with_current_file_cannot_release_backup(self):
        journal=self.metadata()
        self.mutate('UPDATE files SET meta_positive=? WHERE id=?',('foreign prompt',self.snapshot['id']))
        with self.assertRaisesRegex(ValueError,'current_generation_changed'):
            self.evidence(journal)

    def test_new_watched_scope_after_approval_prevents_deletion(self):
        journal=self.metadata()
        save=purge._save
        def watched(path,data,**kwargs):
            save(path,data,**kwargs)
            if data['phase']=='approved':
                self.service.submit('add_watched_folder',str(journal.parent.resolve())).result()
        with patch.object(purge,'_save',watched):
            result=self.apply(journal)
        self.assertTrue(result['approved'])
        self.assertFalse(result['deleted'])
        self.assertTrue(Path(json.loads(journal.read_bytes())['backup']).exists())

    def test_journal_change_after_approval_prevents_deletion(self):
        journal=self.metadata()
        save=purge._save
        def changed(path,data,**kwargs):
            save(path,data,**kwargs)
            if data['phase']=='approved':
                self.rewrite(journal,lambda value:value.update(phase='published'))
        with patch.object(purge,'_save',changed):
            result=self.apply(journal)
        self.assertTrue(result['approved'])
        self.assertFalse(result['deleted'])
        self.assertIn('journal_changed',result['error'])

    def test_curation_change_after_approval_prevents_deletion(self):
        journal=self.metadata()
        save=purge._save
        def changed(path,data,**kwargs):
            save(path,data,**kwargs)
            if data['phase']=='approved':
                self.service.submit('edit_curation',self.snapshot['path'],rating=3).result()
        with patch.object(purge,'_save',changed):
            result=self.apply(journal)
        self.assertTrue(result['approved'])
        self.assertFalse(result['deleted'])
        self.assertIn('native_changed',result['error'])

    def test_immutable_phase_evidence_never_overwrites_an_existing_record(self):
        journal=self.metadata()
        save=purge._save
        occupied=[]
        def occupy(path,data,**kwargs):
            if data['phase']=='approved':
                path.write_bytes(b'foreign evidence')
                occupied.append(path)
            return save(path,data,**kwargs)
        with patch.object(purge,'_save',occupy):
            result=self.apply(journal)
        self.assertFalse(result['deleted'])
        self.assertEqual(len(occupied),1)
        self.assertEqual(occupied[0].read_bytes(),b'foreign evidence')

    def test_cancellation_of_queued_approval_has_no_delete(self):
        journal=self.metadata()
        event=threading.Event()
        queued=Future()
        def admit(*args,**kwargs):
            event.set()
            return queued
        with patch.object(self.service,'submit',admit):
            result=self.apply(journal,event=event)
        self.assertTrue(queued.cancelled())
        self.assertFalse(result['approved'])
        self.assertFalse(result['deleted'])

    def test_running_approval_is_drained_then_cancelled_before_delete(self):
        journal=self.metadata()
        event=threading.Event()
        future=Future();self.assertTrue(future.set_running_or_notify_cancel())
        entered,release=threading.Event(),threading.Event()
        submit=self.service.submit
        threads=[]
        def admit(operation,*args,**kwargs):
            def run():
                entered.set()
                release.wait(3)
                try:future.set_result(submit(operation,*args,**kwargs).result())
                except Exception as error:future.set_exception(error)
            thread=threading.Thread(target=run);threads.append(thread);thread.start()
            event.set()
            release.set()
            return future
        try:
            with patch.object(self.service,'submit',admit):
                result=self.apply(journal,event=event)
        finally:
            release.set()
            for thread in threads:thread.join(3)
        self.assertTrue(entered.is_set())
        self.assertTrue(future.done())
        self.assertTrue(result['approved'])
        self.assertFalse(result['deleted'])

    def test_already_purged_backup_is_not_deleted_by_path_again(self):
        journal=self.metadata()
        evidence=self.evidence(journal)
        result=self.apply(journal,evidence)
        self.assertTrue(result['deleted'])
        with self.assertRaisesRegex(ValueError,'review_changed'):
            self.apply(journal,evidence)


if __name__=='__main__':
    unittest.main(verbosity=2)
