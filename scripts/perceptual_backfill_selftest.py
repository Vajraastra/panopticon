"""Imágenes reales privadas: bytes, cache, límites, commits y terminal Qt."""
from concurrent.futures import Future
from copy import deepcopy
import io
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from PySide6.QtCore import QObject,Slot,QTimer
from PySide6.QtWidgets import QApplication
from modules.duplicate_finder.logic.perceptual_backfill import PerceptualBackfill,algorithm_id,calculate
from modules.duplicate_finder.logic.perceptual_worker import PerceptualBackfillWorker
from curation_snapshot_selftest import pump
import duplicate_exact_review_selftest as helpers


class Receiver(QObject):
    def __init__(self):
        super().__init__()
        self.values,self.threads=[],[]
    @Slot(dict)
    def result(self,value):
        self.values.append(value)
        self.threads.append(threading.get_ident())


class Backfill(helpers.Review):
    def setUp(self):
        super().setUp()
        self.workers,self.releases=[],[]
        self.addCleanup(self.shutdown)
    def shutdown(self):
        for release in self.releases:release.set()
        for worker in self.workers:
            worker.stop()
            self.assertTrue(worker.wait(3000))  # Sólo fixture, I/O liberado.
        QApplication.processEvents()
    def image(self,name='image.png',seed=0,**kwargs):
        with Image.new('RGB',(64,64)) as image:
            image.putdata([((x*19+seed)%256,(x*7+seed*3)%256,(x*23)%256) for x in range(4096)])
            stream=io.BytesIO()
            image.save(stream,format='PNG')
        return self.file(name,data=stream.getvalue(),**kwargs)
    def phase(self,**kwargs):
        return PerceptualBackfill(self.service,[self.root.as_posix()],**kwargs).run()
    def page(self,algorithm=None):
        return self.service.read('get_phash_page',[self.root.as_posix()],algorithm or algorithm_id())
    def pair_images(self):
        return self.image('raw.png'),self.image('curated.png',rating=5,tags=['documented'])
    def observe(self,worker):
        receiver,native=Receiver(),[]
        worker.finished_signal.connect(receiver.result)
        worker.finished.connect(lambda:native.append(True))
        self.workers.append(worker)
        return receiver,native
    def finish(self,receiver,native):
        self.assertTrue(pump(lambda:receiver.values and native))
        self.assertEqual(len(receiver.values),1)
        self.assertEqual(len(native),1)
        self.assertEqual(receiver.threads,[threading.get_ident()])
        return receiver.values[0]

    def test_real_images_persist_current_algorithm_without_changing_curated_data(self):
        raw,curated=self.pair_images()
        before=self.service.read('get_duplicate_snapshot',[raw.as_posix(),curated.as_posix()])
        value=self.phase()
        self.assertTrue(value['complete'])
        self.assertEqual((value['persisted'],value['processed'],value['reused']),(2,2,0))
        self.assertIn('/pillow:',value['algorithm'])
        records=self.page()['records']
        self.assertEqual(records[0]['phash'],records[1]['phash'])
        self.assertEqual(len(records[0]['phash']),16)
        after=self.service.read('get_duplicate_snapshot',[raw.as_posix(),curated.as_posix()])
        for old,new,record in zip(before,after,records,strict=True):
            expected=deepcopy(old['state'])
            expected['file']['phash']=record['phash']
            self.assertEqual(new['state'],expected)

    def test_same_cache_skips_decode_and_writes_but_still_reads_current_sha(self):
        self.pair_images()
        self.assertTrue(self.phase()['complete'])
        before=self.service.metrics_snapshot()['committed']
        with patch.object(Image,'open',side_effect=AssertionError('cached image decoded')):
            value=self.phase()
        self.assertTrue(value['complete'])
        self.assertEqual((value['persisted'],value['reused']),(0,2))
        self.assertEqual(self.service.metrics_snapshot()['committed'],before)
        self.image('raw.png',seed=10,register=False)
        with patch.object(Image,'open',side_effect=AssertionError('stale cache decoded')):
            value=self.phase()
        self.assertFalse(value['complete'])
        self.assertEqual(value['issues'][0]['reason'],'catalog_identity_mismatch')
        self.assertEqual(value['persisted'],0)

    def test_unknown_sha_is_reported_and_does_not_prepare_empty_registry(self):
        path=self.image(register=False)
        self.register(path.as_posix())
        value=self.phase()
        self.assertFalse(value['complete'])
        self.assertEqual((value['unknown_images'],value['issue_count']),(1,1))
        self.assertEqual(value['issues'][0]['reason'],'phash_source_sha_unknown')
        self.assertFalse(self.service.read('get_phash_registry_status')['prepared'])

    def test_changed_algorithm_recomputes_and_invalidates_previous_namespace(self):
        self.image()
        old=algorithm_id()
        self.assertTrue(self.phase()['complete'])
        new=old.replace('/numpy:','/numpy:next-')
        with patch('modules.duplicate_finder.logic.perceptual_backfill.algorithm_id',return_value=new):
            value=self.phase()
        self.assertTrue(value['complete'])
        self.assertEqual((value['persisted'],value['reused']),(1,0))
        self.assertIsNone(self.page(old)['records'][0]['phash'])
        self.assertIsNotNone(self.page(new)['records'][0]['phash'])

    def test_pixel_limit_and_invalid_image_preserve_bytes_without_storing_phash(self):
        image=self.image()
        value=self.phase(pixel_limit=10)
        self.assertFalse(value['complete'])
        self.assertEqual(value['issues'][0]['reason'],'duplicate_visual_pixel_limit')
        self.assertEqual(value['persisted'],0)
        self.file('invalid.png',data=b'not an image')
        value=self.phase()
        self.assertFalse(value['complete'])
        self.assertEqual(value['persisted'],1)
        self.assertTrue(image.exists())

    def test_mutation_during_decode_with_same_size_and_restored_mtime_is_detected_by_second_sha(self):
        import imagehash
        path=self.image()
        original=imagehash.phash
        stat=path.stat()
        def changed(image,**kwargs):
            value=original(image,**kwargs)
            payload=bytearray(path.read_bytes())
            payload[-1]^=1
            path.write_bytes(payload)
            os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
            return value
        with patch.object(imagehash,'phash',changed):value=self.phase()
        self.assertFalse(value['complete'])
        self.assertEqual(value['persisted'],0)
        self.assertEqual(value['issues'][0]['reason'],'copy_version_file_changed')
        self.assertIsNone(self.page()['records'][0]['phash'])

    def test_initial_image_limit_rejects_before_schema_or_any_partial_write(self):
        self.pair_images()
        value=self.phase(max_images=1)
        self.assertEqual(value['error'],'phash_backfill_image_budget')
        self.assertEqual(value['persisted'],0)
        self.assertFalse(self.service.read('get_phash_registry_status')['prepared'])

    def test_pages_and_bounded_commits_process_complete_chosen_scope(self):
        for i in range(260):self.image(f'{i}.png')
        calls=[]
        submit=self.service.submit
        def traced(operation,*args,**kwargs):
            if operation=='store_phash_batch':calls.append(len(args[0]))
            return submit(operation,*args,**kwargs)
        with patch.object(self.service,'submit',traced):value=self.phase()
        self.assertTrue(value['complete'])
        self.assertEqual(value['persisted'],260)
        self.assertEqual(calls,[64,64,64,64,4])

    def test_identity_changed_at_owner_admission_blocks_batch_without_losing_curation(self):
        raw,curated=self.pair_images()
        snapshot=self.service.read('get_duplicate_snapshot',[curated.as_posix()])[0]
        submit=self.service.submit
        def changed(operation,*args,**kwargs):
            if operation=='store_phash_batch':
                submit('archive_copy',snapshot['state']['file']['id'],expected_fingerprint=snapshot['fingerprint']).result(3)
            return submit(operation,*args,**kwargs)
        with patch.object(self.service,'submit',changed):value=self.phase()
        self.assertFalse(value['complete'])
        self.assertIn('phash_copy_identity_changed',value['error'])
        self.assertEqual(value['persisted'],0)
        self.assertIsNone(self.page()['records'][0]['phash'])
        self.assertTrue(raw.exists() and curated.exists())

    def test_unconfirmed_prepare_or_store_future_never_counts_persisted_data(self):
        self.image()
        submit=self.service.submit
        for target,reason in (('prepare_phash_registry','phash_registry_prepare_unconfirmed'),
                              ('store_phash_batch','phash_batch_commit_unconfirmed')):
            def unconfirmed(operation,*args,**kwargs):
                if operation==target:
                    future=Future()
                    future.set_result(False)
                    return future
                return submit(operation,*args,**kwargs)
            with patch.object(self.service,'submit',unconfirmed):value=self.phase()
            self.assertEqual(value['error'],reason)
            self.assertEqual(value['persisted'],0)

    def test_pre_cancelled_phase_never_reads_or_writes(self):
        event=threading.Event()
        event.set()
        with (patch.object(self.service,'read',side_effect=AssertionError('read admitted')),
              patch.object(self.service,'submit',side_effect=AssertionError('write admitted'))):
            value=PerceptualBackfill(self.service,[self.root.as_posix()]).run(event=event)
        self.assertTrue(value['cancelled'])
        self.assertEqual(value['persisted'],0)

    def test_cancellation_after_accepted_commit_preserves_confirmed_counts(self):
        self.pair_images()
        event=threading.Event()
        submit=self.service.submit
        def accepted(operation,*args,**kwargs):
            future=submit(operation,*args,**kwargs)
            if operation=='store_phash_batch':
                future.result(3)
                event.set()
            return future
        with patch.object(self.service,'submit',accepted):
            value=PerceptualBackfill(self.service,[self.root.as_posix()]).run(event=event)
        self.assertTrue(value['cancelled'])
        self.assertEqual(value['persisted'],2)
        self.assertTrue(all(record['phash'] for record in self.page()['records']))

    def test_cancel_does_not_hide_failure_of_an_admitted_commit(self):
        self.image()
        event=threading.Event()
        submit=self.service.submit
        def failed(operation,*args,**kwargs):
            if operation=='store_phash_batch':
                future=Future()
                future.set_result(False)
                event.set()
                return future
            return submit(operation,*args,**kwargs)
        with patch.object(self.service,'submit',failed):
            value=PerceptualBackfill(self.service,[self.root.as_posix()]).run(event=event)
        self.assertFalse(value['cancelled'])
        self.assertEqual(value['error'],'phash_batch_commit_unconfirmed')
        self.assertEqual(value['persisted'],0)
        self.assertIsNone(self.page()['records'][0]['phash'])

    def test_qt_worker_is_lazy_captures_scope_and_publishes_one_terminal_on_gui(self):
        self.pair_images()
        folders=[self.root.as_posix()]
        with (patch.object(self.service,'read',side_effect=AssertionError('constructor read')),
              patch.object(self.service,'submit',side_effect=AssertionError('constructor write')),
              patch.object(Path,'stat',side_effect=AssertionError('constructor stat'))):
            worker=PerceptualBackfillWorker(self.service,folders,token=9)
        folders.clear()
        receiver,native=self.observe(worker)
        worker.start()
        result=self.finish(receiver,native)
        self.assertTrue(result['complete'])
        self.assertEqual((result['token'],result['persisted']),(9,2))

    def test_qt_cancel_is_responsive_and_does_not_store_unadmitted_calculations(self):
        self.pair_images()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        threads=[]
        def blocked(record,event,**kwargs):
            threads.append(threading.get_ident())
            entered.set()
            self.assertTrue(release.wait(3))
            return calculate(record,event,**kwargs)
        worker=PerceptualBackfillWorker(self.service,[self.root.as_posix()])
        receiver,native=self.observe(worker)
        with patch('modules.duplicate_finder.logic.perceptual_backfill.calculate',blocked):
            worker.start()
            self.assertTrue(entered.wait(1))
            ticks=[]
            QTimer.singleShot(0,lambda:ticks.append(True))
            started=time.monotonic()
            worker.stop()
            self.assertLess(time.monotonic()-started,.1)
            self.assertTrue(pump(lambda:ticks))
            release.set()
            value=self.finish(receiver,native)
        self.assertTrue(value['cancelled'])
        self.assertEqual(value['persisted'],0)
        self.assertNotIn(threading.get_ident(),threads)


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('Backfill.'+name,sys.modules[__name__])
        for name in Backfill.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
