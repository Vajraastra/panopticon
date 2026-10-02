"""Private Windows scopes for folder overlap and explicit pHash backfill."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import hashlib, os, sqlite3, sys, tempfile, threading, unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from core.library_service import LibraryService, LibraryError
from core.library_connections import open_reader
from core.library_repository import LibraryRepository
from core.path_identity import observe_path
from modules.duplicate_finder.logic.database_worker import NativeDuplicateQueryWorker
from modules.duplicate_finder.logic.perceptual_backfill import PerceptualBackfill, algorithm_id

A,B='a'*64,'b'*64


class ReviewScopes(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.service=LibraryService(self.root/'private.db')
        self.addCleanup(self.service.close)
        self.event=threading.Event()

    def file(self,name,digest=A):
        path=self.root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(b'private')
        self.service.submit('register_files_minimal',[dict(path=str(path),filename=path.name,size=7,created=1)]).result(3)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('UPDATE files SET hash_original=? WHERE path=?',(digest,path.as_posix())); conn.commit()
        return path

    def prepare(self): self.service.backfill_path_identity()

    def read(self,operation,folders,**options):
        args=() if operation=='get_folder_overlap' else (algorithm_id(),)
        return self.service.read_worker(operation,folders,*args,cancel_event=self.event,**options)

    def test_unicode_alias_overlap_and_phash_choose_existing_folder(self):
        first=self.file('Árbol/One.png'); self.file('Right/Two.png'); self.file('ÁrbolOther/Other.png',B)
        self.service.submit('edit_curation',str(first),rating=5,add_tags=['keep']).result(3)
        before=self.service.read('get_duplicate_snapshot',[first.as_posix()]); self.prepare()
        folders=[str(self.root/'árbol'),str(self.root/'Right')]
        value=self.read('get_folder_overlap',folders)
        self.assertEqual([r['active_copies'] for r in value['folders']],[1,1])
        self.assertEqual(value['pairs'][0]['jaccard'],1)
        self.assertEqual(self.read('get_phash_page',folders[:1])['records'][0]['path'],first.as_posix())
        self.assertEqual(self.service.read('get_duplicate_snapshot',[first.as_posix()]),before)

    def test_literal_wildcards_and_offline_history_do_not_inflate_counts(self):
        first=self.file('100%_Á/One.png'); self.file('100ZZÁ/Other.png',B); self.file('Right/Two.png')
        old=self.file('100%_Á/Old.png',B); self.prepare()
        self.service.submit('remove_files',[old.as_posix()]).result(3)
        folders=[str(first.parent),str(self.root/'Right')]
        self.assertEqual([r['active_copies'] for r in self.read('get_folder_overlap',folders)['folders']],[1,1])
        self.assertEqual(self.read('get_phash_page',folders[:1])['total_images'],1)

    def test_case_sensitive_distinct_scopes_are_compared_separately(self):
        first=self.file('Root/One.png',A); second=self.root/'root'/'Two.png'
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('INSERT INTO files(path,filename,hash_original) VALUES(?,?,?)',(second.as_posix(),second.name,B)); conn.commit()
        def files(path,**kwargs):
            return replace(observe_path(str(first),**kwargs),path=path,canonical=path,
                physical=(1,0,1 if path==first.as_posix() else 2,0,7,0,0))
        with patch('core.library_path_identity.observe_path',side_effect=files): self.prepare()
        def folders(path,**kwargs):
            return replace(observe_path(str(self.root),**kwargs),path=path,canonical=path,kind='directory')
        scopes=[str(self.root/'Root'),str(self.root/'root')]
        with patch('core.folder_tasks.observe_path',side_effect=folders):
            value=self.read('get_folder_overlap',scopes)
            self.assertEqual([r['active_copies'] for r in value['folders']],[1,1])
            self.assertEqual(value['pairs'][0]['classification'],'disjoint')
            self.assertEqual(self.read('get_phash_page',scopes)['total_images'],2)

    def test_actual_aliases_and_nested_scopes_reject_without_partial_report(self):
        self.file('Árbol/One.png'); self.file('Árbol/Nested/Two.png'); self.prepare()
        for scopes in ([str(self.root/'Árbol'),str(self.root/'árbol')],
                       [str(self.root/'Árbol'),str(self.root/'árbol'/'Nested')]):
            for operation in ('get_folder_overlap','get_phash_page'):
                with self.subTest(scopes=scopes,operation=operation):
                    with self.assertRaisesRegex((ValueError,LibraryError),'nested'):
                        self.read(operation,scopes)

    def test_missing_or_file_scope_and_incomplete_registry_require_review(self):
        first=self.file('Left/One.png'); self.file('Right/Two.png'); self.prepare()
        for invalid in (str(first),str(self.root/'Absent')):
            with self.assertRaisesRegex((ValueError,LibraryError),'scope|unavailable'):
                self.read('get_folder_overlap',[invalid,str(self.root/'Right')])
        self.file('Left/New.png')
        with self.assertRaisesRegex((ValueError,LibraryError),'review_required'):
            self.read('get_phash_page',[str(first.parent)])

    def test_no_sql_snapshot_during_observations_and_no_native_or_cherry_writes(self):
        first=self.file('Left/One.png'); second=self.file('Right/Two.png'); self.prepare()
        receipt=first.parent/'catalog.db'; receipt.write_bytes(b'opaque')
        before=self.service.metrics_snapshot()['committed']; original=observe_path; calls=[]
        def observe(path,**kwargs):
            self.assertEqual(self.service.metrics_snapshot()['reads_active'],0)
            calls.append(path); return original(path,**kwargs)
        with patch('core.folder_tasks.observe_path',side_effect=observe), patch.object(self.service,'submit',side_effect=AssertionError('write')):
            self.read('get_folder_overlap',[str(first.parent),str(second.parent)])
            self.read('get_phash_page',[str(first.parent)])
        self.assertEqual(len(calls),6); self.assertEqual(receipt.read_bytes(),b'opaque')
        self.assertEqual(self.service.metrics_snapshot()['committed'],before)

    def test_directory_replaced_during_read_rejects_whole_result(self):
        first=self.file('Left/One.png'); self.file('Right/Two.png'); self.prepare()
        count=0
        def changed(path,**kwargs):
            nonlocal count
            count+=1; value=observe_path(path,**kwargs)
            if count>2: value=replace(value,physical=(value.physical[0],0,value.physical[2]+1,*value.physical[3:]))
            return value
        with patch('core.folder_tasks.observe_path',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'physical_state_changed'):
                self.read('get_folder_overlap',[str(first.parent),str(self.root/'Right')])

    def test_legacy_is_sql_only_and_has_no_implicit_migration(self):
        first=self.file('Left/One.png'); self.file('Right/Two.png')
        with patch('core.folder_tasks.observe_path',side_effect=AssertionError('legacy filesystem')):
            self.assertEqual(self.read('get_folder_overlap',[str(first.parent),str(self.root/'Right')])['pairs'][0]['jaccard'],1)
            self.assertEqual(self.read('get_phash_page',[str(first.parent)])['total_images'],1)
        self.assertFalse(self.service.read('get_path_identity_status')['prepared'])

    def test_prepared_overlap_and_phash_seek_canonical_prefix_index(self):
        first=self.file('Left/One.png'); second=self.file('Right/Two.png'); self.prepare()
        scopes=[str(first.parent),str(second.parent)]
        with closing(open_reader(self.service.db_path)) as conn:
            repo=LibraryRepository(conn)
            repo._folder_scope_context={o.path:dict(canonical=o.canonical,kind=o.kind,physical=o.physical)
                for o in [observe_path(folder) for folder in scopes]}
            queries=[]; conn.set_trace_callback(queries.append)
            repo.get_folder_overlap(scopes); repo.get_phash_page(scopes,algorithm_id())
            conn.set_trace_callback(None)
            target=[q for q in queries if 'FROM scopes' in q or 'canonical>=' in q and 'FROM files' in q]
            self.assertGreaterEqual(len(target),2)
            for query in target:
                plan=conn.execute('EXPLAIN QUERY PLAN '+query).fetchall()
                self.assertTrue(any('idx_path_identity_canonical' in row[3] and 'SEARCH' in row[3] for row in plan),plan)

    def test_phash_paging_preserves_frontier_and_complete_budget_rejection(self):
        first=self.file('Left/One.png'); second=self.file('Left/Two.png'); self.file('Right/Three.png'); self.prepare()
        scopes=[str(first.parent)]
        page=self.read('get_phash_page',scopes,limit=1); self.assertTrue(page['has_more'])
        upper=page['through_id']; self.file('Left/Late.png'); self.prepare()
        following=self.read('get_phash_page',scopes,limit=1,after_id=page['next_after_id'],through_id=upper)
        self.assertEqual([r['path'] for r in following['records']],[second.as_posix()]); self.assertFalse(following['has_more'])
        with patch('core.folder_overlap.MAX_ACTIVE_COPIES',2):
            with self.assertRaisesRegex((ValueError,LibraryError),'copy_budget'):
                self.read('get_folder_overlap',[str(first.parent),str(self.root/'Right')])

    def test_qt_overlap_worker_and_explicit_phash_use_observed_scope(self):
        from PIL import Image
        first=self.file('Árbol/One.png'); self.file('Right/Two.png')
        Image.new('RGB',(16,16),'red').save(first)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('UPDATE files SET hash_original=? WHERE path=?',(hashlib.sha256(first.read_bytes()).hexdigest(),first.as_posix())); conn.commit()
        self.prepare(); scopes=[str(self.root/'árbol'),str(self.root/'Right')]
        worker=NativeDuplicateQueryWorker(self.service,'get_folder_overlap',folders=scopes)
        delivered=[]; worker.finished_signal.connect(delivered.append); worker.run()
        self.assertIsNone(delivered[0]['error']); self.assertEqual(delivered[0]['value']['folders'][0]['active_copies'],1)
        result=PerceptualBackfill(self.service,scopes[:1]).run()
        self.assertTrue(result['complete'],result); self.assertEqual(result['persisted'],1)
        self.event.set()
        with self.assertRaisesRegex((ValueError,LibraryError),'cancelled'):
            self.read('get_phash_page',scopes[:1])


if __name__=='__main__': unittest.main()
