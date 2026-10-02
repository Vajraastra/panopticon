"""Private Windows folder scopes, aggregate albums and worker delivery."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import os, sqlite3, sys, tempfile, threading, time, unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from core.library_async import LibraryReader
from core.library_connections import open_reader
from core.library_repository import LibraryRepository
from core.library_service import LibraryService, LibraryError
from core.path_identity import observe_path
from core.folder_queries import condition


class Folders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.service=LibraryService(self.root/'private.db')
        self.addCleanup(self.service.close)
        self.event=threading.Event()

    def image(self,name, rating=0):
        path=self.root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(b'private')
        self.service.submit('register_files_minimal',[dict(path=str(path),filename=path.name,size=7,created=1)]).result(3)
        if rating:self.service.submit('edit_curation',str(path),rating=rating,add_tags=['keep']).result(3)
        return path

    def read(self,operation,*args,**options):
        return self.service.read_worker(operation,*args,cancel_event=self.event,**options)

    def prepare(self):self.service.backfill_path_identity()

    def test_unicode_alias_folder_count_preview_selection_and_search(self):
        path=self.image('Árbol/One.png',5); self.image('Árbol/Nested/Two.png',2); self.image('ÁrbolOther/Three.png')
        self.prepare(); alias=self.root/'árbol'
        self.assertEqual(self.read('get_folder_count',str(alias)),2)
        self.assertEqual(self.read('get_folder_preview',str(alias),limit=1),[path.as_posix()])
        page=self.read('get_library_page',folder=str(alias),limit=1,offset=0)
        self.assertEqual(page['count'],2); self.assertEqual(page['paths'],[path.as_posix()])
        count,rows=self.read('search_files_paginated',folder=str(alias),min_rating=4,tags=['keep'])
        self.assertEqual((count,rows),(1,[(path.as_posix(),5)]))
        self.assertEqual(self.read('resolve_library_selection',folder=str(alias))['count'],2)

    def test_prefix_treats_percent_underscore_and_unicode_as_literal_names(self):
        wanted=self.image('100%_Á/One.png'); self.image('100ZZa/Other.png'); self.prepare()
        self.assertEqual(self.read('get_files_recursive',str(wanted.parent)),[wanted.as_posix()])
        self.assertEqual(self.read('get_known_files_in_folder',str(wanted.parent)),{wanted.as_posix()})

    def test_file_query_is_exact_without_including_similarly_named_folder(self):
        path=self.image('One.png',5); self.image('One.png.folder/Other.png'); self.prepare()
        self.assertEqual(self.read('search_files_paginated',query='path:'+str(path)+' rating:5'),(1,[(path.as_posix(),5)]))
        self.assertEqual(self.read('get_folder_count',str(path)),0)

    def test_sensitive_directory_scope_does_not_mix_case_distinct_paths(self):
        first=self.image('Root/One.png'); second=self.root/'root'/'Other.png'
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('INSERT INTO files(path,filename) VALUES(?,?)',(second.as_posix(),second.name)); conn.commit()
        def observation(path,**kwargs):
            value=observe_path(str(first),**kwargs)
            return replace(value,path=path,canonical=path,kind='file',physical=(1,0,1 if path==first.as_posix() else 2,0,7,0,0))
        with patch('core.library_path_identity.observe_path',side_effect=observation):self.prepare()
        folder=(self.root/'root').as_posix()
        def directory(path,**kwargs):
            return replace(observe_path(str(self.root),**kwargs),path=path,canonical=path,kind='directory')
        with patch('core.folder_tasks.observe_path',side_effect=directory):
            self.assertEqual(self.read('get_files_recursive',folder),[second.as_posix()])

    def test_legacy_scope_keeps_prior_rules_without_probes_or_schema(self):
        path=self.image('Árbol/One.png')
        with patch('core.folder_tasks.observe_path',side_effect=AssertionError('implicit preparation')):
            self.assertEqual(self.read('get_folder_count',str(path.parent)),1)
        self.assertFalse(self.service.read('get_path_identity_status')['prepared'])

    def test_offline_history_is_not_counted_or_reactivated(self):
        path=self.image('Images/One.png',5)
        self.service.submit('remove_files',[str(path)]).result(3); self.prepare()
        self.assertEqual(self.read('get_folder_count',str(path.parent)),0)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT rating,is_offline FROM files').fetchone(),(5,1))

    def test_incomplete_registry_rejects_whole_scope(self):
        path=self.image('Images/One.png'); self.service.submit('prepare_path_identity').result(3)
        with self.assertRaisesRegex(ValueError,'review_required'):self.read('get_folder_count',str(path.parent))

    def test_no_reader_or_writer_snapshot_survives_during_folder_probes(self):
        path=self.image('Images/One.png'); self.prepare()
        def observation(folder,**kwargs):
            self.assertEqual(self.service.metrics_snapshot()['reads_active'],0)
            self.service.submit('update_file_rating',str(path),3).result(3)
            return observe_path(folder,**kwargs)
        with patch('core.folder_tasks.observe_path',side_effect=observation):
            self.assertEqual(self.read('get_folder_count',str(path.parent)),1)

    def test_folder_change_after_sql_disallows_stale_count(self):
        path=self.image('Images/One.png'); self.prepare(); calls=0
        def observation(folder,**kwargs):
            nonlocal calls
            calls+=1; value=observe_path(folder,**kwargs)
            return replace(value,physical=(999,*value.physical[1:])) if calls==2 else value
        with patch('core.folder_tasks.observe_path',side_effect=observation):
            with self.assertRaisesRegex(ValueError,'physical_state_changed'):self.read('get_folder_count',str(path.parent))

    def test_external_sql_change_between_observation_and_scope_rejects(self):
        path=self.image('Images/One.png'); self.prepare()
        def observation(folder,**kwargs):
            with closing(sqlite3.connect(self.service.db_path)) as conn:
                conn.execute("INSERT INTO files(path,filename) VALUES('C:/foreign.png','foreign.png')"); conn.commit()
            return observe_path(folder,**kwargs)
        with patch('core.folder_tasks.observe_path',side_effect=observation):
            with self.assertRaisesRegex((ValueError,LibraryError),'review_required|sql_state_changed'):
                self.read('get_folder_count',str(path.parent))

    def test_concurrent_confirmed_ingestion_can_supply_a_newer_coherent_count(self):
        path=self.image('Images/One.png'); self.prepare(); first=True
        def observation(folder,**kwargs):
            nonlocal first
            value=observe_path(folder,**kwargs)
            if first:
                first=False
                incoming=path.with_name('Two.png'); incoming.write_bytes(b'private')
                self.service.register_files_observed([dict(path=str(incoming),filename=incoming.name,size=7,created=1)],cancel_event=self.event)
            return value
        with patch('core.folder_tasks.observe_path',side_effect=observation):
            self.assertEqual(self.read('get_folder_count',str(path.parent)),2)

    def test_directory_content_change_without_native_commit_keeps_snapshot_count(self):
        path=self.image('Images/One.png'); self.prepare(); calls=0
        def observation(folder,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:path.with_name('NotIndexed.png').write_bytes(b'private')
            return observe_path(folder,**kwargs)
        with patch('core.folder_tasks.observe_path',side_effect=observation):
            self.assertEqual(self.read('get_folder_count',str(path.parent)),1)

    def test_complete_selection_limit_refuses_whole_scope_not_prefix(self):
        first=self.image('Images/One.png'); self.image('Images/Two.png'); self.prepare()
        with self.assertRaisesRegex(ValueError,'selection_requires_streaming'):
            self.read('resolve_library_selection',folder=str(first.parent),limit=1)

    def test_cancelled_scope_never_delivers_results(self):
        path=self.image('Images/One.png'); self.prepare(); self.event.set()
        with self.assertRaisesRegex(LibraryError,'cancelled'):self.read('get_folder_count',str(path.parent))

    def test_album_count_cover_and_nested_overlaps_are_independent(self):
        first=self.image('Árbol/One.png'); second=self.image('Árbol/Nested/Two.png'); self.prepare()
        for folder in (self.root/'árbol',second.parent):self.service.submit('add_watched_folder',str(folder)).result(3)
        total,albums=self.read('get_folders_paginated')
        self.assertEqual(total,2); self.assertEqual([row['count'] for row in albums],[2,1])
        self.assertEqual([row['cover'] for row in albums],[first.as_posix(),second.as_posix()])
        self.assertEqual(self.read('get_folders_paginated',limit=1,offset=1)[1],[albums[1]])

    def test_album_watch_change_during_probe_is_rejected(self):
        path=self.image('Images/One.png'); self.prepare(); self.service.submit('add_watched_folder',str(path.parent)).result(3)
        def observation(folder,**kwargs):
            self.service.submit('add_watched_folder',str(self.root/'Another')).result(3)
            return observe_path(folder,**kwargs)
        with patch('core.folder_tasks.observe_path',side_effect=observation):
            with self.assertRaisesRegex(ValueError,'page_changed'):self.read('get_folders_paginated')

    def test_album_does_not_issue_per_folder_count_or_preview_queries(self):
        path=self.image('Images/One.png'); self.service.submit('add_watched_folder',str(path.parent)).result(3)
        for prepared in (False,True):
            if prepared:self.prepare()
            with patch.object(LibraryRepository,'get_folder_count',side_effect=AssertionError('N+1')),patch.object(LibraryRepository,'get_folder_preview',side_effect=AssertionError('N+1')):
                self.assertEqual(self.read('get_folders_paginated')[1][0]['count'],1)

    def test_prepared_prefix_query_uses_canonical_index(self):
        path=self.image('Images/One.png'); self.prepare()
        with closing(open_reader(self.service.db_path)) as conn:
            repo=LibraryRepository(conn); o=observe_path(str(path.parent)); repo._folder_scope_context={o.path:dict(canonical=o.canonical,kind=o.kind,physical=o.physical)}
            where,params=condition(repo,o.path)
            plan=conn.execute('EXPLAIN QUERY PLAN SELECT id FROM files WHERE '+where,params).fetchall()
            self.assertTrue(any('idx_path_identity_canonical' in row[3] and 'SEARCH' in row[3] for row in plan),plan)

    def test_qt_reader_probes_off_gui_and_delivers_on_gui(self):
        path=self.image('Images/One.png'); self.prepare()
        reader=LibraryReader(self.service); delivered=[]; threads=[]; main=threading.get_ident()
        reader.result.connect(lambda *args:delivered.append((threading.get_ident(),args)))
        def observation(folder,**kwargs):
            threads.append(threading.get_ident()); return observe_path(folder,**kwargs)
        try:
            with patch('core.folder_tasks.observe_path',side_effect=observation):
                reader.request('get_folder_count',str(path.parent)); end=time.monotonic()+3
                while not delivered and time.monotonic()<end:self.app.processEvents(); time.sleep(.001)
            self.assertTrue(delivered); self.assertEqual(delivered[0][0],main)
            self.assertTrue(threads); self.assertTrue(all(thread!=main for thread in threads))
            self.assertEqual(delivered[0][1][1:],(1,None))
        finally:
            reader.close()
            if reader._thread:reader._thread.join(3); self.assertFalse(reader._thread.is_alive())
            reader.deleteLater(); self.app.processEvents()


if __name__=='__main__':unittest.main(verbosity=2)
