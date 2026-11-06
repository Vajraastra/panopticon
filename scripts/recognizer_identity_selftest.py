"""Versiones/carreras reales con Native temporal e inferencia simulada; sin modelos/GPU."""
import os,sys,threading,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import numpy as np
from PIL import Image
from PySide6.QtWidgets import QApplication
from core.library_service import LibraryError
from core.paths import ProjectPaths
from modules.character_recognizer.logic.copy_bindings import capture,normalized
from modules.character_recognizer.logic.thread_worker import RecognitionWorker,AutoScanWorker
from modules.character_recognizer.ui.recognition_view import _LIVE_WORKERS
import curation_snapshot_selftest as helpers
import recognizer_data_selftest as existing


class CoreBindings(helpers.Fixture):
    def binding(self,path):
        return self.service.read('get_curation_snapshot',[path],include_revision=True)[0]

    def test_optional_revision_matches_full_state_preserves_old_contract_order_and_duplicates(self):
        self.register('C:/a.png','C:/b.png')
        self.service.submit('edit_curation','C:/a.png',add_tags=['curated'],rating=5).result(3)
        old=self.service.read('get_curation_snapshot',['C:/a.png'])
        self.assertEqual(set(old[0]),{'id','path','tags','rating'})
        rows=self.service.read('get_curation_snapshot',['C:/a.png','C:/missing.png','C:/a.png'],include_revision=True)
        self.assertEqual(rows[0],rows[2]);self.assertIsNone(rows[1]['id']);self.assertIsNone(rows[1]['fingerprint'])
        self.assertEqual(rows[0]['tags'],('curated',))
        self.assertEqual(rows[0]['fingerprint'],self.service.read('get_copy_version_state',rows[0]['id'])['fingerprint'])

    def test_delta_with_full_revision_rejects_late_curation_without_touching_any_state(self):
        self.register('C:/a.png');before=self.binding('C:/a.png')
        self.service.submit('edit_curation','C:/a.png',add_tags=['new'],rating=4).result(3)
        state=self.service.read('get_copy_version_state',before['id'])
        with self.assertRaisesRegex(LibraryError,'changed_since_review'):
            self.service.submit('edit_curation','C:/a.png',expected_copy_id=before['id'],expected_fingerprint=before['fingerprint'],add_tags=['stale'],rating=0).result(3)
        self.assertEqual(self.service.read('get_copy_version_state',before['id']),state)

    def test_current_copy_with_offline_history_accepts_bound_revision_and_preserves_old_curation(self):
        self.register('C:/a.png');self.service.submit('edit_curation','C:/a.png',add_tags=['old'],rating=5).result(3)
        old=self.binding('C:/a.png');self.service.submit('archive_copy',old['id'],expected_fingerprint=old['fingerprint']).result(3)
        offline=self.service.read('get_copy_version_state',old['id'])
        self.service.submit('make_copy_online',old['id'],expected_fingerprint=offline['fingerprint'],digest='1'*64,
            observation=dict(filename='a.png',size=1,created=1)).result(3)
        current=self.binding('C:/a.png')
        self.service.submit('edit_curation','C:/a.png',expected_copy_id=current['id'],expected_fingerprint=current['fingerprint'],add_tags=['current']).result(3)
        self.assertEqual(self.binding('C:/a.png')['tags'],('current',))
        self.assertEqual([t['name'] for t in self.service.read('get_copy_version_state',old['id'])['state']['tags']],['old'])

    def test_invalid_revision_arguments_reject_without_mutation_or_ddl(self):
        self.register('C:/a.png');row=self.binding('C:/a.png')
        for extra in (dict(expected_fingerprint=row['fingerprint']),dict(expected_copy_id=row['id'],expected_fingerprint='invalid')):
            with self.assertRaises(LibraryError):self.service.submit('edit_curation','C:/a.png',add_tags=['wrong'],**extra).result(3)
        for flag in (1,None,'yes'):
            with self.assertRaises(ValueError):self.service.read('get_curation_snapshot',['C:/a.png'],include_revision=flag)
        self.assertEqual(self.binding('C:/a.png'),row)

    def test_capture_257_paths_uses_two_bounded_read_blocks_and_no_writes(self):
        paths=[f'C:/collection/{index}.png' for index in range(257)];self.register(*paths)
        read=self.service.read;calls=[]
        def counted(op,block,**kwargs):calls.append((op,len(block),kwargs['include_revision']));return read(op,block,**kwargs)
        with patch.object(self.service,'read',counted),patch.object(self.service,'submit',side_effect=AssertionError('capture writes')):
            result=capture(self.service,paths+[paths[0]])
        self.assertEqual(calls,[('get_curation_snapshot',256,True),('get_curation_snapshot',1,True)])
        self.assertEqual(len(result),257);self.assertTrue(all(row['id'] is not None for row in result.values()))

    def test_scope_memory_deadline_and_cancellation_discard_entire_capture(self):
        self.register('C:/a.png')
        for target,value in (('MAX_PATHS',0),('MAX_BYTES',1),('MAX_SECONDS',-1)):
            with patch('modules.character_recognizer.logic.copy_bindings.'+target,value),self.assertRaises(ValueError):capture(self.service,['C:/a.png'])
        self.assertIsNone(capture(self.service,['C:/a.png'],running=lambda:False))
        read=self.service.read
        with patch.object(self.service,'read',lambda *args,**kw:read(*args,**kw)[:-1]),self.assertRaisesRegex(ValueError,'incomplete'):
            capture(self.service,['C:/a.png'])

    def test_large_revision_and_repeated_output_do_not_return_a_partial_scope(self):
        self.register('C:/a.png');self.service.submit('edit_curation','C:/a.png',add_tags=['x'*256],rating=5).result(3)
        from core import curation_bindings
        with patch.object(curation_bindings,'MAX_BYTES',500),self.assertRaisesRegex(ValueError,'payload_too_large'):
            self.service.read('get_curation_snapshot',['C:/a.png']*5,include_revision=True)
        self.assertEqual(self.binding('C:/a.png')['rating'],5)

    def test_incomplete_revision_cannot_fall_back_to_identity_only_or_legacy_path_write(self):
        self.register('C:/a.png');read=self.service.read
        def missing_revision(*args,**kwargs):
            rows=read(*args,**kwargs)
            rows[0]['fingerprint']=None
            return rows
        with patch.object(self.service,'read',missing_revision),self.assertRaisesRegex(ValueError,'incomplete'):
            capture(self.service,['C:/a.png'])

    def test_scope_bytes_checked_incrementally_before_sql_or_serializing_entire_selection(self):
        from modules.character_recognizer.logic import copy_bindings
        encoded=[];original=copy_bindings.encode
        def bounded(value):
            self.assertIsInstance(value,str);encoded.append(len(value));return original(value)
        with patch.object(copy_bindings,'MAX_BYTES',5000),patch.object(copy_bindings,'encode',bounded),patch.object(self.service,'read',side_effect=AssertionError('oversize read')):
            with self.assertRaisesRegex(ValueError,'scope_limit'):
                capture(self.service,['C:/'+('a'*3000)+'.png','C:/'+('b'*3000)+'.png'])
        self.assertEqual(len(encoded),2)

    def test_repeated_compact_rows_count_bytes_before_serializing_large_return(self):
        from core import curation_bindings
        records=[dict(path='C:/a.png',state=dict(file=dict(id=1,rating=0),tags=[dict(name='x'*256)]),fingerprint='1'*64)]
        original=curation_bindings.encode;encoded=[]
        def bounded(value):
            self.assertIsInstance(value,dict);encoded.append(True);return original(value)
        with patch.object(curation_bindings,'MAX_BYTES',500),patch.object(curation_bindings,'encode',bounded),self.assertRaisesRegex(ValueError,'payload_too_large'):
            curation_bindings.compact(records,['C:/a.png']*256)
        self.assertEqual(encoded,[True])


