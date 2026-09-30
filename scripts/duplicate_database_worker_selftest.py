"""Terminal Qt y cancelación; build admitido conserva su commit y GUI disponible."""
from concurrent.futures import Future
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer,QObject,Slot
from PySide6.QtWidgets import QApplication
from core.library_repository import LibraryRepository
from modules.duplicate_finder.logic.database_worker import DatabaseGroupReviewWorker,NativeDuplicateQueryWorker
from modules.duplicate_finder.logic.exact_review import _probe
import duplicate_database_review_selftest as prior
from curation_snapshot_selftest import pump


class Receiver(QObject):
    def __init__(self):
        super().__init__()
        self.values,self.threads=[],[]
    @Slot(dict)
    def result(self,value):
        self.values.append(value)
        self.threads.append(threading.get_ident())


class Workers(prior.Review):
    def setUp(self):
        super().setUp()
        self.workers,self.releases=[],[]
        self.addCleanup(self.shutdown)
    def shutdown(self):
        for event in self.releases:
            event.set()
        for worker,receiver,native in self.workers:
            worker.stop()
            self.assertTrue(worker.wait(3000))
        QApplication.processEvents()
    def observe(self,worker):
        receiver,native=Receiver(),[]
        worker.finished_signal.connect(receiver.result)
        worker.finished.connect(lambda:native.append(True))
        self.workers.append((worker,receiver,native))
        return receiver,native
    def finish(self,worker,receiver,native):
        self.assertTrue(pump(lambda:native and receiver.values))
        self.assertEqual(len(native),1)
        self.assertEqual(len(receiver.values),1)
        self.assertEqual(receiver.threads,[threading.get_ident()])
        return receiver.values[0]

    def test_query_delivers_one_terminal_on_qt_without_sql_in_qt(self):
        self.pair()
        worker=NativeDuplicateQueryWorker(self.service,'get_duplicate_candidates_page',token=7)
        receiver,native=self.observe(worker)
        threads=[]
        read=self.service.read
        def traced(*args,**kwargs):
            threads.append(threading.get_ident())
            return read(*args,**kwargs)
        with patch.object(self.service,'read',traced):
            worker.start()
            result=self.finish(worker,receiver,native)
        self.assertEqual(result['token'],7)
        self.assertIsNone(result['error'])
        self.assertEqual(result['value']['groups'][0]['copies'],2)
        self.assertNotIn(threading.get_ident(),threads)

    def test_pre_cancelled_query_and_build_never_call_service(self):
        for operation in ('get_duplicate_candidates_page','prepare_duplicate_search'):
            worker=NativeDuplicateQueryWorker(self.service,operation)
            receiver,native=self.observe(worker)
            worker.stop()
            with (patch.object(self.service,'read',side_effect=AssertionError('read admitted')),
                  patch.object(self.service,'submit',side_effect=AssertionError('write admitted'))):
                worker.start()
                result=self.finish(worker,receiver,native)
            self.assertTrue(result['cancelled'])
            self.assertIsNone(result['value'])

    def test_accepted_build_finishes_after_stop_with_qt_heartbeat(self):
        self.pair()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        prepare=LibraryRepository.prepare_duplicate_search
        def blocked(repo):
            value=prepare(repo)
            entered.set()
            self.assertTrue(release.wait(3))
            return value
        worker=NativeDuplicateQueryWorker(self.service,'prepare_duplicate_search')
        receiver,native=self.observe(worker)
        beats=[]
        timer=QTimer()
        timer.timeout.connect(lambda:beats.append(True))
        timer.start(2)
        self.addCleanup(timer.stop)
        with patch.object(LibraryRepository,'prepare_duplicate_search',blocked):
            worker.start()
            self.assertTrue(entered.wait(1))
            worker.stop()
            self.assertTrue(pump(lambda:len(beats)>=2))
            self.assertFalse(receiver.values)
            release.set()
            result=self.finish(worker,receiver,native)
        self.assertFalse(result['cancelled'])
        self.assertIsNone(result['error'])
        self.assertIs(result['value'],True)
        self.assertTrue(self.service.read('get_duplicate_search_status')['prepared'])

    def test_unconfirmed_future_and_query_exception_are_visible(self):
        worker=NativeDuplicateQueryWorker(self.service,'prepare_duplicate_search')
        receiver,native=self.observe(worker)
        future=Future()
        future.set_result(False)
        with patch.object(self.service,'submit',return_value=future):
            worker.start()
            result=self.finish(worker,receiver,native)
        self.assertEqual(result['error'],'duplicate_index_prepare_unconfirmed')
        worker=NativeDuplicateQueryWorker(self.service,'get_duplicate_candidates_page')
        receiver,native=self.observe(worker)
        with patch.object(self.service,'read',side_effect=ValueError('fixture query failure')):
            worker.start()
            result=self.finish(worker,receiver,native)
        self.assertEqual(result['error'],'fixture query failure')
        self.assertIsNone(result['value'])

    def test_group_verification_publishes_complete_curated_preference(self):
        raw,curated=self.pair()
        worker=DatabaseGroupReviewWorker(str(self.root),self.service,prior.DIGEST,expected_count=2,token=19)
        receiver,native=self.observe(worker)
        worker.start()
        result=self.finish(worker,receiver,native)
        self.assertEqual((result['scope'],result['mode'],result['token']),('database','hash',19))
        self.assertEqual(result['reports'][prior.DIGEST]['remove'],(raw.as_posix(),))
        self.assertTrue(result['complete'])

    def test_group_stop_does_not_publish_partial_approval(self):
        self.pair()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        def blocked(path,event):
            entered.set()
            self.assertTrue(release.wait(3))
            return _probe(path,event)
        worker=DatabaseGroupReviewWorker(str(self.root),self.service,prior.DIGEST)
        receiver,native=self.observe(worker)
        with patch('modules.duplicate_finder.logic.database_review._probe',blocked):
            worker.start()
            self.assertTrue(entered.wait(1))
            worker.stop()
            release.set()
            result=self.finish(worker,receiver,native)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['reports'],{})

    def test_constructor_rejects_unrelated_operation_without_io(self):
        for operation in ('archive_copy','remove_files','get_all_tags'):
            with self.assertRaises(ValueError):
                NativeDuplicateQueryWorker(self.service,operation)
        with self.assertRaises(ValueError):
            NativeDuplicateQueryWorker(self.service,'prepare_duplicate_search',unexpected=True)


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('Workers.'+name,sys.modules[__name__])
        for name in Workers.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
