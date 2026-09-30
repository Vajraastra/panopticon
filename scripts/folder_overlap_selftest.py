"""Conjuntos completos y cobertura, sin leer imágenes ni tocar catálogos Cherry."""
from contextlib import closing
import json
import ntpath
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.folder_overlap import compare
from core.library_repository import LibraryRepository
from core.library_connections import open_reader
from core.library_service import LibraryError
import duplicate_exact_review_selftest as helpers

A,B,C='a'*64,'b'*64,'c'*64
normalize=lambda value:ntpath.normpath(value).replace('\\','/')


class Queries(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.conn.execute('CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT,hash_original,hash_mod,is_offline DEFAULT 0)')
        self.addCleanup(self.conn.close)
        self.counter=0
    def row(self,folder,digest=A,*,modified=None,offline=0):
        self.counter+=1
        self.conn.execute('INSERT INTO files VALUES(?,?,?,?,?)',
            (self.counter,f'{folder}/{self.counter}.png',digest,modified,offline))
    def compare(self,folders=('E:/left','E:/right'),**kwargs):
        return compare(self.conn,list(folders),normalize=normalize,**kwargs)

    def test_unique_sets_ignore_copy_multiplicity_and_report_coverage(self):
        for digest in (A,A,B,None):self.row('E:/left',digest)
        for digest in (A,C):self.row('E:/right',digest)
        value=self.compare()
        self.assertEqual(value['folders'][0],dict(folder='E:/left',active_copies=4,
            hashed_copies=3,unknown_copies=1,unique_hashes=2))
        pair=value['pairs'][0]
        self.assertEqual((pair['shared_hashes'],pair['union_hashes']),(1,3))
        self.assertAlmostEqual(pair['jaccard'],1/3)
        self.assertEqual((pair['first_coverage'],pair['second_coverage']),(.5,.5))
        self.assertEqual(pair['classification'],'partial_overlap')
        self.assertNotIn('remove',json.dumps(value))
        self.assertNotIn('auto_merge',json.dumps(value))

    def test_threshold_applies_to_jaccard_not_directional_containment(self):
        self.row('E:/left',A)
        for digest in (A,B,C):self.row('E:/right',digest)
        pair=self.compare()['pairs'][0]
        self.assertEqual(pair['first_coverage'],1)
        self.assertEqual(pair['classification'],'partial_overlap')
        self.assertEqual(self.compare(threshold=33)['pairs'][0]['classification'],'high_overlap')

    def test_threshold_boundary_and_disjoint_zero_threshold(self):
        for folder in ('E:/left','E:/right'):self.row(folder,A)
        self.row('E:/right',B)
        self.assertEqual(self.compare(threshold=50)['pairs'][0]['classification'],'high_overlap')
        self.assertEqual(self.compare(threshold=51)['pairs'][0]['classification'],'partial_overlap')
        self.conn.execute('DELETE FROM files')
        self.row('E:/left',A)
        self.row('E:/right',B)
        self.assertEqual(self.compare(threshold=0)['pairs'][0]['classification'],'disjoint')

    def test_empty_and_unknown_folders_are_insufficient_not_identical(self):
        self.assertIsNone(self.compare()['pairs'][0]['jaccard'])
        self.row('E:/left',A)
        self.row('E:/right',None)
        pair=self.compare()['pairs'][0]
        self.assertEqual(pair['jaccard'],0)
        self.assertIsNone(pair['second_coverage'])
        self.assertEqual(pair['classification'],'insufficient_hashes')

    def test_effective_hash_mod_empty_fallback_invalid_and_blob_excluded(self):
        self.row('E:/left',A,modified=B.upper())
        self.row('E:/right',B,modified='')
        for digest in ('bad','f'*63,'x'*64,b'a'*64,17):self.row('E:/right',digest)
        value=self.compare()
        self.assertEqual(value['pairs'][0]['jaccard'],1)
        self.assertEqual(value['folders'][1]['unknown_copies'],5)

    def test_offline_history_is_excluded_without_deleting_it(self):
        self.row('E:/left',A)
        self.row('E:/right',B)
        self.row('E:/right',A,offline=1)
        before=list(self.conn.execute('SELECT * FROM files'))
        changes=self.conn.total_changes
        self.assertEqual(self.compare()['pairs'][0]['shared_hashes'],0)
        self.assertEqual(list(self.conn.execute('SELECT * FROM files')),before)
        self.assertEqual(self.conn.total_changes,changes)

    def test_literal_percent_underscore_backslash_and_sibling_prefix(self):
        self.row('E:/literal%_folder',A)
        self.row('E:/literalXYfolder',B)
        self.row('E:/literal%_folderSibling',B)
        self.row('E:/right',A)
        value=self.compare((r'E:\literal%_folder','E:/right'))
        self.assertEqual(value['folders'][0]['active_copies'],1)
        self.assertEqual(value['pairs'][0]['jaccard'],1)

    def test_all_pairs_are_classified_once_and_no_pairs_truncated(self):
        folders=[f'E:/profile-{i}' for i in range(32)]
        for folder in folders:self.row(folder,A)
        value=self.compare(folders)
        self.assertEqual(len(value['pairs']),496)
        self.assertEqual({(p['first'],p['second']) for p in value['pairs']},
            {(a,b) for a in range(32) for b in range(a+1,32)})
        self.assertTrue(all(p['jaccard']==1 for p in value['pairs']))

    def test_nested_equal_case_alias_scopes_reject_before_sql(self):
        for folders in [('E:/left','e:/LEFT/'),('E:/left','E:/left/subdir'),('E:/left/subdir','E:/left')]:
            with self.assertRaisesRegex(ValueError,'nested_scope'):self.compare(folders)
        self.assertEqual(self.compare(('E:/left','E:/leftovers'))['folders'][0]['active_copies'],0)

    def test_invalid_scope_and_threshold_fail_before_sql(self):
        for folders in [[],['E:/one'],['relative','E:/two'],['E:relative','E:/two'],
            ['E:/one\x00','E:/two'],['E:/'+str(i) for i in range(33)]]:
            with self.assertRaisesRegex(ValueError,'invalid_folder_overlap_scope'):self.compare(folders)
        for threshold in (True,-1,101,.5,'51'):
            with self.assertRaisesRegex(ValueError,'threshold'):self.compare(threshold=threshold)

    def test_copy_budget_rejects_all_results_instead_of_returning_prefix(self):
        for _ in range(4):self.row('E:/left',A)
        self.row('E:/right',A)
        with patch('core.folder_overlap.MAX_ACTIVE_COPIES',3):
            with self.assertRaisesRegex(ValueError,'copy_budget_exceeded'):self.compare()

    def test_one_sql_statement_and_no_filesystem_or_image_reads(self):
        self.row('E:/left',A)
        self.row('E:/right',A)
        queries=[]
        self.conn.set_trace_callback(queries.append)
        with patch.object(Path,'stat',side_effect=AssertionError('filesystem observed')):
            self.compare()
        self.assertEqual(len(queries),1)


class Service(helpers.Review):
    def folders(self):return [(self.root/name).as_posix() for name in ('A','Z')]
    def test_service_is_read_only_and_does_not_create_index_or_touch_cherry(self):
        raw,curated=self.pair()
        receipt=raw.parent/'catalog.db'
        receipt.write_bytes(b'opaque Cherry catalog')
        before=self.service.read('get_duplicate_snapshot',[raw.as_posix(),curated.as_posix()])
        metrics=self.service.metrics_snapshot()
        with (patch.object(self.service,'submit',side_effect=AssertionError('write admitted')),
              patch.object(Path,'stat',side_effect=AssertionError('filesystem observed'))):
            value=self.service.read('get_folder_overlap',self.folders())
        self.assertEqual(value['pairs'][0]['jaccard'],1)
        self.assertEqual(self.service.read('get_duplicate_snapshot',[raw.as_posix(),curated.as_posix()]),before)
        self.assertFalse(self.service.read('get_duplicate_search_status')['prepared'])
        self.assertEqual(receipt.read_bytes(),b'opaque Cherry catalog')
        self.assertEqual(self.service.metrics_snapshot()['committed'],metrics['committed'])

    def test_native_ro_connection_supports_report_without_schema_changes(self):
        self.pair()
        with closing(open_reader(Path(self.temp.name)/'fixture.db')) as conn:
            repo=LibraryRepository(conn)
            self.assertEqual(repo.get_folder_overlap(self.folders())['pairs'][0]['jaccard'],1)
            with self.assertRaises(sqlite3.OperationalError):conn.execute('DELETE FROM files')

    def test_pre_cancelled_read_and_time_budget_release_reader_and_keep_writer_usable(self):
        self.pair()
        event=threading.Event()
        event.set()
        with self.assertRaisesRegex(LibraryError,'cancelled'):
            self.service.read('get_folder_overlap',self.folders(),cancel_event=event)
        for i in range(100):self.file(f'A/{i}.png')
        with patch.dict('core.library_service.READ_BUDGET_SECONDS',get_folder_overlap=0):
            with self.assertRaisesRegex(LibraryError,'discovery_budget_exceeded'):
                self.service.read('get_folder_overlap',self.folders())
        self.service.submit('edit_curation',(self.root/'A/raw.png').as_posix(),rating=2).result(3)
        self.assertEqual(self.service.read('get_folder_overlap',self.folders())['pairs'][0]['jaccard'],1)

    def test_report_uses_one_wal_snapshot_while_writer_commits(self):
        self.pair()
        native=LibraryRepository.get_folder_overlap
        committed,failures=[],[]
        def intercept(repo,*args,**kwargs):
            def lower(value):
                if not committed and not failures:
                    try:
                        self.file('A/new.png',data=b'new hash')
                        committed.append(True)
                    except Exception as error:failures.append(error)
                return value.lower() if isinstance(value,str) else value
            repo.conn.create_function('lower',1,lower)
            return native(repo,*args,**kwargs)
        with patch.object(LibraryRepository,'get_folder_overlap',intercept):
            value=self.service.read('get_folder_overlap',self.folders())
        self.assertEqual(failures,[])
        self.assertEqual(committed,[True])
        self.assertEqual(value['pairs'][0]['jaccard'],1)
        self.assertEqual(self.service.read('get_folder_overlap',self.folders())['pairs'][0]['jaccard'],.5)


if __name__=='__main__':
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(Queries))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromName('Service.'+name,sys.modules[__name__])
        for name in Service.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
