"""Restauración real privada Windows: identidad/curación/recibos y cortes FS/SQL."""
from concurrent.futures import Future
from contextlib import closing
from copy import deepcopy
import hashlib,json,ntpath,sqlite3,sys,threading,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from core.duplicate_restore import restore
from core.library_versions import state
from modules.duplicate_finder.logic.recovery_restore import RecoveryRestore
from modules.duplicate_finder.logic.recovery_storage import write_journal
from modules.duplicate_finder.logic.restore_worker import RecoveryRestoreWorker
from curation_snapshot_selftest import pump
import duplicate_recovery_selftest as prior


class Restore(prior.Recovery):

    def test_prepared_restoration_binds_same_id_and_keeps_folder_reads_usable(self):
        source,keeper=self.prepare(curated=True); self.service.backfill_path_identity()
        result=self.run_restore(); self.assertTrue(result['complete'],result)
        coverage=self.service.read('get_path_identity_status')
        self.assertEqual((coverage['pending'],coverage['unresolved']),(0,0))
        after=self.service.read('get_copy_version_state',self.arguments['copy_id'])
        expected=deepcopy(self.before['state']); expected['file']['is_offline']=0
        self.assertEqual(after['state'],expected)
        self.assertEqual(self.service.read_worker('get_folder_count',str(source.parent)),1)

    def test_prepared_restore_binding_failure_keeps_bytes_recoverable_and_id_offline(self):
        source,_=self.prepare(); self.service.backfill_path_identity()
        with patch('core.path_identity_online.store',side_effect=ValueError('binding_failed')):
            result=self.run_restore()
        self.assertTrue(result['moved']); self.assertFalse(result['committed'])
        self.assertEqual(source.read_bytes(),b'same bytes')
        self.assertEqual(self.service.read('get_copy_version_state',self.arguments['copy_id']),self.before)
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)
        self.assertEqual(json.loads(Path(result['operation_journal']).read_bytes())['phase'],'partial')

    def sql(self,statement,args=()):
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute(statement,args);conn.commit()

    def prepare(self,failure=False,curated=False):
        raw,keeper=self.pair()
        if curated:
            self.service.submit('edit_curation',raw.as_posix(),add_tags=['documented'],rating=5).result(3)
            self.sql("UPDATE files SET meta_tool='fixture',meta_model='model',meta_positive='prompt',meta_negative='negative',meta_seed='7',origin_url='https://fixture',origin_site='fixture',artist='artist',hash_mod=hash_original,hash_original=?",('f'*64,))
        chosen=keeper if curated else raw
        self.retire(chosen,failure=failure)
        review=self.audit();move=next(move for move in review['moves'] if move['source']==chosen.as_posix())
        copy=next(copy for copy in review['copies'] if copy['copy_id']==move['copy_id'])
        self.arguments=dict(journal=str(self.journal),copy_id=copy['copy_id'],journal_fingerprint=review['journal_fingerprint'],expected_fingerprint=copy['fingerprint'])
        self.before=self.service.read('get_copy_version_state',copy['copy_id'])
        self.destination=Path(move['destination']);self.source=chosen
        return chosen,raw if curated else keeper

    def run_restore(self,event=None):return RecoveryRestore(self.service,**self.arguments).run(event)

    def test_restores_same_curated_id_with_all_generation_original_mod_hash_tags_and_receipts(self):
        source,keeper=self.prepare(curated=True)
        receipt=self.root/'catalog.db';receipt.write_bytes(b'hashes/URLs/alias safeguard')
        original=self.journal.read_bytes()
        result=self.run_restore()
        self.assertTrue(result['complete']);self.assertTrue(result['committed']);self.assertTrue(result['moved'])
        self.assertEqual(source.read_bytes(),b'same bytes');self.assertEqual(keeper.read_bytes(),b'same bytes')
        self.assertFalse(self.destination.exists());self.assertEqual(original,self.journal.read_bytes())
        after=self.service.read('get_copy_version_state',self.arguments['copy_id'])
        expected=deepcopy(self.before['state']);expected['file']['is_offline']=0
        self.assertEqual(after['state'],expected)
        self.assertEqual(receipt.read_bytes(),b'hashes/URLs/alias safeguard');self.assertEqual(self.service.read('count_files'),2)
        operation=json.loads(Path(result['operation_journal']).read_bytes())
        self.assertEqual(operation['phase'],'complete');self.assertTrue(operation['committed'])
        self.assertEqual(operation['before']['state'],self.before['state'])
        self.assertEqual(self.audit()['moves'][0]['observed'],'not_retired')

    def test_archived_source_present_reactivates_without_any_rename(self):
        source,_=self.prepare(failure=True)
        with patch('modules.duplicate_finder.logic.recovery_restore.os.rename',side_effect=AssertionError('unneeded rename')):result=self.run_restore()
        self.assertTrue(result['complete']);self.assertTrue(result['committed']);self.assertFalse(result['moved'])
        self.assertEqual(source.read_bytes(),b'same bytes');self.assertEqual(self.service.read('count_files'),2)

    def test_pre_cancel_performs_no_read_or_writes_or_operation_journal(self):
        self.prepare();event=threading.Event();event.set()
        with patch.object(self.service,'read',side_effect=AssertionError('pre-cancel read')):result=self.run_restore(event)
        self.assertTrue(result['cancelled']);self.assertIsNone(result['operation_journal']);self.assertTrue(self.destination.exists())

    def test_stale_journal_or_state_requires_new_review_before_any_effect(self):
        self.prepare();value=json.loads(self.journal.read_bytes());value['phase']='partial'
        self.journal.write_text(json.dumps(value),encoding='utf-8')
        self.assertEqual(self.run_restore()['error'],'duplicate_recovery_journal_changed');self.assertTrue(self.destination.exists())
        review=self.audit();self.arguments['journal_fingerprint']=review['journal_fingerprint']
        self.sql('UPDATE files SET rating=4 WHERE id=?',(self.arguments['copy_id'],))
        self.assertEqual(self.run_restore()['error'],'duplicate_restore_requires_review');self.assertTrue(self.destination.exists())

    def test_new_source_file_after_preparation_is_preserved_without_overwrite(self):
        source,_=self.prepare()
        def race(path,value):
            write_journal(path,value)
            if value['phase']=='prepared':source.write_bytes(b'new source occupant')
        with patch('modules.duplicate_finder.logic.recovery_restore.write_journal',race):result=self.run_restore()
        self.assertFalse(result['committed']);self.assertFalse(result['moved'])
        self.assertEqual(result['error'],'duplicate_restore_path_occupied')
        self.assertEqual(source.read_bytes(),b'new source occupant');self.assertEqual(self.destination.read_bytes(),b'same bytes')

    def test_destination_changed_after_preparation_is_preserved_and_not_reactivated(self):
        self.prepare()
        def race(path,value):
            write_journal(path,value)
            if value['phase']=='prepared':self.destination.write_bytes(b'changed recovered bytes')
        with patch('modules.duplicate_finder.logic.recovery_restore.write_journal',race):result=self.run_restore()
        self.assertEqual(result['error'],'duplicate_restore_bytes_changed');self.assertFalse(result['committed'])
        self.assertFalse(self.source.exists());self.assertEqual(self.destination.read_bytes(),b'changed recovered bytes')

    def test_prepared_journal_failure_prevents_all_effects(self):
        self.prepare()
        with patch('modules.duplicate_finder.logic.recovery_restore.write_journal',side_effect=OSError('full fixture')):result=self.run_restore()
        self.assertEqual(result['error'],'duplicate_restore_io_failed');self.assertFalse(result['committed'])
        self.assertFalse(result['moved']);self.assertTrue(self.destination.exists());self.assertFalse(self.source.exists())

    def test_owner_rejects_curation_race_after_move_preserving_bytes_and_changed_curation(self):
        self.prepare();submit=self.service.submit
        def race(operation,*args,**kwargs):
            if operation=='restore_duplicate_copy':self.sql('UPDATE files SET meta_positive=? WHERE id=?',('new documented prompt',self.arguments['copy_id']))
            return submit(operation,*args,**kwargs)
        with patch.object(self.service,'submit',race):result=self.run_restore()
        self.assertTrue(result['moved']);self.assertFalse(result['committed']);self.assertFalse(result['complete'])
        self.assertIn('copy_version_changed_since_review',result['error'])
        self.assertEqual(self.source.read_bytes(),b'same bytes');self.assertFalse(self.destination.exists())
        after=self.service.read('get_copy_version_state',self.arguments['copy_id'])
        self.assertEqual(after['state']['file']['is_offline'],1);self.assertEqual(after['state']['file']['meta_positive'],'new documented prompt')
        self.assertEqual(json.loads(Path(result['operation_journal']).read_bytes())['phase'],'partial')

    def test_unconfirmed_future_after_move_has_recoverable_source_and_retry_restores_same_id(self):
        self.prepare();failed=Future();failed.set_result(False)
        with patch.object(self.service,'submit',return_value=failed):result=self.run_restore()
        self.assertEqual(result['error'],'duplicate_restore_commit_unconfirmed');self.assertTrue(result['moved']);self.assertFalse(result['committed'])
        self.assertEqual(self.audit()['moves'][0]['observed'],'archived_source_present')
        again=self.run_restore();self.assertTrue(again['complete']);self.assertTrue(again['committed']);self.assertFalse(again['moved'])
        self.assertEqual(again['copy_id'],result['copy_id']);self.assertNotEqual(again['operation_journal'],result['operation_journal'])
        self.assertTrue(Path(result['operation_journal']).exists())

    def test_cancel_after_move_finishes_commit_and_does_not_hide_admitted_failure(self):
        self.prepare();event=threading.Event()
        def after_move(path,value):
            write_journal(path,value)
            if value['phase']=='moved':event.set()
        with patch('modules.duplicate_finder.logic.recovery_restore.write_journal',after_move):result=self.run_restore(event)
        self.assertTrue(result['committed']);self.assertTrue(result['complete']);self.assertFalse(result['cancelled'])
        source,_=self.prepare_second_pair()
        event.clear();failed=Future();failed.set_result(False)
        with patch('modules.duplicate_finder.logic.recovery_restore.write_journal',after_move),patch.object(self.service,'submit',return_value=failed):result=self.run_restore(event)
        self.assertTrue(result['moved']);self.assertFalse(result['cancelled']);self.assertEqual(result['error'],'duplicate_restore_commit_unconfirmed')
        self.assertEqual(source.read_bytes(),b'new fixture bytes')

    def prepare_second_pair(self):
        source=self.file('C/raw.png',data=b'new fixture bytes')
        keeper=self.file('Y/curated.png',data=b'new fixture bytes',rating=5,tags=['documented'])
        self.retire(source);review=self.audit();copy=next(copy for copy in review['copies'] if copy['source']==source.as_posix())
        self.arguments=dict(journal=str(self.journal),copy_id=copy['copy_id'],journal_fingerprint=review['journal_fingerprint'],expected_fingerprint=copy['fingerprint'])
        return source,keeper

    def test_final_journal_failure_does_not_claim_unconfirmed_db_and_keeps_commit_visible(self):
        self.prepare()
        def failed_final(path,value):
            if value['phase'] in ('complete','partial'):raise OSError('full final journal fixture')
            write_journal(path,value)
        with patch('modules.duplicate_finder.logic.recovery_restore.write_journal',failed_final):result=self.run_restore()
        self.assertTrue(result['moved']);self.assertTrue(result['committed']);self.assertFalse(result['complete'])
        self.assertEqual(result['error'],'duplicate_restore_io_failed')
        self.assertEqual(self.service.read('get_copy_version_state',self.arguments['copy_id'])['state']['file']['is_offline'],0)
        self.assertTrue(Path(result['operation_journal']).exists())

    def test_worker_captures_args_and_publishes_terminal_once_without_gui_wait(self):
        self.prepare();arguments=deepcopy(self.arguments)
        worker=RecoveryRestoreWorker(self.service,token=91,**arguments);arguments['copy_id']=999
        terminal,native=[],[];worker.finished_signal.connect(terminal.append);worker.finished.connect(lambda:native.append(True))
        try:
            worker.start();self.assertTrue(pump(lambda:native));self.assertEqual(len(terminal),1)
            self.assertTrue(terminal[0]['committed']);self.assertEqual(terminal[0]['token'],91)
        finally:worker.wait(3000);worker.deleteLater();QApplication.processEvents()


