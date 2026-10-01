"""Private bounded backfill/resolution, cancellation and concurrent changes."""
from contextlib import closing
from pathlib import Path
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.library_service import LibraryService, LibraryError
from core.library_batch import commit_control, BatchCancelled
from core.library_repository import LibraryRepository


class Migration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.service=LibraryService(self.root/'private.db')
        self.addCleanup(self.service.close)

    def register(self,paths):
        self.service.submit('register_files_minimal',[dict(path=str(p),filename=Path(p).name,size=1,created=1) for p in paths]).result(3)

    def add(self,name):
        path=self.root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'private')
        self.register([path])
        return path

    def test_205_records_prepare_100_100_5_and_resolve_unicode_alias(self):
        images=[]
        folder=self.root/'Árbol'
        folder.mkdir()
        for i in range(205):
            path=folder/f'Image{i}.png'
            path.write_bytes(b'private')
            images.append(path)
        self.register(images)
        notices=[]
        result=self.service.backfill_path_identity(progress=notices.append)
        self.assertEqual((result['confirmed'],result['pages'],result['cancelled']),(205,3,False))
        self.assertEqual([n['confirmed'] for n in notices],[100,200,205])
        self.assertEqual(result['coverage']['pending'],0)
        alias=self.root/'árbol'/'image0.png'
        lookup=self.service.resolve_path_identity(str(alias))
        self.assertEqual(lookup['status'],'unique')
        self.assertEqual(lookup['file_id'],self.service.read('get_file_id',str(images[0])))

    def test_unprepared_partial_and_unresolved_never_select_id(self):
        image=self.add('one.png')
        self.assertEqual(self.service.resolve_path_identity(str(image))['status'],'not_prepared')
        self.service.submit('prepare_path_identity').result(3)
        self.assertEqual(self.service.resolve_path_identity(str(image))['status'],'incomplete')
        self.service.backfill_path_identity()
        missing=self.add('other.png')
        missing.unlink()
        self.service.backfill_path_identity()
        lookup=self.service.resolve_path_identity(str(image))
        self.assertEqual(lookup['status'],'unresolved')
        self.assertIsNone(lookup['file_id'])

    def test_ambiguous_legacy_copies_keep_distinct_ratings_and_no_winner(self):
        image=self.add('Árbol/Mixed.png')
        alias=self.root/'árbol'/'mixed.png'
        self.register([alias])
        self.service.submit('edit_curation',str(image),rating=5,add_tags=['curated']).result(3)
        self.service.backfill_path_identity()
        result=self.service.resolve_path_identity(str(alias))
        self.assertEqual(result['status'],'ambiguous')
        self.assertIsNone(result['file_id'])
        self.assertEqual([r[2] for r in result['records']],[5,0])

    def test_cancel_after_commit_counts_only_confirmed_and_resume_rechecks(self):
        paths=[]
        for i in range(3):
            paths.append(self.add(f'{i}.png'))
        event=threading.Event()
        result=self.service.backfill_path_identity(limit=2,cancel_event=event,progress=lambda r:event.set())
        self.assertEqual((result['cancelled'],result['confirmed'],result['pages']),(True,2,1))
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],1)
        resumed=self.service.backfill_path_identity(limit=2)
        self.assertEqual(resumed['confirmed'],3)
        self.assertEqual(resumed['coverage']['pending'],0)
        for path in paths:
            self.assertEqual(self.service.resolve_path_identity(str(path))['status'],'unique')

    def test_cancel_before_prepare_does_not_create_registry_or_backup(self):
        self.add('one.png')
        event=threading.Event()
        event.set()
        result=self.service.backfill_path_identity(cancel_event=event)
        self.assertEqual(result['confirmed'],0)
        self.assertTrue(result['cancelled'])
        self.assertFalse(self.service.read('get_path_identity_status')['prepared'])
        self.assertEqual(list(self.root.glob('*.pre-path-identity-*.bak')),[])

    def test_late_new_file_is_outside_initial_boundary_and_pending_visible(self):
        self.add('one.png')
        self.add('two.png')
        notices=[]
        def progress(row):
            notices.append(row)
            if len(notices)==1:
                self.add('late.png')
        result=self.service.backfill_path_identity(limit=1,progress=progress)
        self.assertEqual(result['confirmed'],2)
        self.assertEqual(result['coverage']['pending'],1)
        self.assertIsNone(self.service.resolve_path_identity(str(self.root/'one.png'))['file_id'])

    def test_later_page_failure_retains_first_confirmed_page(self):
        self.add('one.png')
        self.add('two.png')
        original=self.service.publish_path_identity_page
        calls=0
        def publish(page,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:
                raise ValueError('injected physical change')
            return original(page,**kwargs)
        notices=[]
        with patch.object(self.service,'publish_path_identity_page',side_effect=publish):
            with self.assertRaisesRegex(ValueError,'injected physical change'):
                self.service.backfill_path_identity(limit=1,progress=notices.append)
        self.assertEqual([n['confirmed'] for n in notices],[1])
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],1)

    def test_offline_existing_copy_remains_retained_and_unknown_lookup(self):
        image=self.add('one.png')
        file_id=self.service.read('get_file_id',str(image))
        self.service.submit('edit_curation',str(image),rating=5,add_tags=['keep']).result(3)
        self.service.submit('remove_files',[str(image)]).result(3)
        result=self.service.backfill_path_identity()
        self.assertEqual(result['counts']['offline'],1)
        self.assertEqual(self.service.resolve_path_identity(str(image))['status'],'missing')
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT id,rating,is_offline FROM files').fetchone(),(file_id,5,1))

    def test_resolver_does_not_hold_reader_during_disk_and_unknown_file_not_registered(self):
        image=self.add('one.png')
        self.service.backfill_path_identity()
        from core.library_path_migration import observe_path
        def probe(path,**kwargs):
            self.assertEqual(self.service.metrics_snapshot()['reads_active'],0)
            return observe_path(path,**kwargs)
        other=self.root/'new.png'
        other.write_bytes(b'private')
        with patch('core.library_path_migration.observe_path',side_effect=probe):
            self.assertEqual(self.service.resolve_path_identity(str(other))['status'],'missing')
        self.assertEqual(self.service.read('count_files'),1)

    def test_control_precancel_does_not_admit_preparation(self):
        event=threading.Event()
        event.set()
        with self.assertRaises(BatchCancelled):
            commit_control(self.service,'prepare_path_identity',event)
        with self.assertRaises(ValueError):
            commit_control(self.service,'vacuum_database',event)
        self.assertFalse(self.service.read('get_path_identity_status')['prepared'])

    def test_control_cancel_while_queue_full_preserves_existing_fifo(self):
        image=self.add('one.png')
        db=self.service.db_path
        self.service.close()
        self.service=LibraryService(db,capacity=1)
        self.addCleanup(self.service.close)
        entered,release,cancel=threading.Event(),threading.Event(),threading.Event()
        original=LibraryRepository.update_file_rating
        def held(repo,*args,**kwargs):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(3))
            return original(repo,*args,**kwargs)
        outcomes=[]
        def control():
            try:
                commit_control(self.service,'prepare_path_identity',cancel)
            except BaseException as exc:
                outcomes.append(exc)
        with patch.object(LibraryRepository,'update_file_rating',held):
            first=self.service.submit('update_file_rating',str(image),1)
            self.assertTrue(entered.wait(2))
            second=self.service.submit('update_file_rating',str(image),2)
            worker=threading.Thread(target=control)
            worker.start()
            try:
                until=time.monotonic()+2
                while self.service.metrics_snapshot()['rejected_full']==0 and time.monotonic()<until:
                    time.sleep(.01)
                self.assertGreater(self.service.metrics_snapshot()['rejected_full'],0)
                cancel.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(outcomes),1)
                self.assertIsInstance(outcomes[0],BatchCancelled)
            finally:
                cancel.set()
                release.set()
                worker.join(2)
            self.assertTrue(first.result(3))
            self.assertTrue(second.result(3))
        self.assertEqual(self.service.read('get_file_rating',str(image)),2)
        self.assertFalse(self.service.read('get_path_identity_status')['prepared'])

    def test_invalid_limit_rejects_before_schema_and_close(self):
        for limit in (0,101,True):
            with self.assertRaises(ValueError):
                self.service.backfill_path_identity(limit=limit)
        self.assertFalse(self.service.read('get_path_identity_status')['prepared'])
        self.service.close()
        with self.assertRaises(LibraryError):
            self.service.backfill_path_identity()

    def test_cancel_at_final_coverage_still_reports_confirmed_commit(self):
        self.add('one.png')
        event=threading.Event()
        original=self.service.read
        def read(operation,*args,**kwargs):
            if operation=='get_path_identity_status':
                event.set()
            return original(operation,*args,**kwargs)
        with patch.object(self.service,'read',side_effect=read):
            result=self.service.backfill_path_identity(cancel_event=event)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['confirmed'],1)
        self.assertEqual(original('get_path_identity_status')['pending'],0)

    def test_cli_preview_no_schema_then_explicit_apply_and_resolve(self):
        image=self.add('Árbol/Mixed.png')
        self.service.submit('edit_curation',str(image),rating=5,add_tags=['keep']).result(3)
        db=self.service.db_path
        self.service.close()
        command=[sys.executable,str(Path(__file__).with_name('path_identity_prepare.py')),'--db',db]
        preview=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(preview.returncode,0,preview.stderr)
        data=json.loads(preview.stdout)
        self.assertFalse(data['prepared'])
        self.assertEqual(data['pending'],1)
        applied=subprocess.run([*command,'--apply','--resolve',str(self.root/'árbol'/'mixed.png')],
            capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(applied.returncode,0,applied.stderr)
        rows=[json.loads(line) for line in applied.stdout.splitlines()]
        self.assertEqual(rows[-1]['status'],'unique')
        self.assertEqual(rows[-1]['records'][0][2],5)
        self.assertEqual(rows[-2]['confirmed'],1)
        self.assertEqual(len(list(self.root.glob('*.pre-path-identity-*.bak'))),1)

    def test_cli_rejects_non_native_without_mutating_it(self):
        foreign=self.root/'foreign.db'
        with closing(sqlite3.connect(foreign)) as conn:
            conn.execute('CREATE TABLE foreign_marker(name TEXT)')
            conn.execute("INSERT INTO foreign_marker VALUES('preserve')")
            conn.commit()
        before=hashlib.sha256(foreign.read_bytes()).digest()
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('path_identity_prepare.py')),
            '--db',str(foreign),'--apply'],capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('library_not_ready',result.stderr)
        self.assertEqual(hashlib.sha256(foreign.read_bytes()).digest(),before)
        self.assertEqual(list(self.root.glob('foreign.db.pre-*.bak')),[])


if __name__=='__main__':
    unittest.main(verbosity=2)