class RecognizerIdentity(helpers.Fixture):
    view=existing.RecognizerData.view
    image=existing.RecognizerData.image
    ready=existing.RecognizerData.ready
    settled=existing.RecognizerData.settled
    bulk=existing.RecognizerData.bulk
    tags=existing.RecognizerData.tags
    close_views=existing.RecognizerData.close_views
    blocked=existing.RecognizerData.blocked

    def setUp(self):
        super().setUp();self.views=[];self.addCleanup(self.close_views)
        self.pather=patch.object(ProjectPaths,'root',return_value=Path(self.temp.name));self.pather.start();self.addCleanup(self.pather.stop)
        self.a=str(Path(self.temp.name)/'a.png');self.b=str(Path(self.temp.name)/'b.png')
        for path in (self.a,self.b):Image.new('RGB',(12,10),'red').save(path)
        self.register(self.a,self.b)

    def replace(self,path):
        key=self.service.read('get_file_id',path);state=self.service.read('get_copy_version_state',key)
        self.service.submit('archive_copy',key,expected_fingerprint=state['fingerprint']).result(3)
        offline=self.service.read('get_copy_version_state',key)
        self.service.submit('make_copy_online',key,expected_fingerprint=offline['fingerprint'],digest='1'*64,
            observation=dict(filename=Path(path).name,size=1,created=1)).result(3)
        return key,self.service.read('get_file_id',path)

    def test_manual_confirm_tags_current_id_with_history_and_keeps_previous_version(self):
        self.service.submit('edit_curation',self.a,add_tags=['old'],rating=5).result(3)
        old,new=self.replace(self.a);view=self.view();self.image(view,self.a);self.ready(view)
        view.on_confirm();self.settled(view)
        self.assertEqual(self.service.read('get_curation_snapshot',[self.a])[0]['tags'],('alpha',))
        self.assertEqual(self.service.read('get_copy_version_state',old)['state']['file']['rating'],5)
        self.assertNotEqual(old,new)

    def test_native_id_changed_after_display_never_tags_new_copy_or_learns_reference(self):
        view=self.view();self.image(view,self.a,None,np.ones(3));self.ready(view)
        old,new=self.replace(self.a);view.combo_names.setEditText('Wrong');view.on_manual_save()
        self.assertTrue(helpers.pump(lambda:view._pending_edit is None and not view._reader._active))
        self.assertIn('copy ID or its reviewed state changed',view.lbl_status.text());self.assertFalse(view._curation_ready)
        self.assertIn('Reload the batch',' '.join(self.tags(view)));view.on_manual_save()
        self.assertEqual(self.service.read('get_curation_snapshot',[self.a])[0]['tags'],())
        self.assertEqual(view.profile_db.get_all_profiles(),[]);self.assertEqual(view.worker.next_count,0)

    def test_curation_changed_after_display_stops_first_attempt_and_keeps_newer_tags(self):
        view=self.view();self.image(view,self.a);self.ready(view)
        self.service.submit('edit_curation',self.a,add_tags=['other'],rating=4).result(3)
        view.on_confirm();self.settled(view)
        self.assertIn('reviewed state changed',view.lbl_status.text());self.assertEqual(view.worker.next_count,0)
        self.assertEqual(self.service.read('get_curation_snapshot',[self.a])[0]['tags'],('other',))

    def test_bulk_keeps_confirmed_prefix_and_never_rebinds_changed_tail_even_on_retry(self):
        view=self.view();self.bulk(view,[self.a,self.b]);old,new=self.replace(self.b)
        view.on_tag_all();self.assertTrue(helpers.pump(lambda:view._pending_edit is None))
        self.assertEqual(view._bulk_done,1);self.assertEqual(view._bulk_remaining,(self.b,))
        self.assertIn('copy ID or its reviewed state changed',view.lbl_auto_status.text())
        self.assertFalse(view.btn_tag_all.isEnabled())
        view.on_tag_all();self.assertTrue(helpers.pump(lambda:view._pending_edit is None))
        self.assertEqual(view._bulk_done,1);self.assertEqual(self.service.read('get_curation_snapshot',[self.b])[0]['tags'],())
        self.assertNotEqual(old,new)

    def test_bulk_revision_changed_after_scan_stops_without_overwriting_or_auto_refresh(self):
        view=self.view();self.bulk(view,[self.a,self.b])
        self.service.submit('edit_curation',self.b,add_tags=['newer']).result(3)
        view.on_tag_all();self.assertTrue(helpers.pump(lambda:view._pending_edit is None))
        self.assertEqual(view._bulk_done,1);self.assertIn('reviewed state changed',view.lbl_auto_status.text())
        view.on_tag_all();self.assertTrue(helpers.pump(lambda:view._pending_edit is None))
        self.assertEqual(self.service.read('get_curation_snapshot',[self.b])[0]['tags'],('newer',))

    def test_actual_worker_captures_id_before_decode_and_inference_then_rejects_replaced_native(self):
        view=self.view();view.worker=None;old=self.service.read('get_file_id',self.a);fixture=self
        class Engine:
            def initialize(self):pass
            def analyze_image(self,*args,**kwargs):fixture.replace(fixture.a);return np.ones(3),None,.9
            def compare(self,*args):return .9
        with patch('modules.character_recognizer.logic.thread_worker.RecognitionEngine',Engine):
            view.load_images([self.a]);worker=view.worker
            self.assertTrue(helpers.pump(lambda:'Reload the batch' in ' '.join(self.tags(view))))
            self.assertEqual(view._processed_copy_id,old);self.assertFalse(view.btn_confirm.isEnabled())
            view.shutdown();self.assertTrue(helpers.pump(view.shutdown_ready))
            self.assertTrue(helpers.pump(lambda:worker not in _LIVE_WORKERS))

    def test_actual_auto_worker_captures_batch_before_inference_and_does_not_tag_replacement(self):
        view=self.view();view.worker.paths=[self.a,self.b];fixture=self
        view.profile_db.add_reference('Alpha',np.ones(3))
        changed=[]
        class Engine:
            def initialize(self):pass
            def analyze_image(self,*args,**kwargs):
                if not changed:fixture.replace(fixture.b);changed.append(True)
                return np.ones(3),None,.9
            def compare(self,*args):return .9
        with patch('modules.character_recognizer.logic.thread_worker.RecognitionEngine',Engine):
            view.on_auto_scan();self.assertTrue(helpers.pump(lambda:view.auto_worker is None and bool(view._auto_paths)))
        view.on_tag_all();self.assertTrue(helpers.pump(lambda:view._pending_edit is None))
        self.assertEqual(view._bulk_done,1);self.assertEqual(self.service.read('get_curation_snapshot',[self.b])[0]['tags'],())

    def test_scope_rejected_before_model_initialization_emits_terminal_once_no_suggestion(self):
        view=self.view();worker=AutoScanWorker([self.a],mode='illustration',library_service=self.service)
        errors,terminal,suggestions=[],[],[]
        worker.error.connect(errors.append);worker.finished_signal.connect(lambda:terminal.append(True));worker.suggestion.connect(lambda *args:suggestions.append(args))
        with patch('modules.character_recognizer.logic.copy_bindings.MAX_PATHS',0),patch.object(worker.engine,'initialize',side_effect=AssertionError('model initialized')):
            worker.start();self.assertTrue(worker.wait(3000));QApplication.processEvents()
        self.assertEqual(errors,['recognition_copy_scope_limit']);self.assertEqual(terminal,[True]);self.assertEqual(suggestions,[])

    def test_cancelled_auto_inference_never_publishes_partial_dominance(self):
        view=self.view();worker=AutoScanWorker([self.a,self.b],mode='illustration',library_service=self.service)
        worker.db.add_reference('Alpha',np.ones(3));suggestions=[];worker.suggestion.connect(lambda *args:suggestions.append(args))
        class Engine:
            def initialize(self):pass
            def analyze_image(self,*args,**kwargs):worker.stop();return np.ones(3),None,.9
            def compare(self,*args):return .9
        worker.engine=Engine();worker.start();self.assertTrue(worker.wait(3000));QApplication.processEvents()
        self.assertEqual(suggestions,[])

    def test_unload_blocked_native_read_returns_immediately_retains_worker_and_readiness(self):
        view=self.view();entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        read=self.service.read
        def blocked(op,*args,**kwargs):
            if kwargs.get('include_revision'):entered.set();release.wait(3)
            return read(op,*args,**kwargs)
        with patch.object(self.service,'read',blocked):
            view.on_auto_scan();self.assertTrue(entered.wait(1));worker=view.auto_worker;view.shutdown()
            self.assertFalse(view.shutdown_ready());self.assertIn(worker,_LIVE_WORKERS)
            release.set();self.assertTrue(helpers.pump(view.shutdown_ready));self.assertTrue(helpers.pump(lambda:worker not in _LIVE_WORKERS))
        self.assertEqual(self.service.read('count_files'),2)

    def test_incomplete_manual_revision_disables_confirmation_and_reports_read_failure(self):
        view=self.view();read=self.service.read
        def missing_revision(op,*args,**kwargs):
            rows=read(op,*args,**kwargs)
            if kwargs.get('include_revision'):rows[0]['fingerprint']=None
            return rows
        with patch.object(self.service,'read',missing_revision):
            self.image(view,self.a);self.assertTrue(helpers.pump(lambda:'Incomplete' in ' '.join(self.tags(view))))
            self.assertFalse(view.btn_confirm.isEnabled());view.on_confirm()
        self.assertEqual(self.service.read('get_curation_snapshot',[self.a])[0]['tags'],())

    def test_old_batch_revision_error_cannot_disable_a_new_batch(self):
        from concurrent.futures import Future
        view=self.view();self.bulk(view,[self.a,self.b]);future=Future()
        with patch.object(self.service,'submit',return_value=future):
            view.on_tag_all()
        view.load_images([self.b]);self.assertFalse(view._bulk_review_required)
        future.set_exception(LibraryError('edit_curation','copy_version_changed_since_review'))
        self.assertTrue(helpers.pump(lambda:view._pending_edit is None))
        self.assertFalse(view._bulk_review_required);self.assertEqual(view._queued_paths,[self.b])


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