class Native(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.digest=hashlib.sha256(b'bytes').hexdigest()
        self.conn.executescript('''CREATE TABLE panopticon_schema(name TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT,is_offline INTEGER,hash_original TEXT,hash_mod TEXT,prompt TEXT);
          CREATE TABLE tags(id INTEGER PRIMARY KEY,name TEXT,category TEXT);CREATE TABLE file_tags(file_id INTEGER,tag_id INTEGER,source TEXT);''')
        self.conn.execute('INSERT INTO files VALUES(1,?,1,?,NULL,?)',('C:/collection/niÑO.png',self.digest,'documented generation'))

    def tearDown(self):self.conn.close()

    def command(self,**kwargs):
        return restore(self.conn,1,expected_fingerprint=kwargs.get('fingerprint',state(self.conn,1)['fingerprint']),
            path=kwargs.get('path','C:/collection/niÑO.png'),digest=kwargs.get('digest',self.digest),
            normalize=lambda path:ntpath.normpath(path).replace('\\','/'))

    def test_only_online_flag_changes_and_no_ddl_is_persisted(self):
        before=state(self.conn,1)['state'];trace=[];self.conn.set_trace_callback(trace.append)
        self.assertTrue(self.command());after=state(self.conn,1)['state'];before['file']['is_offline']=0
        self.assertEqual(after,before);self.assertFalse(any(query.startswith(('INSERT','DELETE','CREATE')) for query in trace))

    def test_case_unicode_alias_blocks_even_if_native_path_index_would_allow_it(self):
        self.conn.execute('INSERT INTO files VALUES(2,?,0,?,NULL,NULL)',('c:/COLLECTION/NIño.PNG',self.digest))
        with self.assertRaisesRegex(ValueError,'duplicate_restore_path_occupied'):self.command()
        self.assertEqual(state(self.conn,1)['state']['file']['is_offline'],1)

    def test_missing_sha_or_wrong_digest_or_path_or_active_id_cannot_create_new_version(self):
        for values in (dict(digest='f'*64),dict(path='C:/elsewhere/niÑO.png'),dict(fingerprint='f'*64)):
            with self.assertRaises(ValueError):self.command(**values)
        self.conn.execute('UPDATE files SET hash_original=NULL WHERE id=1')
        with self.assertRaisesRegex(ValueError,'digest_mismatch'):self.command()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM files').fetchone()[0],1)

    def test_collision_read_budget_clears_handler_and_rejects_before_mutation(self):
        self.conn.executemany('INSERT INTO files VALUES(?, ?,0,?,NULL,NULL)',((index,f'C:/other/{index}.png',self.digest) for index in range(2,5000)))
        with patch('core.duplicate_restore.MAX_CHECK_SECONDS',0):
            with self.assertRaisesRegex(ValueError,'check_budget_exceeded'):self.command()
        self.assertEqual(state(self.conn,1)['state']['file']['is_offline'],1)
        self.assertTrue(self.command())


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('Restore.'+name,sys.modules[__name__]) for name in Restore.__dict__ if name.startswith('test_'))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(Native))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
