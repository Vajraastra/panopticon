"""Isolated real-Native contracts for the inactive ingest prototype."""
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
import tempfile
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.library_repository import LibraryRepository as PrototypeRepository
from core.ingest_review import CONFLICTS,RECEIPTS,review_notice
from core import library_service as native
from core.library_path_ingest import register
from core import catalog_reader

HERE=ROOT/'.work/pickler-repair-tests/ingest-review'
HERE.mkdir(parents=True,exist_ok=True)


class IngestReview(unittest.TestCase):
    repository_class=PrototypeRepository
    def setUp(self):
        folder=HERE/'fixture-temp';folder.mkdir(exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=folder);self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        stack=ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.object(native,'LibraryRepository',self.repository_class))
        stack.enter_context(patch.object(native,'WRITES',native.WRITES|{'prepare_ingest_reviews'}))
        stack.enter_context(patch.object(native,'READS',native.READS|{'get_ingest_review_receipt','get_ingest_review_status'}))
        self.service=native.LibraryService(self.root/'private.db');self.addCleanup(self.service.close)
        self.service.submit('prepare_ingest_reviews').result(5)
        self.service.submit('add_watched_folder',str(self.root)).result(5)
        self.path=self.root/'old.png';self.path.write_bytes(b'old bytes')
        self.data=dict(path=str(self.path),filename=self.path.name,size=9,created=1,hash_original='a'*64,
                       origin_url='old://documented',origin_site='old',artist='Old Artist')
        self.service.submit('register_files_minimal',[self.data]).result(5)
        self.key=self.service.read('get_file_id',str(self.path))
        self.service.submit('edit_curation',str(self.path),rating=5,add_tags=['keep']).result(5)

    def state(self):return self.service.read('get_copy_version_state',self.key)
    def status(self):return self.service.read('get_ingest_review_status')
    def receipt(self,operation):return self.service.read('get_ingest_review_receipt',operation)['receipt']

    def write(self,records,*,operation=None,catalog=False):
        if not catalog:
            args={} if operation is None else dict(operation_id=operation)
            return self.service.submit('register_files_minimal',records,**args).result(5)
        shaped=[dict(data=value,identity=None,clock=None,target=None,historical=False) for value in records]
        return self.service.submit('register_catalog_files_observed',shaped,self.root.as_posix(),operation_id=operation).result(5)

    def sql(self,sql,args=()):
        with closing(sqlite3.connect(self.service.db_path,isolation_level=None)) as conn:
            return conn.execute(sql,args).fetchall()

    def test_implicit_public_primitive_keeps_signature_and_records_drift_without_mutation(self):
        before=self.state()
        self.assertTrue(self.write([dict(self.data,hash_original='b'*64,origin_url='new://unrelated',artist='New')]))
        self.assertEqual(self.state(),before)
        result=self.status();self.assertEqual(result['open_count'],1)
        evidence=result['records'][0]['evidence']
        self.assertEqual(evidence['incoming']['hash_original'],'b'*64)
        self.assertEqual(evidence['native'][0]['hash_original'],'a'*64)
        self.assertEqual(evidence['native'][0]['fingerprint'],before['fingerprint'])
        self.assertEqual(result['records'][0]['reason'],'catalog_digest_drift')

    def test_catalog_detects_incoming_hash_before_old_catalog_wrapper_erases_it(self):
        before=self.state();operation=str(uuid4())
        self.write([dict(self.data,hash_original='b'*64)],operation=operation,catalog=True)
        self.assertEqual(self.state(),before)
        self.assertEqual(self.receipt(operation)['report']['conflict_count'],1)
        self.assertEqual(self.status()['records'][0]['evidence']['incoming']['hash_original'],'b'*64)

    def test_one_conflict_does_not_block_ninety_nine_healthy_observations(self):
        before=self.state();records=[dict(self.data,hash_original='b'*64)]
        for index in range(99):
            path=self.root/f'new-{index}.png';path.write_bytes(b'new')
            records.append(dict(path=str(path),filename=path.name,size=3,created=1,hash_original=f'{index+1:064x}'))
        operation=str(uuid4());self.write(records,operation=operation,catalog=True)
        result=self.receipt(operation)['report']
        self.assertEqual((result['accepted_count'],result['conflict_count']),(99,1))
        self.assertEqual(self.service.read('count_files'),100)
        self.assertEqual(self.state(),before);self.assertTrue(self.service.read('check_fts')['consistent'])
        self.assertEqual(len(result['accepted']),99)

    def test_hash_original_and_hash_mod_are_both_compatible_and_preserved(self):
        self.sql('UPDATE files SET hash_mod=? WHERE id=?',('b'*64,self.key))
        before=self.state()
        for digest,decision in [('a'*64,'compatible_original'),('b'*64,'compatible_modified')]:
            operation=str(uuid4());self.write([dict(self.data,hash_original=digest)],operation=operation,catalog=True)
            self.assertEqual(self.state(),before)
            self.assertEqual(self.receipt(operation)['report']['accepted'][0]['decision'],decision)
        self.assertEqual(self.status()['open_count'],0)

    def test_unhashed_observation_changes_do_not_mix_old_identity_state(self):
        before=self.state()
        changed=dict(self.data,size=99);changed.pop('hash_original')
        self.write([changed])
        self.assertEqual(self.state(),before)
        self.assertEqual(self.status()['records'][0]['reason'],'observation_changed_without_verified_version')

    def test_unknown_native_hash_allows_first_hash_only_for_same_observed_version(self):
        self.sql('UPDATE files SET hash_original=NULL WHERE id=?',(self.key,))
        self.write([self.data]);self.assertEqual(self.state()['state']['file']['hash_original'],'a'*64)
        self.assertEqual(self.status()['open_count'],0)
        self.sql('UPDATE files SET hash_original=NULL WHERE id=?',(self.key,));before=self.state()
        self.write([dict(self.data,size=10)])
        self.assertEqual(self.state(),before);self.assertEqual(self.status()['open_count'],1)

    def test_fractional_birthtime_and_inventory_seconds_are_same_observation(self):
        self.sql('UPDATE files SET created_date=? WHERE id=?',(1.789,self.key))
        row=dict(self.data);row.pop('hash_original')
        self.write([row]);self.assertEqual(self.status()['open_count'],0)
        self.assertEqual(self.state()['state']['file']['created_date'],1.789)

    def test_two_new_hashes_for_same_path_defer_both_without_adopting_an_identity(self):
        path=self.root/'ambiguous.png'
        row=dict(path=str(path),filename=path.name,size=10,created=1,hash_original='b'*64)
        operation=str(uuid4());self.write([row,dict(row,hash_original='c'*64)],operation=operation)
        self.assertIsNone(self.service.read('get_file_id',str(path)))
        self.assertEqual(self.receipt(operation)['report']['accepted_count'],0)
        self.assertEqual(self.receipt(operation)['report']['conflict_count'],2)

    def test_repeat_conflict_deduplicates_evidence_and_historical_ack_cannot_replay(self):
        operation=str(uuid4());row=dict(self.data,hash_original='b'*64)
        self.write([row],operation=operation)
        self.write([row],operation=str(uuid4()))
        self.assertEqual(self.status()['open_count'],1)
        self.service.submit('edit_curation',str(self.path),rating=2,add_tags=['later']).result(5)
        before=self.state();self.write([row],operation=operation)
        self.assertEqual(self.state(),before)
        with self.assertRaisesRegex(native.LibraryError,'operation_conflict'):
            self.write([dict(row,size=10)],operation=operation)

    def test_accepted_fts_failure_rolls_back_new_rows_conflicts_and_receipt_together(self):
        operation=str(uuid4());path=self.root/'good.png'
        records=[dict(self.data,hash_original='b'*64),dict(path=str(path),filename=path.name,size=1,created=1)]
        with patch.object(PrototypeRepository,'_fts_sync_file',side_effect=RuntimeError('FTS injected')):
            with self.assertRaisesRegex(native.LibraryError,'FTS injected'):self.write(records,operation=operation)
        self.assertEqual(self.status()['open_count'],0);self.assertIsNone(self.receipt(operation))
        self.assertEqual(self.service.read('count_files'),1)

    def test_prepared_observed_conflict_does_not_rebind_registry_or_curation(self):
        self.service.submit('prepare_path_identity').result(5);self.service.backfill_path_identity()
        registry=self.sql('SELECT * FROM path_identity_records WHERE file_id=?',(self.key,))
        before=self.state();operation=str(uuid4());service=self.service;folder=self.root.as_posix()
        class Watched:
            def read(self,*args,**kwargs):return service.read(*args,**kwargs)
            def submit(self,name,records):
                return service.submit('register_catalog_files_observed',records,folder,operation_id=operation)
        register(Watched(),[dict(self.data,hash_original='b'*64)],threading.Event())
        self.assertEqual(self.state(),before)
        self.assertEqual(self.sql('SELECT * FROM path_identity_records WHERE file_id=?',(self.key,)),registry)
        self.assertEqual(self.receipt(operation)['report']['conflict_count'],1)

    def test_receipt_supports_visible_notice_and_queue_ack_without_retry(self):
        operation=str(uuid4());self.write([dict(self.data,hash_original='b'*64)],operation=operation,catalog=True)
        notice=review_notice(dict(receipt=self.receipt(operation)))
        self.assertTrue(notice['requires_review']);self.assertFalse(notice['retry'])
        self.assertEqual((notice['accepted'],notice['conflicts']),(0,1))
        self.assertEqual(len(notice['conflict_ids']),1)

    def test_catalog_history_size_date_are_not_actual_stamped_observations(self):
        self.path.write_bytes(b'1234567890')
        birth=self.path.stat().st_birthtime
        self.sql('UPDATE files SET hash_mod=?,file_size=?,created_date=? WHERE id=?',('b'*64,10,birth,self.key))
        before=self.state()
        with closing(sqlite3.connect(self.root/'catalog.db',isolation_level=None)) as conn:
            conn.execute('CREATE TABLE files(hash TEXT,filename TEXT,url_source TEXT,date_added REAL,file_size INTEGER)')
            conn.execute('INSERT INTO files VALUES(?,?,?,?,?)',('a'*64,self.path.name,'new://catalog',1,9))
        current=catalog_reader.read_image_files(self.root).files[0]
        self.assertEqual((current['size'],current['created']),(10,birth))
        self.assertEqual((current['catalog_size'],current['catalog_created']),(9,1))
        incoming=dict(path=current['path'],filename=current['filename'],size=current['size'],created=current['created'],
                      hash_original=current['cherry_hash'],origin_url=current['cherry_url'])
        operation=str(uuid4());self.write([incoming],operation=operation,catalog=True)
        self.assertEqual(self.receipt(operation)['report']['accepted_count'],1)
        self.assertEqual(self.status()['open_count'],0);self.assertEqual(self.state(),before)
        operation=str(uuid4());self.write([dict(incoming,origin_url='old://documented')],operation=operation)
        self.assertEqual(self.status()['open_count'],0);self.assertEqual(self.state(),before)

    def test_empty_primitive_is_noop_without_sql_and_thousand_compatible_records_work(self):
        trace=[]
        with closing(sqlite3.connect(':memory:')) as conn:
            repo=PrototypeRepository(conn);repo.create_schema();repo.run_transformations();conn.commit()
            conn.set_trace_callback(trace.append)
            self.assertTrue(repo.register_files_minimal([]));self.assertEqual(trace,[])
            batch=[dict(path=f'C:/fixture/{i}.png',filename=f'{i}.png',size=13,created=1.25) for i in range(1000)]
            with conn:self.assertTrue(repo.register_files_minimal(batch))
            before=conn.total_changes
            with conn:self.assertTrue(repo.register_files_minimal(batch))
            self.assertEqual(conn.total_changes,before)

    def test_legacy_download_end_date_is_preserved_for_compatible_sha_or_same_size_no_sha(self):
        self.sql('UPDATE files SET created_date=? WHERE id=?',(2,self.key));before=self.state()
        for digest in ('a'*64,None):
            operation=str(uuid4());incoming=dict(self.data,created=1,hash_original=digest)
            self.write([incoming],operation=operation,catalog=True)
            self.assertEqual(self.receipt(operation)['report']['conflict_count'],0)
            self.assertEqual(self.state(),before)

    def test_raw_case_alias_requires_review_and_never_adopts_another_id(self):
        before=self.state();alias=dict(self.data,path=str(self.path).upper(),filename=self.path.name.upper(),hash_original='b'*64)
        self.write([alias])
        self.assertEqual(self.service.read('count_files'),1);self.assertEqual(self.state(),before)
        self.assertEqual(self.status()['records'][0]['reason'],'case_alias_requires_observed_identity')

    def test_opaque_legacy_hash_same_case_is_compatible_without_case_reinterpretation(self):
        self.sql('UPDATE files SET hash_original=? WHERE id=?',('LEGACY',self.key));before=self.state()
        self.write([dict(self.data,hash_original='LEGACY')])
        self.assertEqual(self.state(),before);self.assertEqual(self.status()['open_count'],0)

    def test_raw_offline_case_alias_preserves_history_and_allows_unrelated_good_copy(self):
        self.service.submit('archive_copy',self.key,expected_fingerprint=self.state()['fingerprint']).result(5)
        before=self.state();good=self.root/'unrelated.png';operation=str(uuid4())
        alias=dict(self.data,path=str(self.path).upper(),filename=self.path.name.upper(),hash_original='b'*64)
        self.write([alias,dict(path=str(good),filename=good.name,size=3,created=1,hash_original='c'*64)],operation=operation)
        report=self.receipt(operation)['report']
        self.assertEqual((report['accepted_count'],report['conflict_count']),(1,1))
        self.assertEqual(self.state(),before)
        self.assertIsNone(self.service.read('get_file_id',alias['path']))
        self.assertEqual(self.service.read('count_files'),1)
        self.assertEqual(self.status()['records'][0]['reason'],'historical_path_requires_review')
        self.assertEqual(self.status()['records'][0]['evidence']['native'][0]['id'],self.key)

    def test_prepared_target_tuple_keeps_unicode_case_alias_on_its_observed_identity(self):
        self.service.submit('prepare_path_identity').result(5);self.service.backfill_path_identity()
        operation=str(uuid4());service=self.service;folder=self.root.as_posix();targets=[]
        class Watched:
            def read(self,*args,**kwargs):return service.read(*args,**kwargs)
            def submit(self,name,records):
                targets.extend(record['target'] for record in records)
                return service.submit('register_catalog_files_observed',records,folder,operation_id=operation)
        incoming=dict(self.data,path=str(self.path).upper(),filename=self.path.name.upper())
        register(Watched(),[incoming],threading.Event())
        self.assertTrue(targets);self.assertIsInstance(targets[0],tuple)
        self.assertEqual(self.receipt(operation)['report']['accepted_count'],1)
        self.assertEqual(self.status()['open_count'],0);self.assertEqual(self.service.read('count_files'),1)
        self.assertEqual(self.service.read('get_copy_version_state',self.key)['state']['file']['rating'],5)


if __name__=='__main__':unittest.main(verbosity=2)
