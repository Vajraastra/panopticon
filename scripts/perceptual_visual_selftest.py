"""Cache visual validado por bytes, sólo revisión y sin persistencia implícita."""
from copy import deepcopy
import ntpath
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from PySide6.QtWidgets import QApplication
from core.perceptual_hash import states,prepare,store
from modules.duplicate_finder.logic.visual_review import scan
from modules.duplicate_finder.logic.review_worker import VisualReviewWorker
from modules.duplicate_finder.logic.perceptual_backfill import algorithm_id
import perceptual_backfill_selftest as prior

ALGO=prior.algorithm_id()
normalize=lambda path:ntpath.normpath(path).replace('\\','/')


class States(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.conn.execute('CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT,hash_original,hash_mod,phash,is_offline DEFAULT 0)')
        self.conn.execute('INSERT INTO files VALUES(1,?,?,NULL,NULL,0)',('E:/a.png','a'*64))
        self.addCleanup(self.conn.close)
    def test_order_duplicates_missing_and_legacy_unprepared_have_no_truncation_or_ddl(self):
        paths=['E:/missing.png','E:/a.png','E:/a.png']
        changes=self.conn.total_changes
        values=states(self.conn,paths,ALGO,normalize=normalize)
        self.assertEqual([row['path'] for row in values],paths)
        self.assertEqual([row['copy_id'] for row in values],[None,1,1])
        self.assertTrue(all(row['phash'] is None for row in values))
        self.assertEqual(self.conn.total_changes,changes)
    def test_proven_cache_not_returned_for_other_algorithm_offline_or_current_sha(self):
        prepare(self.conn)
        record=dict(copy_id=1,path='E:/a.png',source_sha256='a'*64,phash='a'*16)
        store(self.conn,[record],ALGO,normalize=normalize)
        self.assertEqual(states(self.conn,['E:/a.png'],ALGO,normalize=normalize)[0]['phash'],'a'*16)
        self.assertIsNone(states(self.conn,['E:/a.png'],ALGO+'/other',normalize=normalize)[0]['phash'])
        self.conn.execute('UPDATE files SET hash_mod=?',('b'*64,))
        self.assertIsNone(states(self.conn,['E:/a.png'],ALGO,normalize=normalize)[0]['phash'])
        self.conn.execute('UPDATE files SET is_offline=1')
        self.assertIsNone(states(self.conn,['E:/a.png'],ALGO,normalize=normalize)[0]['copy_id'])
    def test_invalid_unbounded_or_relative_requests_are_rejected(self):
        for paths in ([],['E:/a.png']*257,['relative.png'],['E:/a\x00.png']):
            with self.assertRaisesRegex(ValueError,'state_request'):states(self.conn,paths,ALGO,normalize=normalize)


class Visual(prior.Backfill):
    def scan(self):return scan(str(self.root),5,lambda *_:None,threading.Event(),service=self.service)

    def test_proven_cache_saves_decode_after_fresh_sha_and_stays_review_only_without_writes(self):
        self.pair_images()
        self.assertTrue(self.phase()['complete'])
        before=self.service.metrics_snapshot()['committed']
        with (patch.object(Image,'open',side_effect=AssertionError('cache decoded')),
              patch.object(self.service,'submit',side_effect=AssertionError('visual wrote'))):
            value=self.scan()
        self.assertTrue(value['complete'])
        self.assertEqual(len(value['groups']),1)
        self.assertTrue(all(report['remove']==() for report in value['reports'].values()))
        self.assertEqual(self.service.metrics_snapshot()['committed'],before)

    def test_changed_bytes_do_not_reuse_stale_cache_even_if_native_hash_has_not_changed(self):
        self.pair_images()
        self.assertTrue(self.phase()['complete'])
        self.image('raw.png',seed=9,register=False)
        with patch.object(Image,'open',side_effect=AssertionError('stale cache decoded')):value=self.scan()
        self.assertFalse(value['complete'])
        self.assertEqual(value['issues'][0]['reason'],'catalog_identity_mismatch')
        self.assertEqual(value['groups'],{})

    def test_no_proven_cache_uses_fresh_visual_hash_without_implicit_schema_or_writes(self):
        self.pair_images()
        before=self.service.metrics_snapshot()['committed']
        value=self.scan()
        self.assertTrue(value['complete'])
        self.assertEqual(len(value['groups']),1)
        self.assertFalse(self.service.read('get_phash_registry_status')['prepared'])
        self.assertEqual(self.service.metrics_snapshot()['committed'],before)

    def test_truncated_or_reordered_cache_state_is_never_used_as_valid_prefix(self):
        self.pair_images()
        native=self.service.read
        for change in (lambda values:values[:1],lambda values:list(reversed(values))):
            def read(operation,*args,**kwargs):
                value=native(operation,*args,**kwargs)
                return change(value) if operation=='get_phash_states' else value
            with patch.object(self.service,'read',read):
                with self.assertRaisesRegex(ValueError,'state_identity_changed'):self.scan()

    def test_visual_worker_uses_cache_off_gui_and_publishes_terminal_without_retirement(self):
        self.pair_images()
        self.assertTrue(self.phase()['complete'])
        worker=VisualReviewWorker(str(self.root),5,service=self.service,token=19)
        receiver,native=self.observe(worker)
        threads=[]
        read=self.service.read
        def traced(*args,**kwargs):
            threads.append(threading.get_ident())
            return read(*args,**kwargs)
        with (patch.object(self.service,'read',traced),patch.object(Image,'open',side_effect=AssertionError('cache decoded'))):
            worker.start()
            value=self.finish(receiver,native)
        self.assertEqual((value['mode'],value['token']),('visual',19))
        self.assertTrue(value['complete'])
        self.assertTrue(all(report['remove']==() for report in value['reports'].values()))
        self.assertNotIn(threading.get_ident(),threads)


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(States))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromName('Visual.'+name,sys.modules[__name__])
        for name in Visual.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
