"""Explicit transfer contracts on private Native databases; no real files moved."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.database_manager import DatabaseManager
from core.library_service import LibraryService, LibraryError
from core.library_repository import LibraryRepository
from core.library_transfers import NAME, PATHS, DDL, INDEXES, request as normalize_request


class Transfers(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.service = LibraryService(self.root/'private.db')
        self.addCleanup(self.service.close)
        self.db = DatabaseManager(service=self.service)
        self.source = (self.root/'source Ñ.png').as_posix()
        self.output = (self.root/'new folder'/'copy ñ.png').as_posix()
        self.db.register_files_minimal([dict(path=self.source, filename='source Ñ.png',size=10,
            created=1,hash_original='a'*64,origin_url='Cherry receipt',artist='Artist')])
        self.key = self.db.get_file_id(self.source)
        self.db.update_file_metadata(self.source,dict(width=32,height=16,tool='Old',model='M',
            positive='documented prompt',negative='negative',seed='42'))
        self.db.edit_curation(self.source,add_tags=['curada'],rating=5)
        self.sql("UPDATE file_tags SET source='manual'; UPDATE tags SET category='Subject'")

    def sql(self, sql, params=()):
        with closing(sqlite3.connect(self.service.db_path, isolation_level=None)) as conn:
            if ';' in sql:
                return conn.executescript(sql)
            return conn.execute(sql,params).fetchall()

    def state(self, key=None):
        return self.service.read('get_copy_version_state', self.key if key is None else key)

    def request(self, **changes):
        value = dict(operation_id=str(uuid4()), source_copy_id=self.key, action='copy',
            source_path=self.source,result_path=self.output,destination_paths=[self.output],
            digest='a'*64,caption_digest=None,source_fingerprint=self.state()['fingerprint'])
        value.update(changes)
        return value

    def prepare(self):
        return self.service.submit('prepare_transfers').result(10)

    def approve(self, value):
        return self.service.submit('approve_transfer',value).result(10)

    def commit(self, value, **changes):
        args=dict(observation={'size':10},identity={'canonical':self.output,'physical':[1,2,3,4,5,6,7]})
        args.update(changes)
        return self.service.submit('commit_transfer',value,**args).result(10)

    def receipt(self, value):
        return self.service.read('get_transfer_receipt',value['operation_id'])['receipt']

    def apply(self, value):
        self.prepare(); self.approve(value); self.commit(value)
        return self.receipt(value)

    def test_reads_and_unprepared_rejection_do_not_create_schema(self):
        before=self.sql('SELECT name,sql FROM sqlite_master ORDER BY name')
        value=self.request()
        self.assertFalse(self.service.read('get_transfer_status')['prepared'])
        self.assertIsNone(self.receipt(value))
        with self.assertRaisesRegex(LibraryError,'not_prepared'):self.approve(value)
        self.assertEqual(before,self.sql('SELECT name,sql FROM sqlite_master ORDER BY name'))

    def test_prepare_backed_up_before_ddl_and_idempotent(self):
        before=self.state(); self.prepare(); self.prepare()
        backups=list(self.root.glob('private.db.pre-transfers-*.bak'))
        self.assertEqual(len(backups),1)
        with closing(sqlite3.connect(backups[0])) as conn:
            self.assertIsNone(conn.execute('SELECT name FROM sqlite_master WHERE name=?',(NAME,)).fetchone())
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone(),('ok',))
        self.assertEqual(before,self.state())

    def test_backup_failure_prevents_ddl(self):
        with patch('core.library_service.backup_database',side_effect=OSError('backup failed')):
            with self.assertRaisesRegex(LibraryError,'backup failed'):self.prepare()
        self.assertFalse(self.service.read('get_transfer_status')['prepared'])

    def test_foreign_schema_preserved(self):
        self.sql(f'CREATE TABLE {NAME}(foreign_data TEXT)')
        with self.assertRaisesRegex(LibraryError,'requires_review'):self.prepare()
        self.assertEqual(self.sql(f'PRAGMA table_info({NAME})')[0][1],'foreign_data')

    def test_copy_initial_curation_metadata_and_independent_edits(self):
        before=self.state(); receipt=self.apply(self.request()); key=receipt['result_copy_id']
        self.assertNotEqual(key,self.key)
        copied=self.state(key)['state']; original=before['state']
        self.assertEqual(copied['tags'],original['tags'])
        for column in original['file']:
            if column not in ('id','path','filename'):
                self.assertEqual(copied['file'][column],original['file'][column],column)
        self.assertEqual(before,self.state())
        self.db.edit_curation(self.output,remove_tags=['curada'],add_tags=['new'],rating=2)
        self.assertEqual(before,self.state())
        self.assertEqual(self.db.get_tags_for_file(self.output),['new'])
        self.assertTrue(self.db.check_fts()['consistent'])

    def test_move_preserves_id_and_every_curation_field(self):
        before=self.state()['state']; receipt=self.apply(self.request(action='move'))
        self.assertEqual(receipt['result_copy_id'],self.key)
        after=self.state()['state']
        self.assertEqual(after['tags'],before['tags'])
        for key in before['file']:
            if key not in ('path','filename'):self.assertEqual(after['file'][key],before['file'][key],key)
        self.assertEqual(after['file']['path'],self.output)
        self.assertEqual(self.db.get_file_id(self.source),None)
        self.assertTrue(self.db.check_fts()['consistent'])

    def test_discard_is_recoverable_move_same_identity(self):
        self.assertEqual(self.apply(self.request(action='discard'))['result_copy_id'],self.key)
        self.assertEqual(self.state()['state']['file']['is_offline'],0)

    def test_completed_receipt_never_replays_later_curation(self):
        value=self.request(); receipt=self.apply(value)
        self.db.edit_curation(self.output,rating=1,remove_tags=['curada'])
        later=self.state(receipt['result_copy_id'])
        self.approve(value); self.commit(value)
        self.assertEqual(later,self.state(receipt['result_copy_id']))

    def test_approval_does_not_relocate_native_row(self):
        self.prepare(); before=self.state(); value=self.request(action='move'); self.approve(value)
        self.assertEqual(before,self.state())
        self.assertEqual(self.receipt(value)['phase'],'approved')

    def test_source_changed_before_commit_is_rejected(self):
        self.prepare(); value=self.request(); self.approve(value); self.db.edit_curation(self.source,rating=3)
        before=self.state()
        with self.assertRaisesRegex(LibraryError,'source_changed'):self.commit(value)
        self.assertEqual(before,self.state()); self.assertEqual(self.receipt(value)['phase'],'approved')
        self.assertEqual(self.db.count_files(),1)

    def test_digest_offline_path_and_missing_source_rejected(self):
        self.prepare()
        for change in [dict(digest='b'*64),dict(source_path=(self.root/'wrong.png').as_posix()),dict(source_copy_id=999)]:
            with self.subTest(change=change),self.assertRaises(LibraryError):self.approve(self.request(**change))
        self.sql('UPDATE files SET is_offline=1 WHERE id=?',(self.key,))
        with self.assertRaisesRegex(LibraryError,'source_changed'):self.approve(self.request())

    def test_registered_destination_even_offline_requires_review(self):
        self.prepare()
        self.db.register_files_minimal([dict(path=self.output,filename='copy ñ.png',size=10,created=1)])
        self.sql('UPDATE files SET is_offline=1 WHERE path=?',(self.output,))
        with self.assertRaisesRegex(LibraryError,'destination_requires_review'):self.approve(self.request())

    def test_two_pending_operations_cannot_reserve_same_source(self):
        self.prepare(); value=self.request(); self.approve(value)
        other=(self.root/'other.png').as_posix()
        with self.assertRaises(LibraryError):self.approve(self.request(result_path=other,destination_paths=[other]))

    def test_pending_case_alias_destination_cannot_be_reserved_twice(self):
        self.prepare(); self.approve(self.request())
        other=(self.root/'other source.png').as_posix()
        self.db.register_files_minimal([dict(path=other,filename='other source.png',size=10,created=1,hash_original='a'*64)])
        key=self.db.get_file_id(other)
        value=self.request(source_copy_id=key,source_path=other,result_path=self.output.upper(),
            destination_paths=[self.output.upper()],source_fingerprint=self.state(key)['fingerprint'])
        with self.assertRaisesRegex(LibraryError,'destination_requires_review'):self.approve(value)

    def test_abort_releases_reservation_but_cannot_abort_complete(self):
        self.prepare(); value=self.request(); self.approve(value)
        self.service.submit('abort_transfer',value['operation_id'],expected_fingerprint=normalize_request(value)[1]).result(5)
        self.assertEqual(self.receipt(value)['phase'],'aborted')
        other=self.request(); self.approve(other); self.commit(other)
        with self.assertRaisesRegex(LibraryError,'operation_conflict'):
            self.service.submit('abort_transfer',other['operation_id'],expected_fingerprint=normalize_request(other)[1]).result(5)

    def test_pending_move_blocks_absence_archive_then_abort_releases(self):
        self.prepare(); value=self.request(action='move'); self.approve(value)
        reviewed=self.service.read('get_duplicate_snapshot',[self.source])[0]
        approved=[dict(path=self.source,copy_id=self.key,fingerprint=reviewed['fingerprint'])]
        with self.assertRaisesRegex(LibraryError,'transfer_pending'):
            self.service.submit('archive_missing_copies',approved,folder=self.root.as_posix()).result(5)
        self.assertEqual(self.state()['state']['file']['is_offline'],0)
        self.service.submit('abort_transfer',value['operation_id'],expected_fingerprint=normalize_request(value)[1]).result(5)
        self.service.submit('archive_missing_copies',approved,folder=self.root.as_posix()).result(5)
        self.assertEqual(self.state()['state']['file']['is_offline'],1)

    def test_prepared_identity_updates_same_id_for_move(self):
        self.service.submit('prepare_path_identity').result(5)
        value=self.request(action='move'); self.apply(value)
        matches=self.service.read('get_path_identity_matches',self.output)
        self.assertEqual([row[0] for row in matches['records']],[self.key])
        self.assertEqual(self.state()['state']['file']['path'],self.output)

    def test_fts_failure_rolls_back_row_tags_receipt_and_identity(self):
        self.service.submit('prepare_path_identity').result(5)
        self.prepare(); value=self.request(); self.approve(value); before=self.state()
        with patch.object(LibraryRepository,'_fts_sync_file',side_effect=RuntimeError('FTS injected')):
            with self.assertRaisesRegex(LibraryError,'FTS injected'):self.commit(value)
        self.assertEqual(self.db.count_files(),1); self.assertEqual(before,self.state())
        self.assertEqual(self.receipt(value)['phase'],'approved')
        self.assertEqual(self.service.read('get_path_identity_matches',self.output)['records'],[])

    def test_invalid_worker_evidence_does_not_change_native(self):
        self.prepare(); value=self.request(); self.approve(value); before=self.state()
        for change in [dict(observation={'size':-1}),dict(observation={'size':True}),
                dict(identity={'canonical':self.output,'physical':[1]}),dict(identity={'canonical':self.source,'physical':[1]*7}),
                dict(identity={'canonical':self.output,'physical':[1]*7,'unknown':1})]:
            with self.subTest(change=change),self.assertRaises(LibraryError):self.commit(value,**change)
        self.assertEqual(before,self.state()); self.assertEqual(self.db.count_files(),1)

    def test_malformed_requests_rejected_before_approval(self):
        self.prepare()
        for change in [dict(action='delete'),dict(operation_id='not UUID'),dict(source_copy_id=True),
                dict(digest='BAD'),dict(caption_digest='bad'),dict(destination_paths=[]),dict(destination_paths=[self.source])]:
            with self.subTest(change=change),self.assertRaises(LibraryError):self.approve(self.request(**change))
        self.assertEqual(self.sql(f'SELECT COUNT(*) FROM {NAME}'),[(0,)])

    def test_copy_known_current_hash_and_unknown_original(self):
        self.sql('UPDATE files SET hash_original=NULL,hash_mod=? WHERE id=?',('a'*64,self.key))
        receipt=self.apply(self.request()); copied=self.state(receipt['result_copy_id'])['state']['file']
        self.assertEqual(copied['hash_original'],'a'*64); self.assertEqual(copied['hash_mod'],'a'*64)

    def test_request_uuid_cannot_be_reused_for_another_action(self):
        self.prepare(); value=self.request(); self.approve(value)
        with self.assertRaisesRegex(LibraryError,'operation_conflict'):self.approve(dict(value,action='move'))

    def test_repeated_pending_approval_rechecks_source(self):
        self.prepare(); value=self.request(); self.approve(value)
        self.db.edit_curation(self.source,rating=2)
        with self.assertRaisesRegex(LibraryError,'source_changed'):self.approve(value)
        self.assertEqual(self.receipt(value)['phase'],'approved')

    def test_zero_and_negative_ids_are_valid_sources(self):
        for new_id in (0,-7):
            with self.subTest(new_id=new_id):
                path=(self.root/f'signed{new_id}.png').as_posix()
                self.sql('INSERT INTO files(id,path,filename,file_size,hash_original) VALUES(?,?,?,?,?)',
                    (new_id,path,f'signed{new_id}.png',10,'a'*64))
                output=(self.root/f'copy{new_id}.png').as_posix()
                value=self.request(source_copy_id=new_id,source_path=path,result_path=output,
                    destination_paths=[output],source_fingerprint=self.state(new_id)['fingerprint'])
                self.prepare(); self.approve(value)
                self.commit(value,identity={'canonical':output,'physical':[1]*7})
                self.assertEqual(self.receipt(value)['source_copy_id'],new_id)

    def test_backed_up_upgrade_adds_alias_table_to_prior_owned_schema(self):
        self.sql(DDL)
        for _,ddl in INDEXES:self.sql(ddl)
        before=self.state(); self.assertFalse(self.service.read('get_transfer_status')['prepared'])
        self.prepare(); self.assertEqual(before,self.state())
        self.assertTrue(self.service.read('get_transfer_status')['prepared'])
        backups=list(self.root.glob('private.db.pre-transfers-*.bak')); self.assertEqual(len(backups),1)
        with closing(sqlite3.connect(backups[0])) as conn:
            self.assertIsNotNone(conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(NAME,)).fetchone())
            self.assertIsNone(conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(PATHS,)).fetchone())

    def test_pending_long_short_aliases_block_ingestion(self):
        self.prepare()
        short=(self.root/'LONGNA~1'/'copy.png').as_posix()
        value=self.request(destination_paths=[self.output,short],source_paths=[self.source])
        self.approve(value)
        with self.assertRaisesRegex(LibraryError,'ingestion_pending'):
            self.service.submit('register_files_minimal',[dict(path=short,filename='copy.png',size=10,created=1)]).result(5)
        self.assertEqual(self.db.count_files(),1)

    def test_foreign_alias_schema_is_preserved(self):
        self.sql(f'CREATE TABLE {PATHS}(foreign_data TEXT)')
        with self.assertRaisesRegex(LibraryError,'requires_review'):self.prepare()
        self.assertEqual(self.sql(f'PRAGMA table_info({PATHS})')[0][1],'foreign_data')

    def test_copy_retirement_is_idempotent_with_offline_history(self):
        value=self.request(); saved=self.apply(value); key=saved['result_copy_id']
        self.service.submit('retire_transfer_copy',value['operation_id']).result(5)
        before=self.state(key)
        self.service.submit('retire_transfer_copy',value['operation_id']).result(5)
        self.assertEqual(before,self.state(key)); self.assertEqual(before['state']['file']['is_offline'],1)
        self.assertEqual(before['state']['file']['rating'],5)

    def test_restore_approval_preserves_new_curation_but_requires_fresh_cas(self):
        self.prepare(); value=self.request(action='move'); self.approve(value)
        self.db.edit_curation(self.source,rating=3); current=self.state()
        identity={'canonical':self.source,'physical':[1]*7}
        with self.assertRaisesRegex(LibraryError,'source_changed'):
            self.service.submit('approve_transfer_restore',value,value['source_fingerprint'],identity).result(5)
        self.service.submit('approve_transfer_restore',value,current['fingerprint'],identity).result(5)
        self.assertEqual(current,self.state()); self.assertEqual(self.receipt(value)['phase'],'approved')


if __name__=='__main__':unittest.main(verbosity=2)
