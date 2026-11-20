"""Offscreen recovery consent, cancellation and runtime delivery; no real data."""
import asyncio
from concurrent.futures import Future
from pathlib import Path
import sys
from threading import Event,Thread,get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication,QMessageBox,QPushButton,QWidget
from modules.cherry.logic.archive_batch import MaintenanceBlocked
from modules.cherry.ui.view import CherryView

APP=QApplication.instance() or QApplication([])
FOLDER='C:/private/recovery-project'
OPID='00000000-0000-0000-0000-000000000123'
TOKEN='a'*64
COUNTS=dict(entries=8,remaining=3,admissions=2,packages_deleted=0)


class Runtime:
    def __init__(self):self.jobs=[];self.stopped=False
    def start(self):return True
    def submit(self,coro):
        future=Future();self.jobs.append((coro,future));return future
    def request_stop(self):self.stopped=True
    def close(self):
        for coro,_ in self.jobs:coro.close()


class View(CherryView):
    def __init__(self):
        QWidget.__init__(self)
        self._lm=None;self._closing=False;self._maintenance_busy=False
        self._maintenance_future=None;self._maintenance_cancel_event=None;self._maintenance_started_event=None
        self._maintenance_generation=3;self._preparing_download=False;self._archive_check_busy=False
        self._send_worker=None;self._queue=SimpleNamespace(is_running=False,cancel=lambda:False)
        self._bridge=SimpleNamespace(is_busy=False,cancel=lambda:False)
        self._runtime=Runtime();self.logs=[];self.announced=[];self.status='';self.reloads=0
        self._panel=SimpleNamespace(append_log=lambda *args:self.logs.append(args),set_phase=lambda value:None)
        self._btn_console=QPushButton();self._btn_cancel=QPushButton()
        self.context={'data_service':SimpleNamespace(read=lambda *args:dict(operation=dict(root=FOLDER,phase='review')))}
        self._archives_finished.connect(self._on_archives_finished)
        self._maintenance_recovery_ready.connect(self._on_maintenance_recovery_ready)
    def _update_actions(self):pass
    def _on_active_profile_changed(self,value):pass
    def _set_status(self,text,*args,**kwargs):self.status=text
    def _reload_detail(self):self.reloads+=1
    def _announce_folder(self,value):self.announced.append(value['folder_path'])


