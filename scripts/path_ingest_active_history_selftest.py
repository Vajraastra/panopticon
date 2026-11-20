"""Real private Native/Windows proof checks for active+offline history."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import os
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import library_ingest_review_selftest as helpers
from core.ingest_review import encode
from core import library_path_ingest as PRIVATE_INGEST
from core.library_repository import LibraryRepository


class ActiveHistoryRepository(LibraryRepository):
    def test_private_history_write(self,records):return PRIVATE_INGEST.write(self,records)
    def test_seed_raw_alias(self,records):return self._register_files_minimal_storage(records)

from core import library_service as native,library_path_ingest


class ActiveHistory(unittest.TestCase):
    repository_class=ActiveHistoryRepository
    state=helpers.IngestReview.state
    sql=helpers.IngestReview.sql
    status=helpers.IngestReview.status
    receipt=helpers.IngestReview.receipt
    write=helpers.IngestReview.write

    def setUp(self):
        helpers.IngestReview.setUp(self)
        self.old_id=self.key
        self.digest=hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.service.submit('make_copy_online',self.old_id,expected_fingerprint=self.state()['fingerprint'],
            digest=self.digest,observation=dict(filename=self.path.name,size=9,created=1)).result(5)
        self.old=self.service.read('get_copy_version_state',self.old_id)
        self.key=self.service.read('get_file_id',str(self.path))
        self.service.submit('edit_curation',str(self.path),rating=4,add_tags=['current'],
            expected_copy_id=self.key,expected_fingerprint=self.state()['fingerprint']).result(5)
        self.data=dict(self.data,hash_original=self.digest,origin_url='fresh://current',
                       origin_site='fixture-current',artist='Current Artist')
        self.service.submit('register_files_minimal',[self.data]).result(5)
        stack=ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.object(native,'WRITES',native.WRITES|{'test_private_history_write','test_seed_raw_alias'}))
        stack.enter_context(patch.object(library_path_ingest,'write',PRIVATE_INGEST.write))
        self.service.submit('prepare_path_identity').result(5);self.service.backfill_path_identity()

    def capture(self,data=None):
        result=[]
        PRIVATE_INGEST.register(self.service,[self.data if data is None else data],threading.Event(),
            commit=lambda records:result.extend(records) or True)
        return result

    def watched(self,data=None):
        service=self.service;folder=self.root.as_posix();operation=str(uuid4())
        class Watched:
            def read(self,*args,**kwargs):return service.read(*args,**kwargs)
            def submit(self,name,records):return service.submit('register_catalog_files_observed',records,folder,operation_id=operation)
        PRIVATE_INGEST.register(Watched(),[self.data if data is None else data],threading.Event())
        return self.receipt(operation)

    def assert_intact(self,before,registry):
        self.assertEqual(self.state(),before)
        self.assertEqual(self.service.read('get_copy_version_state',self.old_id),self.old)
        self.assertEqual(self.sql('SELECT * FROM path_identity_records ORDER BY file_id'),registry)
        self.assertTrue(self.service.read('check_fts')['consistent'])

    def test_unique_active_with_history_keeps_both_ids_curation_and_fts(self):
        before=self.state();registry=self.sql('SELECT * FROM path_identity_records ORDER BY file_id')
        records=self.capture()
        self.assertFalse(records[0]['historical']);self.assertEqual(records[0]['target'][0],self.key)
        result=self.watched()
        self.assertEqual((result['report']['accepted_count'],result['report']['conflict_count']),(1,0))
        self.assert_intact(before,registry)
        self.assertEqual(self.service.read('count_files'),1)
        self.assertEqual(self.sql('SELECT count(*) FROM files'),[(2,)])

    def test_writer_rejects_forged_history_flag_against_current_exact_active_binding(self):
        records=self.capture();records[0]['historical']=True
        before=self.state();registry=self.sql('SELECT * FROM path_identity_records ORDER BY file_id')
        with self.assertRaisesRegex(native.LibraryError,'sql_state_changed'):
            self.service.submit('test_private_history_write',records).result(5)
        self.assert_intact(before,registry)

    def test_writer_rejects_offline_id_instead_of_current_target(self):
        records=self.capture();records[0]['target']=(self.old_id,records[0]['target'][1])
        before=self.state();registry=self.sql('SELECT * FROM path_identity_records ORDER BY file_id')
        with self.assertRaisesRegex(native.LibraryError,'sql_state_changed'):
            self.service.submit('test_private_history_write',records).result(5)
        self.assert_intact(before,registry)

    def test_stale_clock_after_active_retirement_cannot_revive_either_id(self):
        records=self.capture()
        self.service.submit('archive_copy',self.key,expected_fingerprint=self.state()['fingerprint']).result(5)
        before=self.state();registry=self.sql('SELECT * FROM path_identity_records ORDER BY file_id')
        with self.assertRaisesRegex(native.LibraryError,'sql_state_changed|review_required'):
            self.service.submit('test_private_history_write',records).result(5)
        self.assert_intact(before,registry);self.assertEqual(self.service.read('count_files'),0)

    def test_offline_only_is_reviewed_without_admission_and_false_flag_is_rejected(self):
        self.service.submit('archive_copy',self.key,expected_fingerprint=self.state()['fingerprint']).result(5)
        before=self.state();registry=self.sql('SELECT * FROM path_identity_records ORDER BY file_id')
        records=self.capture();self.assertTrue(records[0]['historical']);self.assertIsNone(records[0]['target'])
        records[0]['historical']=False
        with self.assertRaisesRegex(native.LibraryError,'sql_state_changed'):
            self.service.submit('test_private_history_write',records).result(5)
        result=self.watched()
        self.assertEqual(result['report']['accepted_count'],0)
        self.assertEqual(result['report']['conflict_count'],1)
        self.assertEqual(self.service.read('count_files'),0)
        self.assert_intact(before,registry)

    def test_changed_sha_uses_shared_gate_without_rebinding_active_or_history(self):
        before=self.state();registry=self.sql('SELECT * FROM path_identity_records ORDER BY file_id')
        result=self.watched(dict(self.data,hash_original='c'*64))
        self.assertEqual(result['report']['accepted_count'],0)
        self.assertEqual(result['report']['conflict_count'],1)
        self.assertEqual(self.status()['records'][0]['reason'],'catalog_digest_drift')
        self.assert_intact(before,registry)

    def test_first_documented_hash_updates_active_instead_of_being_skipped_by_history(self):
        self.sql('UPDATE files SET hash_original=NULL WHERE id=?',(self.key,))
        before=self.state()
        result=self.watched()
        self.assertEqual(result['report']['accepted_count'],1)
        expected=json.loads(encode(before['state']));expected['file']['hash_original']=self.digest
        self.assertEqual(self.state()['state'],expected)
        self.assertEqual(self.service.read('get_copy_version_state',self.old_id),self.old)
        self.assertTrue(self.service.read('check_fts')['consistent'])

    def test_multiple_active_aliases_block_worker_and_recomputed_writer_even_with_fresh_clock(self):
        records=self.capture()
        alias=dict(self.data,path=str(self.path).upper(),filename=self.path.name.upper())
        self.service.submit('test_seed_raw_alias',[alias]).result(5);self.service.backfill_path_identity()
        before=self.state();registry=self.sql('SELECT * FROM path_identity_records ORDER BY file_id')
        with self.assertRaisesRegex(ValueError,'review_required'):
            self.capture()
        canonical=records[0]['identity']['canonical'];path=records[0]['identity']['path']
        fresh=self.service.read('get_path_identity_ingest_state',[canonical],[path],physicals=[records[0]['identity']['physical']])
        records[0]['clock']=fresh['clock']
        with self.assertRaisesRegex(native.LibraryError,'review_required'):
            self.service.submit('test_private_history_write',records).result(5)
        self.assert_intact(before,registry)

    def test_unavailable_physical_hint_blocks_without_adopting_offline_or_other_id(self):
        other=self.root/'other-link.png';os.link(self.path,other)
        row=dict(self.data,path=str(other),filename=other.name)
        self.service.submit('register_files_minimal',[row]).result(5);self.service.backfill_path_identity()
        other.unlink()
        before=self.state();registry=self.sql('SELECT * FROM path_identity_records ORDER BY file_id')
        with self.assertRaisesRegex(ValueError,'physical_state_changed'):
            self.capture()
        self.assert_intact(before,registry)

    def test_history_with_only_physical_hint_is_refused_by_worker(self):
        self.sql('UPDATE path_identity_records SET canonical=? WHERE file_id=?',
            ((self.root/'stale-canonical.png').as_posix(),self.key))
        before=self.state();registry=self.sql('SELECT * FROM path_identity_records ORDER BY file_id')
        with self.assertRaisesRegex(ValueError,'history_requires_exact_active_binding'):
            self.capture()
        self.assert_intact(before,registry)

    def test_writer_requires_current_exact_binding_even_with_valid_new_clock_and_hint(self):
        records=self.capture()
        self.sql('UPDATE path_identity_records SET canonical=? WHERE file_id=?',
            ((self.root/'stale-canonical.png').as_posix(),self.key))
        canonical=records[0]['identity']['canonical'];path=records[0]['identity']['path']
        fresh=self.service.read('get_path_identity_ingest_state',[canonical],[path],physicals=[records[0]['identity']['physical']])
        self.assertFalse(fresh['matches']);self.assertTrue(fresh['candidates'])
        records[0]['clock']=fresh['clock']
        before=self.state();registry=self.sql('SELECT * FROM path_identity_records ORDER BY file_id')
        with self.assertRaisesRegex(native.LibraryError,'history_requires_exact_active_binding'):
            self.service.submit('test_private_history_write',records).result(5)
        self.assert_intact(before,registry)

    def test_worker_rechecks_physical_file_before_publication(self):
        observe=PRIVATE_INGEST.observe_path;calls=[]
        def replaced(path,**kwargs):
            calls.append(path)
            if len(calls)==3:
                replacement=self.root/'replacement.png';replacement.write_bytes(b'new bytes')
                os.replace(replacement,self.path)
            return observe(path,**kwargs)
        before=self.state();registry=self.sql('SELECT * FROM path_identity_records ORDER BY file_id')
        with patch.object(PRIVATE_INGEST,'observe_path',side_effect=replaced):
            with self.assertRaisesRegex(ValueError,'physical_state_changed'):
                self.capture()
        self.assert_intact(before,registry)


if __name__=='__main__':unittest.main(verbosity=2)
