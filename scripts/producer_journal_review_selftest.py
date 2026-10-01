"""Actual private producer journals, bytes and Native receipts; never a replay."""
from concurrent.futures import Future
from contextlib import closing, redirect_stdout
import hashlib
import json
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image,PngImagePlugin
from PySide6.QtCore import QRectF
from core.library_service import LibraryService
from core.library_connections import open_reader
from core import producer_journal_review as reviews
from modules.cropper.logic.cropper_logic import crop_image
from modules.image_optimizer.logic.optimizer import optimize_image
from modules.metadata.logic.file_tasks import MetadataTasks
from modules.metadata.logic import file_tasks
from scripts import producer_journal as cli


class Review(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.db_path=self.root/'private.db'
        self.source,self.output=self.root/'source ñ.png',self.root/'output ñ.png'
        text=PngImagePlugin.PngInfo()
        text.add_itxt('parameters','forest ñ\nNegative prompt: blur\nSteps: 20, Seed: 42, Model: toy')
        with Image.new('RGB',(32,24),'green') as image:
            image.save(self.source,pnginfo=text)
        self.original=self.source.read_bytes()
        self.service=LibraryService(self.db_path)
        self.addCleanup(self.service.close)
        self.service.submit('register_files_minimal',[dict(path=str(self.source),filename=self.source.name,
            size=len(self.original),created=self.source.stat().st_ctime,
            hash_original=hashlib.sha256(self.original).hexdigest(),origin_url='Cherry receipt')]).result()
        self.service.submit('edit_curation',str(self.source),add_tags=['curada'],rating=5).result()
        self.snapshot=self.service.read('get_curation_snapshot',[str(self.source)],include_revision=True)[0]
        self.events=threading.Event()

    def derived(self,producer='cropper',fail_commit=False):
        self.service.submit('prepare_derived_copies').result()
        options=dict(source_snapshot=self.snapshot,native_service=self.service,journal_root=self.root/'derived')
        submit=self.service.submit
        def fail(operation,*args,**kwargs):
            if operation=='register_derived_copy':
                reply=Future()
                reply.set_exception(RuntimeError('private commit failure'))
                return reply
            return submit(operation,*args,**kwargs)
        with patch.object(self.service,'submit',fail if fail_commit else submit):
            if producer=='cropper':
                result=crop_image(self.source,QRectF(0,0,.5,.5),self.output,return_details=True,**options)
            else:
                result=optimize_image(self.source,self.output,**options)
        self.assertTrue(result['published'],result)
        return Path(result['journal'])

    def metadata(self,export=False):
        tasks=MetadataTasks(self.service,self.root/'metadata')
        captured=tasks.load(str(self.source),self.events)
        result=tasks.edit(captured,'new ñ prompt','new bad',destination=self.output if export else None)
        self.assertEqual(result['status'],'complete',result)
        return Path(result['journal'])

    def inspect(self,path):
        return reviews.review(self.db_path,path)

    def rewrite(self,path,change):
        data=json.loads(path.read_bytes())
        change(data)
        path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')

    def fingerprint(self):
        with closing(open_reader(self.db_path)) as conn:
            return conn.execute('SELECT name,type,sql FROM sqlite_master ORDER BY name').fetchall(),conn.execute('SELECT * FROM files').fetchall()

    def test_cropper_confirms_current_bytes_identity_generation_and_receipt(self):
        journal=self.derived()
        report=self.inspect(journal)
        self.assertEqual(report['status'],'confirmed_current')
        self.assertTrue(report['stable'])
        self.assertTrue(report['receipt_matches_request'])
        self.assertTrue(report['current_generation_matches'])
        self.assertTrue(report['source_revision_matches'])
        self.assertNotEqual(report['source']['copy_id'],report['output']['copy_id'])
        self.assertEqual(report['output']['tag_count'],0)
        self.assertLess(len(json.dumps(report).encode()),32*1024)

    def test_optimizer_uses_its_own_receipt(self):
        journal=self.derived('optimizer')
        report=self.inspect(journal)
        self.assertEqual(report['status'],'confirmed_current')
        self.assertEqual(report['receipt']['receipt']['producer'],'image_optimizer')

    def test_metadata_edit_confirms_backup_and_same_native_identity(self):
        journal=self.metadata()
        report=self.inspect(journal)
        self.assertEqual(report['status'],'confirmed_current')
        self.assertTrue(report['backup_matches_source'])
        self.assertFalse(report['source_bytes_match'])
        self.assertEqual(report['source']['copy_id'],report['output']['copy_id'])
        self.assertEqual(report['output']['tag_count'],1)

    def test_metadata_export_is_verified_without_claiming_native_commit(self):
        journal=self.metadata(export=True)
        report=self.inspect(journal)
        self.assertEqual(report['status'],'export_bytes_verified')
        self.assertIsNone(report['receipt']['receipt'])
        self.assertIsNone(report['output'])
        self.assertTrue(report['backup_matches_source'])
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_review_does_not_write_schema_database_journal_or_files(self):
        journal=self.derived()
        before=self.fingerprint()
        files={path:path.read_bytes() for path in (journal,self.source,self.output)}
        with patch.object(self.service,'submit',side_effect=AssertionError('review wrote Native')):
            report=self.inspect(journal)
        self.assertEqual(self.fingerprint(),before)
        self.assertEqual({path:path.read_bytes() for path in files},files)
        self.assertEqual(report['status'],'confirmed_current')

    def test_published_output_without_receipt_is_not_replayed(self):
        journal=self.derived(fail_commit=True)
        before=self.fingerprint()
        report=self.inspect(journal)
        self.assertEqual(report['status'],'published_bytes_without_receipt')
        self.assertTrue(report['result_bytes_match'])
        self.assertFalse(report['receipt_matches_request'])
        self.assertEqual(self.fingerprint(),before)
        self.assertTrue(self.output.exists())

    def test_lost_final_journal_flag_does_not_hide_historical_commit(self):
        journal=self.derived()
        self.rewrite(journal,lambda data:data.update(phase='published',native_confirmation='unconfirmed',receipt=None))
        report=self.inspect(journal)
        self.assertEqual(report['status'],'confirmed_current')
        self.assertTrue(report['receipt_matches_request'])

    def test_matching_receipt_does_not_hide_changed_physical_output(self):
        journal=self.derived()
        self.output.write_bytes(b'externally changed')
        report=self.inspect(journal)
        self.assertEqual(report['status'],'confirmed_history_current_changed')
        self.assertTrue(report['receipt_matches_request'])
        self.assertFalse(report['result_bytes_match'])

    def test_matching_receipt_does_not_hide_missing_output(self):
        journal=self.derived()
        self.output.unlink()
        report=self.inspect(journal)
        self.assertEqual(report['status'],'confirmed_history_current_changed')
        self.assertEqual(report['files']['output']['status'],'missing')

    def test_receipt_with_offline_output_is_historical_only(self):
        journal=self.derived()
        receipt=self.service.read('get_derived_copy_receipt',json.loads(journal.read_bytes())['operation_id'])['receipt']
        current=self.service.read('get_copy_version_state',receipt['copy_id'])
        self.service.submit('archive_copy',receipt['copy_id'],expected_fingerprint=current['fingerprint']).result()
        report=self.inspect(journal)
        self.assertEqual(report['status'],'confirmed_history_current_changed')
        self.assertEqual(report['output']['is_offline'],1)
        self.assertTrue(self.output.exists())

    def test_later_output_curation_is_reported_and_preserved(self):
        journal=self.derived()
        first=self.inspect(journal)
        self.service.submit('edit_curation',str(self.output.resolve()),add_tags=['later review'],rating=4).result()
        second=self.inspect(journal)
        self.assertEqual(second['status'],'confirmed_current')
        self.assertEqual(second['output']['rating'],4)
        self.assertEqual(second['output']['tag_count'],1)
        self.assertNotEqual(second['review_fingerprint'],first['review_fingerprint'])
        self.assertNotEqual(second['output']['fingerprint'],first['output']['fingerprint'])

    def test_later_metadata_edit_does_not_restore_old_generation(self):
        journal=self.metadata()
        tasks=MetadataTasks(self.service,self.root/'metadata')
        captured=tasks.load(str(self.source),self.events)
        result=tasks.edit(captured,'later generation','later bad')
        self.assertEqual(result['status'],'complete')
        before=self.service.read('get_copy_version_state',self.snapshot['id'])
        report=self.inspect(journal)
        self.assertEqual(report['status'],'confirmed_history_current_changed')
        self.assertFalse(report['current_generation_matches'])
        self.assertEqual(self.service.read('get_copy_version_state',self.snapshot['id']),before)

    def test_changed_backup_is_visible_and_never_repaired(self):
        journal=self.metadata()
        backup=Path(json.loads(journal.read_bytes())['backup'])
        backup.write_bytes(b'changed backup')
        report=self.inspect(journal)
        self.assertFalse(report['backup_matches_source'])
        self.assertEqual(backup.read_bytes(),b'changed backup')

    def test_native_revision_changed_while_hashing_marks_review_unstable(self):
        journal=self.derived()
        original=reviews._file
        changed=[]
        def read(*args,**kwargs):
            result=original(*args,**kwargs)
            if not changed:
                changed.append(True)
                self.service.submit('edit_curation',str(self.output.resolve()),add_tags=['concurrent'],rating=2).result()
            return result
        with patch.object(reviews,'_file',read):
            report=self.inspect(journal)
        self.assertFalse(report['stable'])
        self.assertEqual(report['status'],'changed_during_review')

    def test_file_changed_after_hashing_marks_review_unstable(self):
        journal=self.derived()
        original=reviews._native
        calls=[]
        def read(*args,**kwargs):
            result=original(*args,**kwargs)
            calls.append(True)
            if len(calls)==2:
                self.output.write_bytes(b'changed after hash')
            return result
        with patch.object(reviews,'_native',read):
            report=self.inspect(journal)
        self.assertFalse(report['stable'])
        self.assertEqual(report['status'],'changed_during_review')

    def test_journal_changed_after_hashing_marks_review_unstable(self):
        journal=self.derived()
        original=reviews._native
        calls=[]
        def read(*args,**kwargs):
            result=original(*args,**kwargs)
            calls.append(True)
            if len(calls)==2:
                self.rewrite(journal,lambda data:data.update(error='concurrent journal write'))
            return result
        with patch.object(reviews,'_native',read):
            report=self.inspect(journal)
        self.assertFalse(report['stable'])

    def test_no_native_read_transaction_survives_into_hashing(self):
        journal=self.derived()
        connections=[]
        original=reviews.open_reader
        def opened(*args):
            conn=original(*args)
            connections.append(conn)
            return conn
        original_file=reviews._file
        def read(*args,**kwargs):
            self.assertTrue(connections)
            for conn in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute('SELECT 1')
            return original_file(*args,**kwargs)
        with patch.object(reviews,'open_reader',opened),patch.object(reviews,'_file',read):
            self.assertEqual(self.inspect(journal)['status'],'confirmed_current')

    def test_unknown_version_rejected_before_native_or_file_reads(self):
        journal=self.derived()
        self.rewrite(journal,lambda data:data.update(version=2))
        with patch.object(reviews,'_native',side_effect=AssertionError('unknown journal touched Native')):
            with self.assertRaisesRegex(ValueError,'invalid_header'):
                self.inspect(journal)

    def test_duplicate_json_key_rejected(self):
        journal=self.derived()
        journal.write_text(journal.read_text(encoding='utf-8')[:-1]+',"version":1}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'duplicate_key'):
            self.inspect(journal)

    def test_nonfinite_json_rejected(self):
        journal=self.derived()
        journal.write_text(journal.read_text(encoding='utf-8')[:-1]+',"unknown":NaN}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'nonfinite'):
            self.inspect(journal)

    def test_oversized_journal_rejected_before_decode(self):
        journal=self.derived()
        with patch.object(reviews,'MAX_JOURNAL',10):
            with self.assertRaisesRegex(ValueError,'scope_or_size'):
                self.inspect(journal)

    def test_request_conflict_with_historical_receipt_is_explicit(self):
        journal=self.derived()
        self.rewrite(journal,lambda data:data['request']['generation'].update(positive='tampered request'))
        report=self.inspect(journal)
        self.assertEqual(report['status'],'receipt_conflict')
        self.assertFalse(report['receipt_matches_request'])

    def test_operation_folder_mismatch_is_rejected(self):
        journal=self.derived()
        other=journal.parent.parent/'wrong'
        other.mkdir()
        path=other/'journal.json'
        path.write_bytes(journal.read_bytes())
        with self.assertRaisesRegex(ValueError,'operation_scope'):
            self.inspect(path)

    def test_backup_outside_operation_folder_is_rejected(self):
        journal=self.metadata()
        self.rewrite(journal,lambda data:data.update(backup=str(self.root/'unrelated.png')))
        with self.assertRaisesRegex(ValueError,'backup_scope'):
            self.inspect(journal)

    def test_cancelled_review_never_touches_native_or_files(self):
        journal=self.derived()
        self.events.set()
        with patch.object(reviews,'_native',side_effect=AssertionError('cancelled Native read')):
            with self.assertRaisesRegex(ValueError,'cancelled'):
                reviews.review(self.db_path,journal,event=self.events)

    def test_missing_database_is_not_created(self):
        journal=self.derived()
        missing=self.root/'missing.db'
        with self.assertRaises(FileNotFoundError):
            reviews.review(missing,journal)
        self.assertFalse(missing.exists())

    def test_source_output_path_is_hashed_only_once_for_metadata_edit(self):
        journal=self.metadata()
        original=reviews._file
        paths=[]
        def read(path,*args):
            paths.append(path)
            return original(path,*args)
        with patch.object(reviews,'_file',read):
            report=self.inspect(journal)
        self.assertEqual(paths.count(str(self.source.resolve())),1)
        self.assertEqual(report['status'],'confirmed_current')

    def test_failed_preparation_can_be_reviewed_without_preparing_schema(self):
        tasks=MetadataTasks(self.service,self.root/'metadata')
        captured=tasks.load(str(self.source),self.events)
        with patch.object(file_tasks,'modify_metadata',return_value=(False,'private transform failed')):
            result=tasks.edit(captured,'new','bad')
        self.assertEqual(result['status'],'error')
        self.assertFalse(self.service.read('get_generation_history_status')['prepared'])
        before=self.fingerprint()
        report=self.inspect(result['journal'])
        self.assertEqual(report['status'],'incomplete_preparation')
        self.assertTrue(report['backup_matches_source'])
        self.assertEqual(self.fingerprint(),before)
        self.assertFalse(self.service.read('get_generation_history_status')['prepared'])

    def test_source_hash_change_is_reported_separately_from_valid_derived_output(self):
        journal=self.derived()
        self.source.write_bytes(self.original+b'changed parent')
        report=self.inspect(journal)
        self.assertFalse(report['source_bytes_match'])
        self.assertTrue(report['result_bytes_match'])
        self.assertEqual(report['status'],'confirmed_current')

    def test_missing_output_appearing_after_snapshot_makes_review_unstable(self):
        journal=self.derived()
        saved=self.output.read_bytes()
        self.output.unlink()
        original=reviews._native
        calls=[]
        def read(*args,**kwargs):
            result=original(*args,**kwargs)
            calls.append(True)
            if len(calls)==2:
                self.output.write_bytes(saved)
            return result
        with patch.object(reviews,'_native',read):
            report=self.inspect(journal)
        self.assertFalse(report['stable'])
        self.assertEqual(report['status'],'changed_during_review')

    def test_file_size_limit_is_explicit_without_approving_partial_hash(self):
        journal=self.derived()
        with patch.object(reviews,'MAX_FILE',10):
            report=self.inspect(journal)
        self.assertEqual(report['files']['output']['status'],'unverified')
        self.assertFalse(report['result_bytes_match'])
        self.assertEqual(report['status'],'confirmed_history_current_changed')

    def test_cancel_during_hashing_stops_before_second_snapshot(self):
        journal=self.derived()
        original=reviews._file
        def read(*args,**kwargs):
            self.events.set()
            return original(*args,**kwargs)
        with patch.object(reviews,'_file',read):
            with self.assertRaisesRegex(ValueError,'cancelled'):
                reviews.review(self.db_path,journal,event=self.events)

    def test_budget_and_invalid_budget_are_explicit(self):
        journal=self.derived()
        with self.assertRaisesRegex(ValueError,'budget'):
            reviews.review(self.db_path,journal,budget_seconds=.000000001)
        for value in (0,301,float('nan'),float('inf'),True,'60'):
            with self.assertRaisesRegex(ValueError,'invalid_budget'):
                reviews.review(self.db_path,journal,budget_seconds=value)

    def test_relative_input_path_in_journal_is_rejected(self):
        journal=self.metadata()
        self.rewrite(journal,lambda data:data.update(source='relative.png'))
        with self.assertRaisesRegex(ValueError,'invalid_path'):
            self.inspect(journal)

    def test_edit_source_and_destination_cannot_be_redirected_independently(self):
        journal=self.metadata()
        self.rewrite(journal,lambda data:data.update(destination=str(self.root/'other.png')))
        with self.assertRaisesRegex(ValueError,'edit_path_conflict'):
            self.inspect(journal)

    def test_cli_emits_one_bounded_json_report_without_mutating_journal(self):
        journal=self.derived()
        before=journal.read_bytes()
        output=io.StringIO()
        with redirect_stdout(output):
            code=cli.main(['--db',str(self.db_path),'--journal',str(journal)])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(output.getvalue())['status'],'confirmed_current')
        self.assertEqual(journal.read_bytes(),before)

    def test_cli_read_failure_is_json_and_does_not_create_database(self):
        journal=self.derived()
        missing=self.root/'absent.db'
        output=io.StringIO()
        with redirect_stdout(output):
            code=cli.main(['--db',str(missing),'--journal',str(journal)])
        self.assertEqual(code,2)
        self.assertEqual(json.loads(output.getvalue())['status'],'review_failed')
        self.assertFalse(missing.exists())


if __name__=='__main__':
    unittest.main(verbosity=2)
