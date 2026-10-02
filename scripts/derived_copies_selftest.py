"""Contratos SQL con DB privadas: revisión, identidad, recibos y rollback."""
from contextlib import closing
import copy
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.database_manager import DatabaseManager
from core.derived_copies import NAME, MAX_REQUEST, prepare, register
from core.library_repository import LibraryRepository
from core.library_service import LibraryService, LibraryError


class Derived(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.service = LibraryService(self.root/'private.db')
        self.addCleanup(self.service.close)
        self.db = DatabaseManager(service=self.service)
        self.source = str(self.root/'origen Ñ.png')
        self.output = str(self.root/'derivado ñ.webp')
        self.assertTrue(self.db.register_files_minimal([dict(path=self.source, filename='origen Ñ.png',
            size=10, created=1, hash_original='a'*64, origin_url='cherry receipt', artist='Artist')]))
        self.key = self.db.get_file_id(self.source)
        self.db.update_file_metadata(self.source, dict(width=32,height=16,tool='Old',model='M',
            positive='original prompt',negative='negative',seed='42'))
        self.db.edit_curation(self.source, add_tags=['curada'], rating=5)

    def sql(self, query, params=()):
        with closing(sqlite3.connect(self.service.db_path, isolation_level=None)) as conn:
            return conn.execute(query, params).fetchall()

    def state(self, key=None):
        return self.service.read('get_copy_version_state', self.key if key is None else key)

    def request(self, **changes):
        value = dict(expected_fingerprint=self.state()['fingerprint'], operation_id=str(uuid4()),
            source_path=self.source, path=self.output, destination_paths=[self.output],
            producer='image_optimizer', source_digest='a'*64, result_digest='b'*64,
            generation=dict(width=16,height=8,tool='New',model='M',positive='derived prompt',negative='',seed='42'),
            observation=dict(filename='derivado ñ.webp',size=20,created=2))
        value.update(changes)
        return value

    def prepare(self):
        self.assertIs(self.service.submit('prepare_derived_copies').result(timeout=10), True)

    def apply(self, value, key=None):
        return self.service.submit('register_derived_copy', self.key if key is None else key,
                                   **value).result(timeout=15)

    def receipt(self, value):
        return self.service.read('get_derived_copy_receipt', value['operation_id'])

    def all_rows(self):
        return self.sql('SELECT * FROM files ORDER BY id'), self.sql('SELECT * FROM files_fts ORDER BY rowid')

    def test_readers_and_rejected_unprepared_write_do_not_create_schema(self):
        before = self.sql('SELECT name,sql FROM sqlite_master ORDER BY name')
        request = self.request()
        self.assertEqual(self.receipt(request),dict(prepared=False,receipt=None))
        self.assertEqual(self.service.read('get_derived_copies_status'),dict(prepared=False))
        with self.assertRaisesRegex(LibraryError,'not_prepared'):
            self.apply(request)
        self.assertEqual(before,self.sql('SELECT name,sql FROM sqlite_master ORDER BY name'))
        self.assertEqual(self.db.count_files(),1)

    def test_prepare_is_explicit_idempotent_and_preserves_data(self):
        before = self.all_rows()
        self.prepare()
        self.prepare()
        self.assertEqual(before,self.all_rows())
        self.assertTrue(self.service.read('get_derived_copies_status')['prepared'])

    def test_conflicting_schema_is_preserved(self):
        self.sql(f'CREATE TABLE {NAME}(foreign_data TEXT)')
        with self.assertRaisesRegex(LibraryError,'name_conflict'):
            self.prepare()
        self.assertEqual(self.sql(f'PRAGMA table_info({NAME})')[0][1],'foreign_data')

    def test_owned_new_identity_hash_generation_fts_and_provenance(self):
        self.prepare()
        before = self.state()
        request = self.request()
        self.assertIs(self.apply(request),True)
        saved = self.receipt(request)['receipt']
        self.assertNotEqual(saved['copy_id'],self.key)
        file = self.state(saved['copy_id'])['state']['file']
        self.assertEqual(file['hash_original'],'b'*64)
        self.assertIsNone(file['hash_mod'])
        self.assertEqual((file['width'],file['height'],file['meta_positive']),(16,8,'derived prompt'))
        self.assertEqual((file['rating'],self.state(saved['copy_id'])['state']['tags']),(0,[]))
        for key in ('origin_url','origin_site','artist','phash'):
            self.assertIsNone(file[key])
        self.assertEqual(before,self.state())
        self.assertEqual(saved['source_copy_id'],self.key)
        self.assertEqual(saved['source_fingerprint'],request['expected_fingerprint'])
        self.assertEqual(saved['source_sha256'],'a'*64)
        self.assertEqual(saved['result_sha256'],'b'*64)
        self.assertEqual(saved['producer'],'image_optimizer')
        self.assertTrue(self.db.check_fts()['consistent'])
        self.assertEqual(self.db.search_by_terms(['derived prompt'])[0],1)

    def test_same_bytes_still_get_independent_identity(self):
        self.prepare()
        request = self.request(result_digest='a'*64)
        self.apply(request)
        key = self.receipt(request)['receipt']['copy_id']
        self.assertNotEqual(key,self.key)
        self.assertEqual(self.state(key)['state']['file']['hash_original'],'a'*64)
        self.assertEqual(self.state(key)['state']['tags'],[])

    def test_source_curation_generation_and_origin_changes_reject(self):
        self.prepare()
        for column,new in [('rating',4),('meta_positive','later'),('origin_url','later receipt')]:
            with self.subTest(column=column):
                request = self.request()
                self.sql(f'UPDATE files SET {column}=? WHERE id=?',(new,self.key))
                before = self.all_rows()
                with self.assertRaisesRegex(LibraryError,'changed_since_review'):
                    self.apply(request)
                self.assertEqual(before,self.all_rows())
                self.assertIsNone(self.receipt(request)['receipt'])

    def test_offline_source_rejected_with_current_revision(self):
        self.prepare()
        self.sql('UPDATE files SET is_offline=1 WHERE id=?',(self.key,))
        with self.assertRaisesRegex(LibraryError,'source_offline'):
            self.apply(self.request())
        self.assertEqual(self.db.count_files(),0)

    def test_source_path_and_digest_must_match_reviewed_copy(self):
        self.prepare()
        for changes in [dict(source_path=str(self.root/'other.png')),dict(source_digest='c'*64)]:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(LibraryError,'source_(path|hash)_changed'):
                    self.apply(self.request(**changes))
        self.assertEqual(self.db.count_files(),1)

    def test_current_hash_preferred_to_original(self):
        self.prepare()
        self.sql('UPDATE files SET hash_mod=? WHERE id=?',('c'*64,self.key))
        with self.assertRaisesRegex(LibraryError,'source_hash_changed'):
            self.apply(self.request())
        request = self.request(source_digest='c'*64)
        self.apply(request)
        self.assertEqual(self.receipt(request)['receipt']['source_sha256'],'c'*64)

    def test_unknown_source_hash_not_filled_by_derivative_registration(self):
        self.prepare()
        self.sql('UPDATE files SET hash_original=NULL WHERE id=?',(self.key,))
        before = self.state()
        self.apply(self.request())
        self.assertEqual(before,self.state())

    def test_missing_identity_rejected_without_registering_source(self):
        self.prepare()
        with self.assertRaisesRegex(LibraryError,'copy_version_missing'):
            self.apply(self.request(),key=self.key+100)
        self.assertEqual(self.db.count_files(),1)

    def test_unicode_active_and_offline_destination_collisions(self):
        self.prepare()
        other = str(self.root/'DERIVADO Ñ.WEBP')
        self.db.register_files_minimal([dict(path=other,filename='DERIVADO Ñ.WEBP',size=1,created=1)])
        for offline in (0,1):
            self.sql('UPDATE files SET is_offline=? WHERE path=?',
                     (offline,other.replace('\\','/')))
            before = self.all_rows()
            with self.assertRaisesRegex(LibraryError,'destination_occupied'):
                self.apply(self.request())
            self.assertEqual(before,self.all_rows())

    def test_supplied_alias_also_blocks_registration(self):
        self.prepare()
        alias = str(self.root/'DERIVA~1.WEB')
        self.db.register_files_minimal([dict(path=alias,filename='DERIVA~1.WEB',size=1,created=1)])
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.apply(self.request(destination_paths=[alias,self.output]))

    def test_retry_is_historical_ack_without_reapplying_current_states(self):
        self.prepare()
        request = self.request()
        self.apply(request)
        key = self.receipt(request)['receipt']['copy_id']
        self.db.edit_curation(self.output,add_tags=['later derivative'],rating=3)
        self.sql('UPDATE files SET is_offline=1 WHERE id IN (?,?)',(key,self.key))
        before = self.all_rows()
        self.assertIs(self.apply(request),True)
        self.assertEqual(before,self.all_rows())
        self.assertEqual(self.receipt(request)['receipt']['copy_id'],key)
        self.assertEqual(self.sql(f'SELECT count(*) FROM {NAME}'),[(1,)])

    def test_retry_different_payload_rejected_and_alias_order_irrelevant(self):
        self.prepare()
        alias = str(self.root/'DERIVA~1.WEB')
        request = self.request(destination_paths=[alias,self.output])
        self.apply(request)
        retry = copy.deepcopy(request)
        retry['destination_paths'] = [self.output,alias,self.output]
        self.assertIs(self.apply(retry),True)
        for changes in [dict(result_digest='d'*64),dict(producer='image_cropper'),
                        dict(generation=dict(request['generation'],positive='different'))]:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(LibraryError,'operation_conflict'):
                    self.apply(dict(request,**changes))

    def test_second_operation_cannot_reuse_first_output(self):
        self.prepare()
        self.apply(self.request())
        before = self.all_rows()
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.apply(self.request())
        self.assertEqual(before,self.all_rows())

    def test_fts_failure_rolls_back_identity_receipt_and_sequence(self):
        self.prepare()
        before = self.all_rows(),self.sql('SELECT * FROM sqlite_sequence ORDER BY name')
        request = self.request()
        with patch.object(LibraryRepository,'_fts_sync_file',side_effect=RuntimeError('fixture fts failure')):
            with self.assertRaisesRegex(LibraryError,'fixture fts failure'):
                self.apply(request)
        self.assertEqual(before,(self.all_rows(),self.sql('SELECT * FROM sqlite_sequence ORDER BY name')))
        self.assertIsNone(self.receipt(request)['receipt'])
        self.assertIs(self.apply(request),True)

    def test_receipt_insert_failure_rolls_back_fts_and_identity(self):
        self.prepare()
        self.sql(f"CREATE TRIGGER fail_receipt BEFORE INSERT ON {NAME} BEGIN SELECT RAISE(ABORT,'fixture receipt failure'); END")
        before = self.all_rows()
        with self.assertRaisesRegex(LibraryError,'fixture receipt failure'):
            self.apply(self.request())
        self.assertEqual(before,self.all_rows())
        self.assertTrue(self.db.check_fts()['consistent'])

    def test_foreign_keys_protect_provenance_identity(self):
        self.prepare()
        request = self.request()
        self.apply(request)
        key = self.receipt(request)['receipt']['copy_id']
        with closing(sqlite3.connect(self.service.db_path,isolation_level=None)) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            for copy_id in (self.key,key):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute('DELETE FROM files WHERE id=?',(copy_id,))
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_closed_owner_rejects_without_schema_or_data_changes(self):
        request = self.request()
        self.service.close()
        with self.assertRaisesRegex(LibraryError,'service_closed'):
            self.apply(request)
        self.assertEqual(self.sql('SELECT count(*) FROM files'),[(1,)])

    def test_invalid_requests_cannot_write(self):
        self.prepare()
        request = self.request()
        generation = request['generation']
        invalid = [dict(operation_id='bad'),dict(operation_id=str(uuid4()).upper()),
            dict(source_digest='A'*64),dict(expected_fingerprint='bad'),dict(producer=[]),
            dict(destination_paths=[]),dict(destination_paths=[self.source,self.output]),
            dict(destination_paths=[str(self.root/'other.webp')]),dict(path='relative.webp'),
            dict(source_path='source.png'),dict(path=self.output+'\x00'),
            dict(generation=dict(generation,width=True)),dict(generation=dict(generation,height=0)),
            dict(generation=dict(generation,positive='\x00')),dict(generation=dict(generation,model=1)),
            dict(generation=dict(generation,extra='no')),dict(observation=dict(filename='wrong',size=20,created=2)),
            dict(observation=dict(filename='derivado ñ.webp',size=True,created=2)),
            dict(observation=dict(filename='derivado ñ.webp',size=20,created=float('nan')))]
        before = self.all_rows()
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(LibraryError):
                    self.apply(dict(request,**changes))
                self.assertEqual(before,self.all_rows())
        self.assertEqual(self.sql(f'SELECT count(*) FROM {NAME}'),[(0,)])

    def test_request_byte_limit_not_just_character_limits(self):
        self.prepare()
        generation = dict(width=16,height=8,tool='😀'*65536,model='',positive='',negative='',seed='')
        self.assertGreater(len(generation['tool'].encode()),MAX_REQUEST-1)
        with self.assertRaisesRegex(LibraryError,'request_limit'):
            self.apply(self.request(generation=generation))
        self.assertEqual(self.db.count_files(),1)

    def test_corrupt_oversized_receipt_read_is_bounded_and_rejected(self):
        self.prepare()
        request = self.request()
        self.apply(request)
        self.sql(f'UPDATE {NAME} SET result_path=?',('x'*200000,))
        with self.assertRaisesRegex(ValueError,'invalid_derived_receipt'):
            self.receipt(request)
        with self.assertRaisesRegex(LibraryError,'invalid_derived_receipt'):
            self.apply(request)
        self.assertEqual(self.db.count_files(),2)

    def test_budget_failure_rolls_back_and_owner_remains_usable(self):
        self.prepare()
        request = self.request()
        before = self.all_rows()
        with patch('core.derived_copies.time.monotonic',side_effect=[0,11]):
            with self.assertRaisesRegex(LibraryError,'derived_write_budget'):
                self.apply(request)
        self.assertEqual(before,self.all_rows())
        self.assertIsNone(self.receipt(request)['receipt'])
        self.assertIs(self.apply(request),True)

    def test_direct_calls_require_owner_transaction(self):
        with closing(sqlite3.connect(self.service.db_path,isolation_level=None)) as conn:
            with self.assertRaisesRegex(RuntimeError,'requires_transaction'):
                prepare(conn)
            with self.assertRaisesRegex(RuntimeError,'requires_transaction'):
                register(conn,self.key,**self.request(),normalize=lambda value:value,update_fts=lambda key:None)

    def test_approval_does_not_modify_identity_fts_sequence_or_receipts(self):
        self.prepare()
        request = self.request()
        before = self.all_rows(),self.sql('SELECT * FROM sqlite_sequence ORDER BY name')
        self.assertIs(self.service.submit('approve_derived_copy',self.key,**request).result(timeout=15),True)
        self.assertEqual(before,(self.all_rows(),self.sql('SELECT * FROM sqlite_sequence ORDER BY name')))
        self.assertIsNone(self.receipt(request)['receipt'])
        self.apply(request)
        with self.assertRaisesRegex(LibraryError,'already_committed'):
            self.service.submit('approve_derived_copy',self.key,**request).result(timeout=15)

    def test_source_changes_after_approval_are_rechecked_at_registration(self):
        self.prepare()
        request = self.request()
        self.service.submit('approve_derived_copy',self.key,**request).result(timeout=15)
        self.db.edit_curation(self.source,add_tags=['changed after approval'])
        before = self.all_rows()
        with self.assertRaisesRegex(LibraryError,'changed_since_review'):
            self.apply(request)
        self.assertEqual(before,self.all_rows())
        self.assertIsNone(self.receipt(request)['receipt'])

    def test_destination_changes_after_approval_are_rechecked_at_registration(self):
        self.prepare()
        request = self.request()
        self.service.submit('approve_derived_copy',self.key,**request).result(timeout=15)
        self.db.register_files_minimal([dict(path=self.output,filename='derivado ñ.webp',size=1,created=1)])
        before = self.all_rows()
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.apply(request)
        self.assertEqual(before,self.all_rows())
        self.assertIsNone(self.receipt(request)['receipt'])

    def test_prequeued_cancelled_write_does_not_register_output(self):
        import threading
        self.prepare()
        request = self.request()
        started,release = threading.Event(),threading.Event()
        original = LibraryRepository.prepare_derived_copies
        def block(repo):
            started.set()
            if not release.wait(5):
                raise RuntimeError('fixture queue timeout')
            return original(repo)
        with patch.object(LibraryRepository,'prepare_derived_copies',block):
            blocker = self.service.submit('prepare_derived_copies')
            self.assertTrue(started.wait(5))
            try:
                future = self.service.submit('register_derived_copy',self.key,**request)
                self.assertTrue(future.cancel())
            finally:
                release.set()
            self.assertIs(blocker.result(timeout=10),True)
        self.service.submit('prepare_derived_copies').result(timeout=10)  # Drenar cola.
        self.assertIsNone(self.receipt(request)['receipt'])
        self.assertEqual(self.db.count_files(),1)

    def test_approval_sql_budget_interrupt_removes_handler_and_keeps_owner_alive(self):
        import time
        from core.derived_copies import approve
        self.prepare()
        request = self.request()
        # Un único SELECT recursivo privado fuerza al progress handler, sin una
        # biblioteca grande ni esperar diez segundos reales.
        real_time = time.monotonic
        count = 0
        def expired():
            nonlocal count
            count += 1
            return 0 if count == 1 else 11
        with closing(sqlite3.connect(self.service.db_path,isolation_level=None)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            def long_check(*args, **kwargs):
                conn.execute('WITH RECURSIVE x(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM x WHERE i<100000) SELECT sum(i) FROM x').fetchone()
            with patch('core.derived_copies.time.monotonic',side_effect=expired),patch('core.derived_copies._check',side_effect=long_check):
                with self.assertRaisesRegex(ValueError,'derived_write_budget'):
                    approve(conn,self.key,**request,normalize=LibraryRepository(conn)._normalize_path)
            conn.rollback()
            self.assertIsInstance(real_time(),float)
            self.assertEqual(conn.execute('WITH RECURSIVE x(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM x WHERE i<10000) SELECT sum(i) FROM x').fetchone(),(50005000,))
        self.assertIs(self.apply(request),True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
