"""Diarios reales interrumpidos en fixtures; observación sin SQL/FS mutaciones."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from core.duplicate_policy import DuplicatePolicy
from core.duplicate_snapshot import recovery_states
from modules.duplicate_finder.logic.retirement import DuplicateRetirementTasks,_write_journal
from modules.duplicate_finder.logic.recovery_review import RecoveryReview,read_journal,_physical,MAX_JOURNAL
from modules.duplicate_finder.logic.recovery_worker import RecoveryReviewWorker
from curation_snapshot_selftest import Fixture,pump
import duplicate_exact_review_selftest as prior


class Recovery(Fixture):
    file,pair,scan=prior.Review.file,prior.Review.pair,prior.Review.scan

    def setUp(self):
        super().setUp()
        self.root=(Path(self.temp.name)/'collection').resolve();self.root.mkdir()
        self.recovery=(Path(self.temp.name)/'recovery').resolve()

    def retire(self,*paths,failure=False):
        review=self.scan()
        review.update(mode='hash',folder=str(self.root),policy=asdict(DuplicatePolicy()),token=1,cancelled=False,error=None)
        tasks=DuplicateRetirementTasks(self.service)
        arguments=dict(review=review,selected=[path.as_posix() for path in paths],recovery_folder=str(self.recovery))
        if failure:
            with patch('modules.duplicate_finder.logic.retirement.os.rename',side_effect=OSError('fixture interrupted')):
                result=tasks.read('retire_duplicates',**arguments)
        else:result=tasks.read('retire_duplicates',**arguments)
        self.journal=Path(result['groups'][0]['journal'])
        self.value=json.loads(self.journal.read_bytes())
        return result

    def audit(self):return RecoveryReview(self.service,str(self.journal)).run()

    def test_complete_retirement_is_verified_without_any_writes_or_receipt_changes(self):
        raw,keeper=self.pair()
        receipt=self.root/'catalog.db';receipt.write_bytes(b'Cherry hashes/URL receipts')
        self.retire(raw)
        original=self.journal.read_bytes()
        with patch.object(self.service,'submit',side_effect=AssertionError('audit writes')):
            result=self.audit()
        self.assertTrue(result['complete']);self.assertFalse(result['review_required'])
        self.assertEqual((result['verified_retired'],result['moves'][0]['observed']),(1,'retired_verified'))
        self.assertEqual(original,self.journal.read_bytes())
        self.assertEqual(receipt.read_bytes(),b'Cherry hashes/URL receipts')
        self.assertEqual(keeper.read_bytes(),b'same bytes');self.assertFalse(raw.exists())

    def test_stale_prepared_phase_and_pending_status_do_not_hide_completed_rename(self):
        raw,_=self.pair();self.retire(raw)
        self.value.update(phase='prepared');self.value['moves'][0]['status']='pending'
        _write_journal(self.journal.parent,self.value)
        result=self.audit()
        self.assertEqual((result['phase'],result['moves'][0]['recorded']),('prepared','pending'))
        self.assertEqual(result['moves'][0]['observed'],'retired_verified')

    def test_commit_without_move_detected_even_if_phase_claims_complete(self):
        raw,_=self.pair();self.retire(raw,failure=True)
        self.value.update(phase='complete');self.value['moves'][0]['status']='moved'
        _write_journal(self.journal.parent,self.value)
        result=self.audit();self.assertTrue(result['complete'])
        self.assertEqual(result['moves'][0]['observed'],'archived_source_present')
        self.assertTrue(result['review_required']);self.assertTrue(raw.exists())

    def test_prepared_without_commit_recognizes_source_active(self):
        raw,_=self.pair()
        with patch.object(self.service,'submit',side_effect=ValueError('before acceptance')):
            self.retire(raw)
        result=self.audit()
        self.assertEqual(result['moves'][0]['observed'],'not_retired')
        self.assertEqual(self.service.read('count_files'),2)

    def test_partial_group_has_per_copy_observations_not_blanket_success(self):
        raw,_=self.pair();extra=self.file('B/raw.png')
        self.retire(raw,extra)
        move=self.value['moves'][1]
        Path(move['destination']).rename(move['source'])
        result=self.audit()
        self.assertEqual([move['observed'] for move in result['moves']],['retired_verified','archived_source_present'])
        self.assertEqual(result['verified_retired'],1);self.assertTrue(result['review_required'])

    def test_new_active_version_at_source_is_visible_and_never_inherits_old_identity(self):
        raw,_=self.pair();self.retire(raw)
        self.file('A/raw.png',data=b'new content')
        from core.copy_version_tasks import CopyVersionTasks
        key=next(copy['state']['file']['id'] for copy in self.value['copies'] if copy['path']==raw.as_posix())
        current=self.service.read('get_copy_version_state',key)
        CopyVersionTasks(self.service).read('make_copy_online',copy_id=key,expected_fingerprint=current['fingerprint'])
        result=self.audit();copy=next(copy for copy in result['copies'] if copy['source']==raw.as_posix())
        self.assertTrue(copy['occupied']);self.assertNotEqual(copy['active_copy_id'],copy['copy_id'])
        self.assertEqual(result['moves'][0]['observed'],'requires_review')
        self.assertEqual(raw.read_bytes(),b'new content')

    def test_identical_replacement_destination_requires_identity_review(self):
        raw,_=self.pair();self.retire(raw)
        dest=Path(self.value['moves'][0]['destination'])
        replacement=dest.with_suffix('.replacement');replacement.write_bytes(dest.read_bytes())
        dest.unlink();replacement.rename(dest)
        result=self.audit()
        self.assertEqual(result['moves'][0]['destination_state']['status'],'matches')
        self.assertFalse(result['moves'][0]['destination_state']['same_identity'])
        self.assertEqual(result['moves'][0]['observed'],'requires_review')

    def test_changed_destination_or_keeper_curation_or_documentation_requires_review(self):
        raw,keeper=self.pair();self.retire(raw)
        dest=Path(self.value['moves'][0]['destination']);dest.write_bytes(b'corrupt')
        self.assertEqual(self.audit()['moves'][0]['observed'],'requires_review')
        self.service.submit('edit_curation',keeper.as_posix(),add_tags=['new curation']).result(3)
        result=self.audit();self.assertEqual(result['moves'][0]['observed'],'requires_review')
        copy=next(copy for copy in result['copies'] if copy['source']==keeper.as_posix())
        self.assertEqual(copy['native'],'changed')
        keeper.with_suffix('.txt').write_bytes(b'caption')
        self.assertTrue(next(copy for copy in self.audit()['copies'] if copy['source']==keeper.as_posix())['documented'])

    def test_disconnected_parent_is_unavailable_not_absence(self):
        raw,_=self.pair();self.retire(raw)
        native_stat=Path.stat
        def unavailable(path,*args,**kwargs):
            if path in (raw,raw.parent):raise FileNotFoundError('disconnected fixture')
            return native_stat(path,*args,**kwargs)
        with patch.object(Path,'stat',unavailable):result=self.audit()
        copy=next(copy for copy in result['copies'] if copy['source']==raw.as_posix())
        self.assertEqual(copy['physical']['status'],'unavailable')
        self.assertEqual(result['moves'][0]['observed'],'requires_review')

    def test_invalid_or_escaping_or_duplicate_json_or_oversized_journal_rejects_all(self):
        raw,_=self.pair();self.retire(raw)
        original=deepcopy(self.value)
        bad=[]
        for key,value in [('version',True),('version',2),('copies',original['copies'][:1]),('digest','bogus')]:
            item=deepcopy(original);item[key]=value;bad.append(json.dumps(item).encode())
        item=deepcopy(original);item['moves'][0]['destination']=(self.recovery/'escaped.png').as_posix();bad.append(json.dumps(item).encode())
        item=deepcopy(original);item['approved'][0]['copy_id']+=100;bad.append(json.dumps(item).encode())
        bad += [b'{"version":1,"version":1}',b'['*1500,b' '* (MAX_JOURNAL+1)]
        for raw in bad:
            with self.subTest(size=len(raw)):
                self.journal.write_bytes(raw);result=self.audit()
                self.assertFalse(result['complete']);self.assertEqual(result['moves'],[]);self.assertTrue(result['error'])

    def test_journal_changed_during_hash_drops_entire_result(self):
        raw,_=self.pair();self.retire(raw)
        lock=threading.Lock();changed=False
        def race(*args):
            nonlocal changed
            with lock:
                if not changed:
                    changed=True;self.value['phase']='partial';_write_journal(self.journal.parent,self.value)
            return _physical(*args)
        with patch('modules.duplicate_finder.logic.recovery_review._physical',race):result=self.audit()
        self.assertEqual(result['error'],'duplicate_recovery_journal_changed');self.assertEqual(result['copies'],[])

    def test_missing_or_truncated_native_state_is_not_approved(self):
        raw,_=self.pair();self.retire(raw)
        with patch.object(self.service,'read',return_value=[]):result=self.audit()
        self.assertEqual(result['error'],'duplicate_recovery_native_incomplete');self.assertEqual(result['moves'],[])

    def test_pre_cancel_and_mid_worker_cancel_have_single_terminal_no_partial_review(self):
        raw,_=self.pair();self.retire(raw)
        event=threading.Event();event.set()
        result=RecoveryReview(self.service,str(self.journal)).run(event)
        self.assertTrue(result['cancelled']);self.assertFalse(result['complete'])
        entered,release=threading.Event(),threading.Event();terminals=[];native=[];ticks=[]
        from PySide6.QtCore import QTimer
        timer=QTimer();timer.timeout.connect(lambda:ticks.append(1));timer.start(1)
        def delayed(*args):entered.set();release.wait(3);return _physical(*args)
        worker=RecoveryReviewWorker(self.service,str(self.journal),token=44)
        worker.finished_signal.connect(terminals.append);worker.finished.connect(lambda:native.append(1))
        try:
            with patch('modules.duplicate_finder.logic.recovery_review._physical',delayed):
                worker.start();self.assertTrue(pump(entered.is_set));worker.stop();self.assertTrue(pump(lambda:len(ticks)>2));release.set()
                self.assertTrue(pump(lambda:len(native)==1))
            self.assertEqual(len(terminals),1);self.assertEqual(terminals[0]['token'],44)
            self.assertTrue(terminals[0]['cancelled']);self.assertEqual(terminals[0]['moves'],[])
        finally:release.set();worker.wait(3000);timer.stop();worker.deleteLater();QApplication.processEvents()


class Native(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.conn.executescript('''CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT,is_offline INTEGER,prompt TEXT);
          CREATE TABLE tags(id INTEGER PRIMARY KEY,name TEXT,category TEXT);
          CREATE TABLE file_tags(file_id INTEGER,tag_id INTEGER,source TEXT);
          INSERT INTO files VALUES(1,'C:/collection/a.png',1,'original generation'),(2,'C:/collection/a.png',0,'new generation'),(3,'C:/collection/b.png',0,NULL);
          INSERT INTO tags VALUES(1,'curated','character');INSERT INTO file_tags VALUES(1,1,'manual');''')

    def tearDown(self):self.conn.close()

    def test_bounded_queries_preserve_order_full_history_tags_and_active_occupant(self):
        queries=[];self.conn.set_trace_callback(queries.append)
        result=recovery_states(self.conn,[3,1,999,2])
        self.assertEqual([row['copy_id'] for row in result],[3,1,999,2])
        self.assertEqual(result[1]['active_copy_id'],2)
        self.assertEqual(result[1]['state']['file']['prompt'],'original generation')
        self.assertEqual(result[1]['state']['tags'],[dict(name='curated',category='character',source='manual')])
        self.assertIsNone(result[2]['state'])
        self.assertEqual(sum(query.startswith('SELECT') for query in queries),4)
        self.assertFalse(any(query.startswith(('UPDATE','INSERT','DELETE','CREATE')) for query in queries))

    def test_id_shape_or_whole_payload_limit_rejects_not_prefix(self):
        for ids in ([],[True],[1,1],list(range(257)),[2**63]):
            with self.assertRaisesRegex(ValueError,'invalid_duplicate_recovery_ids'):recovery_states(self.conn,ids)
        self.conn.execute('UPDATE files SET prompt=? WHERE id=1',('a'*65537,))
        with self.assertRaisesRegex(ValueError,'payload_too_large'):recovery_states(self.conn,[3,1])


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
