"""Cache por identidad/SHA/algoritmo: SQL temporal, sin decodificar ni retirar."""
from contextlib import closing
from copy import deepcopy
import ntpath
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.perceptual_hash import NAME,DDL,status,prepare,page,store
from core.library_connections import open_reader
from core.library_service import LibraryError
import duplicate_exact_review_selftest as helpers

A,B='a'*64,'b'*64
ALGO='phash8-v1/imagehash:4.3.2/pillow:12/scipy:1/numpy:2'
PHASH='a'*16
normalize=lambda path:ntpath.normpath(path).replace('\\','/')


class SQL(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.execute('CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT,filename TEXT,hash_original,hash_mod,phash,is_offline DEFAULT 0)')
        self.addCleanup(self.conn.close)
    def row(self,key=1,*,path=None,digest=A,modified=None,phash=None,offline=0):
        self.conn.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?)',(key,path or f'E:/scope/{key}.png',f'{key}.png',digest,modified,phash,offline))
    def record(self,key=1,*,digest=A,phash=PHASH,path=None):
        return dict(copy_id=key,path=path or f'E:/scope/{key}.png',source_sha256=digest,phash=phash)
    def page(self,**kwargs):return page(self.conn,['E:/scope'],ALGO,normalize=normalize,**kwargs)
    def store(self,records=None,algorithm=ALGO):return store(self.conn,records or [self.record()],algorithm,normalize=normalize)

    def test_reads_do_not_create_schema_and_legacy_phash_is_not_proven(self):
        self.row(phash=PHASH)
        self.assertFalse(status(self.conn)['prepared'])
        value=self.page()
        self.assertFalse(value['prepared'])
        self.assertIsNone(value['records'][0]['phash'])
        self.assertFalse(status(self.conn)['prepared'])

    def test_explicit_prepare_requires_owner_transaction_and_is_idempotent(self):
        with self.assertRaisesRegex(RuntimeError,'requires_transaction'):prepare(self.conn)
        self.conn.execute('BEGIN')
        self.assertTrue(prepare(self.conn))
        changes=self.conn.total_changes
        self.assertTrue(prepare(self.conn))
        self.assertEqual(self.conn.total_changes,changes)
        self.conn.commit()
        self.assertTrue(status(self.conn)['prepared'])

    def test_foreign_registry_name_is_never_replaced_or_dropped(self):
        self.conn.execute(f'CREATE TABLE {NAME}(foreign_value)')
        self.conn.execute(f'INSERT INTO {NAME} VALUES(?)',('preserve',))
        with self.assertRaisesRegex(ValueError,'name_conflict'):prepare(self.conn)
        self.assertEqual(self.conn.execute(f'SELECT * FROM {NAME}').fetchall(),[('preserve',)])

    def test_proven_hash_reused_only_for_same_native_sha_and_algorithm(self):
        self.row()
        prepare(self.conn)
        self.assertTrue(self.store())
        self.assertEqual(self.page()['records'][0]['phash'],PHASH)
        self.conn.execute('UPDATE files SET hash_mod=?',(B,))
        self.assertIsNone(self.page()['records'][0]['phash'])
        self.conn.execute('UPDATE files SET hash_mod=NULL')
        value=page(self.conn,['E:/scope'],ALGO.replace('pillow:12','pillow:13'),normalize=normalize)
        self.assertIsNone(value['records'][0]['phash'])

    def test_changed_native_phash_invalidates_evidence_instead_of_trusting_either(self):
        self.row()
        prepare(self.conn)
        self.store()
        self.conn.execute('UPDATE files SET phash=?',('b'*16,))
        self.assertIsNone(self.page()['records'][0]['phash'])

    def test_identical_store_is_noop_and_new_algorithm_changes_only_derived_data(self):
        self.row()
        prepare(self.conn)
        self.store()
        before=list(self.conn.execute('SELECT * FROM files'))
        changes=self.conn.total_changes
        self.store()
        self.assertEqual(self.conn.total_changes,changes)
        self.store(algorithm=ALGO.replace('pillow:12','pillow:13'))
        self.assertEqual(list(self.conn.execute('SELECT * FROM files')),before)

    def test_sha_mismatch_one_member_blocks_entire_batch_before_any_mutation(self):
        self.row()
        self.row(2,digest=B)
        prepare(self.conn)
        changes=self.conn.total_changes
        with self.assertRaisesRegex(ValueError,'identity_changed'):self.store([self.record(),self.record(2)])
        self.assertEqual(self.conn.total_changes,changes)
        self.assertEqual(self.conn.execute(f'SELECT count(*) FROM {NAME}').fetchone()[0],0)
        self.assertIsNone(self.conn.execute('SELECT phash FROM files WHERE id=1').fetchone()[0])

    def test_offline_and_replaced_id_cannot_receive_previous_evidence(self):
        self.row(offline=1)
        self.row(2,path='E:/scope/1.png')
        prepare(self.conn)
        with self.assertRaisesRegex(ValueError,'identity_changed'):self.store()
        self.assertEqual(self.conn.execute(f'SELECT count(*) FROM {NAME}').fetchone()[0],0)
        self.assertEqual([r['copy_id'] for r in self.page()['records']],[2])

    def test_nonimage_record_never_accepts_perceptual_evidence(self):
        self.row(path='E:/scope/document.txt')
        prepare(self.conn)
        with self.assertRaisesRegex(ValueError,'identity_changed'):
            self.store([self.record(path='E:/scope/document.txt')])
        self.assertEqual(self.conn.execute(f'SELECT count(*) FROM {NAME}').fetchone()[0],0)

    def test_unknown_invalid_blob_hashes_are_counted_but_not_written(self):
        for key,digest in enumerate((None,'','g'*64,b'a'*64,A.upper()),1):self.row(key,digest=digest)
        self.row(6,digest=B,offline=1)
        value=self.page()
        self.assertEqual((value['total_images'],value['known_images'],value['unknown_images']),(5,1,4))
        self.assertEqual(value['records'][0]['source_sha256'],A)

    def test_current_mod_and_empty_fallback_are_the_content_identity(self):
        self.row(modified=B)
        self.row(2,modified='')
        self.assertEqual([row['source_sha256'] for row in self.page()['records']],[B,A])

    def test_keyset_pages_exclude_offline_and_nonimage_paths_without_prefix_approval(self):
        for key in range(1,8):self.row(key)
        self.row(8,path='E:/scope/document.txt')
        seen,cursor,bound=[],0,None
        while True:
            value=self.page(after_id=cursor,through_id=bound,limit=2)
            self.assertEqual(value['total_images'],7 if cursor==0 else None)
            bound=value['through_id']
            seen.extend(row['copy_id'] for row in value['records'])
            cursor=value['next_after_id']
            if not value['has_more']:break
        self.assertEqual(seen,list(range(1,8)))

    def test_page_high_watermark_excludes_later_additions_and_continuation_does_not_recount_scope(self):
        for key in range(1,5):self.row(key)
        first=self.page(limit=2)
        self.row(5)
        statements=[]
        self.conn.set_trace_callback(statements.append)
        second=self.page(after_id=first['next_after_id'],through_id=first['through_id'],limit=2)
        self.assertEqual([row['copy_id'] for row in second['records']],[3,4])
        self.assertFalse(second['has_more'])
        self.assertIsNone(second['total_images'])
        self.assertEqual(len(statements),2)  # estado + página; no COUNT de todo el alcance.
        with self.assertRaisesRegex(ValueError,'invalid_phash_page'):self.page(after_id=2)

    def test_literal_scope_separator_and_case_support_one_folder(self):
        self.row(path='E:/literal%_/a.PNG')
        self.row(2,path='E:/literalXY/a.png')
        self.row(3,path='E:/literal%_sibling/a.png')
        value=page(self.conn,[r'E:\literal%_'],ALGO,normalize=normalize)
        self.assertEqual([row['copy_id'] for row in value['records']],[1])

    def test_invalid_batch_algorithm_and_duplicate_ids_never_write(self):
        self.row()
        prepare(self.conn)
        for mutation in (dict(copy_id=True),dict(source_sha256='x'*64),dict(phash='x'*16),dict(path=''),dict(extra=1)):
            with self.assertRaisesRegex(ValueError,'invalid_phash_batch'):self.store([self.record()|mutation])
        for records in ([self.record(),self.record()],[self.record()]*65):
            with self.assertRaisesRegex(ValueError,'invalid_phash_batch'):self.store(records)
        for algorithm in ('legacy-md5','phash8-v1/\x00','phash8-v1/'+'a'*161):
            with self.assertRaisesRegex(ValueError,'algorithm'):self.store(algorithm=algorithm)
        self.assertEqual(self.conn.execute(f'SELECT count(*) FROM {NAME}').fetchone()[0],0)

    def test_explicit_registry_and_transaction_required_for_store(self):
        self.row()
        with self.assertRaisesRegex(ValueError,'not_prepared'):self.store()
        self.conn.commit()
        with self.assertRaisesRegex(RuntimeError,'transaction'):self.store()