class RecoveryGUI(unittest.TestCase):
    def setUp(self):
        self.view=View();self.addCleanup(self.view.close);self.addCleanup(self.view._runtime.close)

    def proposal(self):
        return dict(folder=FOLDER,operation_id=OPID,review_token=TOKEN,counts=dict(COUNTS),generation=3)

    def ask(self,answer):
        def execute(box):
            self.assertEqual(box.defaultButton(),box.button(QMessageBox.StandardButton.Cancel))
            self.assertTrue(box.button(QMessageBox.StandardButton.Yes).toolTip())
            self.assertTrue(box.button(QMessageBox.StandardButton.Cancel).toolTip())
            self.assertIn(OPID,box.text());self.assertIn('3 remaining',box.text())
            self.assertFalse(self.view._maintenance_busy)
            return answer
        return patch.object(QMessageBox,'exec',execute)

    def test_cancel_default_creates_no_resume_job(self):
        with self.ask(QMessageBox.StandardButton.Cancel):
            self.view._on_maintenance_recovery_ready(self.proposal())
        self.assertEqual(self.view._runtime.jobs,[])
        self.assertFalse(self.view._maintenance_busy)

    def test_yes_uses_exact_review_token_and_announces_only_after_confirmation(self):
        with self.ask(QMessageBox.StandardButton.Yes):
            self.view._on_maintenance_recovery_ready(self.proposal())
        self.assertEqual(self.view.announced,[])
        self.assertTrue(self.view._maintenance_busy)
        event=self.view._maintenance_cancel_event
        report=dict(operation_id=OPID,complete=True,counts=COUNTS,replayed_engine_plan=False)
        with patch('modules.cherry.logic.native_maintenance.resume_native_recovery',new=AsyncMock(return_value=report)) as resume:
            asyncio.run(self.view._runtime.jobs[0][0])
        self.assertEqual(resume.call_args.args,(FOLDER,OPID,TOKEN))
        self.assertIs(resume.call_args.kwargs['cancel_event'],event)
        self.assertIs(resume.call_args.kwargs['data_service'],self.view.context['data_service'])
        self.assertEqual(self.view.announced,[FOLDER])
        self.assertFalse(self.view._maintenance_busy);self.assertIsNone(self.view._maintenance_cancel_event)

    def test_closing_busy_or_stale_proposal_never_opens_dialog(self):
        for name,value in [('_closing',True),('_maintenance_busy',True),('_maintenance_generation',4)]:
            previous=getattr(self.view,name);setattr(self.view,name,value)
            with patch.object(QMessageBox,'exec',side_effect=AssertionError('unexpected dialog')):
                self.view._on_maintenance_recovery_ready(self.proposal())
            setattr(self.view,name,previous)
        self.assertEqual(self.view._runtime.jobs,[])

    def test_failed_resume_or_changed_review_does_not_announce_success(self):
        with self.ask(QMessageBox.StandardButton.Yes):
            self.view._on_maintenance_recovery_ready(self.proposal())
        with patch('modules.cherry.logic.native_maintenance.resume_native_recovery',new=AsyncMock(side_effect=ValueError('recovery_review_changed'))):
            asyncio.run(self.view._runtime.jobs[0][0])
        self.assertEqual(self.view.announced,[])
        self.assertIn('recovery_review_changed',self.view.status)
        self.assertNotIn('completed',self.view.status)

    def test_worker_reviews_current_native_root_then_delivers_after_clear_busy(self):
        self.view._maintenance_busy=True;event=Event();self.view._maintenance_cancel_event=event
        gui_thread=get_ident();observed=[]
        def review(folder,opid,**kwargs):
            self.assertNotEqual(get_ident(),gui_thread)
            observed.append((folder,opid,kwargs['cancel_event']))
            return dict(review_token=TOKEN,counts=COUNTS)
        blocked=AsyncMock(side_effect=MaintenanceBlocked('recovery_required',operation_id=OPID))
        with patch('modules.cherry.logic.archive_batch.process_unindexed_project',new=blocked), \
                patch('modules.cherry.logic.native_maintenance.review_native_recovery',side_effect=review), \
                self.ask(QMessageBox.StandardButton.Cancel):
            thread=Thread(target=lambda:asyncio.run(self.view._archives_async('C:/stale/input','Artist',event)))
            thread.start();thread.join(5);self.assertFalse(thread.is_alive());APP.processEvents()
        self.assertEqual(observed,[(FOLDER,OPID,event)])
        self.assertFalse(self.view._maintenance_busy)
        self.assertEqual(self.view.announced,[])

    def test_batch_delivers_same_review_and_passes_cancel_event(self):
        event=Event();deliveries=[];self.view._maintenance_recovery_ready.disconnect()
        self.view._maintenance_recovery_ready.connect(deliveries.append)
        with patch('modules.cherry.logic.archive_batch.process_profiles',new=AsyncMock(side_effect=MaintenanceBlocked('recovery_required',operation_id=OPID))) as call, \
                patch('modules.cherry.logic.native_maintenance.review_native_recovery',return_value=dict(review_token=TOKEN,counts=COUNTS)):
            asyncio.run(self.view._archives_batch_async([],event))
        self.assertIs(call.call_args.kwargs['cancel_event'],event)
        self.assertEqual(deliveries,[self.proposal()])

    def test_cancel_signals_worker_before_cancelling_future(self):
        event=Event();started=Event();started.set()
        self.view._maintenance_cancel_event=event;self.view._maintenance_started_event=started
        self.view._maintenance_future=SimpleNamespace(cancel=lambda:self.assertTrue(event.is_set()))
        self.view._cancel();self.assertTrue(event.is_set())
        event.clear();self.view.shutdown();self.assertTrue(event.is_set());self.assertTrue(self.view._runtime.stopped)

    def test_cancel_before_admission_drains_without_starting_backend(self):
        event=Event();started=Event();future=Future()
        self.view._maintenance_busy=True;self.view._maintenance_cancel_event=event
        self.view._maintenance_started_event=started;self.view._maintenance_future=future
        self.view._cancel();self.assertTrue(event.is_set());self.assertFalse(future.cancelled())
        with patch('modules.cherry.logic.archive_batch.process_unindexed_project',new=AsyncMock()) as process:
            asyncio.run(self.view._archives_async(FOLDER,'Artist',event,started))
        process.assert_not_called();self.assertFalse(self.view._maintenance_busy)
        self.assertIsNone(self.view._maintenance_cancel_event)

    def test_cancelled_or_failed_review_produces_no_popup(self):
        event=Event();event.set()
        blocked=MaintenanceBlocked('recovery_required',operation_id=OPID)
        with patch('modules.cherry.logic.native_maintenance.review_native_recovery',side_effect=AssertionError('cancelled review')):
            self.assertIsNone(self.view._review_maintenance_recovery(blocked,event,3))
        event.clear()
        with self.assertLogs('modules.cherry.ui.view',level='ERROR'), \
                patch('modules.cherry.logic.native_maintenance.review_native_recovery',side_effect=RuntimeError('backend failure')):
            self.assertIsNone(self.view._review_maintenance_recovery(blocked,event,3))
        self.assertEqual(self.view.announced,[])

    def test_complete_native_operation_can_review_pending_catalog_publication(self):
        self.view.context['data_service'].read=lambda *args:dict(operation=dict(root=FOLDER,phase='complete'))
        event=Event();blocked=MaintenanceBlocked('recovery_required',operation_id=OPID)
        with patch('modules.cherry.logic.native_maintenance.review_native_recovery',return_value=dict(review_token=TOKEN,counts=COUNTS)):
            self.assertEqual(self.view._review_maintenance_recovery(blocked,event,3),self.proposal())


if __name__=='__main__':unittest.main(verbosity=2)