class Service(helpers.Review):
    def record(self,path):
        value=self.service.read('get_duplicate_snapshot',[path.as_posix()])[0]['state']['file']
        return dict(copy_id=value['id'],path=path.as_posix(),source_sha256=value['hash_original'],phash=PHASH)

    def test_native_owner_confirms_only_derived_changes_preserving_curated_state_fts_and_receipts(self):
        raw,curated=self.pair()
        receipt=raw.parent/'catalog.db'
        receipt.write_bytes(b'Cherry receipts')
        paths=[raw.as_posix(),curated.as_posix()]
        before=self.service.read('get_duplicate_snapshot',paths)
        self.assertTrue(self.service.submit('prepare_phash_registry').result(3))
        self.assertTrue(self.service.submit('store_phash_batch',[self.record(raw),self.record(curated)],ALGO).result(3))
        after=self.service.read('get_duplicate_snapshot',paths)
        for old,new in zip(before,after):
            expected=deepcopy(old['state'])
            expected['file']['phash']=PHASH
            self.assertEqual(new['state'],expected)
        self.assertEqual(receipt.read_bytes(),b'Cherry receipts')
        self.assertEqual(self.service.read('check_fts')['stale'],0)

    def test_failed_atomic_batch_leaves_no_native_phash_or_evidence_and_next_write_works(self):
        raw,curated=self.pair()
        self.service.submit('prepare_phash_registry').result(3)
        batch=[self.record(raw),self.record(curated)]
        batch[1]['source_sha256']=B
        with self.assertRaisesRegex(Exception,'identity_changed'):
            self.service.submit('store_phash_batch',batch,ALGO).result(3)
        value=self.service.read('get_phash_page',[self.root.as_posix()],ALGO)
        self.assertTrue(all(record['phash'] is None for record in value['records']))
        self.service.submit('edit_curation',curated.as_posix(),rating=4).result(3)
        self.assertTrue(self.service.submit('store_phash_batch',[self.record(raw)],ALGO).result(3))

    def test_readonly_query_and_pre_cancel_do_not_prepare_registry_or_observe_filesystem(self):
        self.pair()
        with (patch.object(Path,'stat',side_effect=AssertionError('filesystem observed')),
              patch.object(self.service,'submit',side_effect=AssertionError('write admitted'))):
            value=self.service.read('get_phash_page',[self.root.as_posix()],ALGO)
        self.assertFalse(value['prepared'])
        event=threading.Event()
        event.set()
        with self.assertRaisesRegex(LibraryError,'cancelled'):
            self.service.read('get_phash_page',[self.root.as_posix()],ALGO,cancel_event=event)
        self.assertFalse(self.service.read('get_phash_registry_status')['prepared'])

    def test_read_budget_releases_reader_and_owner_remains_usable(self):
        self.pair()
        for i in range(100):self.file(f'A/{i}.png')
        with patch.dict('core.library_service.READ_BUDGET_SECONDS',get_phash_page=0):
            with self.assertRaisesRegex(LibraryError,'discovery_budget_exceeded'):
                self.service.read('get_phash_page',[self.root.as_posix()],ALGO)
        self.assertTrue(self.service.submit('prepare_phash_registry').result(3))
        self.assertEqual(len(self.service.read('get_phash_page',[self.root.as_posix()],ALGO)['records']),102)

    def test_failure_after_first_dml_rolls_back_entire_batch_and_releases_owner(self):
        raw,curated=self.pair()
        self.service.submit('prepare_phash_registry').result(3)
        records=[self.record(raw),self.record(curated)]
        with closing(sqlite3.connect(Path(self.temp.name)/'fixture.db')) as conn:
            conn.execute(f"CREATE TRIGGER inject_phash_failure BEFORE INSERT ON {NAME} WHEN NEW.copy_id={records[1]['copy_id']} BEGIN SELECT RAISE(ABORT,'injected phash failure'); END")
            conn.commit()
        with self.assertRaisesRegex(Exception,'injected phash failure'):
            self.service.submit('store_phash_batch',records,ALGO).result(3)
        value=self.service.read('get_phash_page',[self.root.as_posix()],ALGO)
        self.assertTrue(all(row['phash'] is None for row in value['records']))
        with closing(sqlite3.connect(Path(self.temp.name)/'fixture.db')) as conn:
            self.assertEqual(conn.execute(f'SELECT count(*) FROM {NAME}').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT count(*) FROM files WHERE phash IS NOT NULL').fetchone()[0],0)
            conn.execute('DROP TRIGGER inject_phash_failure')
            conn.commit()
        self.assertTrue(self.service.submit('store_phash_batch',records,ALGO).result(3))


if __name__=='__main__':
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(SQL))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromName('Service.'+name,sys.modules[__name__])
        for name in Service.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
