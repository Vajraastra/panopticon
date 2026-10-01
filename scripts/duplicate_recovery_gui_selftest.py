"""Revisión explícita de diarios Qt: no escrituras, cierre y evidencia localizada."""
from pathlib import Path
import sys,threading,time,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication,QLineEdit,QPushButton,QFileDialog
from shiboken6 import delete
from curation_snapshot_selftest import pump
from modules.duplicate_finder.logic.recovery_review import _physical
from modules.duplicate_finder.module import _LIVE_WORKERS
from modules.duplicate_finder.ui.recovery_panel import RecoveryPanel
import duplicate_gui_selftest as prior
import duplicate_recovery_selftest as backend


class RecoveryGUI(prior.GUI):
    retire=backend.Recovery.retire

    def prepare(self,failure=False):
        raw,keeper=self.pair()
        self.recovery=(Path(self.temp.name)/'recovery').resolve()
        self.retire(raw,failure=failure)
        self.module.combo_mode.setCurrentIndex(5)
        self.module.recovery_panel.journal.setText(str(self.journal))
        return raw,keeper

    def run_review(self):
        self.module.run_scan()
        self.assertTrue(pump(lambda:self.module.recovery_review_worker is None))
        self.assertFalse(self.module._recovery_busy)
        return self.module.recovery_panel.result

    def blocked(self):
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        def slow(*args):
            entered.set();self.assertTrue(release.wait(3));return _physical(*args)
        return entered,release,slow

    def test_mode_is_explicit_readonly_and_displays_ids_paths_without_retirement_controls(self):
        raw,keeper=self.prepare()
        original=self.journal.read_bytes()
        self.assertIsNone(self.module.recovery_review_worker)
        self.assertIsNone(self.module.recovery_panel.result)
        threads=[];read=self.service.read
        def recorded(*args,**kwargs):threads.append(threading.get_ident());return read(*args,**kwargs)
        with patch.object(self.service,'submit',side_effect=AssertionError('review writes')),patch.object(self.service,'read',recorded):value=self.run_review()
        self.assertTrue(value['complete']);self.assertEqual(value['verified_retired'],1)
        self.assertNotIn(threading.get_ident(),threads)
        self.assertEqual(self.module.recovery_panel.tree.topLevelItem(0).text(1),'Retiro verificado')
        self.assertIn(raw.as_posix(),self.module.recovery_panel.details.toPlainText())
        self.assertIn(keeper.as_posix(),self.module.recovery_panel.details.toPlainText())
        self.assertIn('1 retiros verificados',self.module.status.text())
        self.assertEqual(self.journal.read_bytes(),original);self.assertEqual(self.events,[])
        self.assertTrue(self.module.btn_delete.isHidden());self.assertFalse(self.module.btn_delete.isEnabled())
        self.assertTrue(self.module.btn_auto_select.isHidden());self.assertIsNone(self.module.review)

    def test_incomplete_move_is_human_pending_evidence_without_source_changes(self):
        raw,_=self.prepare(failure=True);value=self.run_review()
        self.assertTrue(value['complete']);self.assertTrue(value['review_required'])
        self.assertEqual(self.module.recovery_panel.tree.topLevelItem(0).text(1),'ID offline; original aún presente')
        self.assertIn('1 pendientes de revisión',self.module.status.text())
        self.assertEqual(raw.read_bytes(),b'same bytes')

    def test_bad_journal_has_visible_error_no_prefix_and_retry_enabled(self):
        self.prepare();self.journal.write_bytes(b'{invalid json')
        value=self.run_review();self.assertFalse(value['complete'])
        self.assertEqual(self.module.recovery_panel.tree.topLevelItemCount(),0)
        self.assertIn('requiere revisión manual',self.module.status.text())
        self.assertTrue(self.module.btn_run.isEnabled());self.assertFalse(self.module.btn_delete.isEnabled())

    def test_double_start_cancel_heartbeat_and_native_release(self):
        self.prepare();entered,release,slow=self.blocked()
        with patch('modules.duplicate_finder.logic.recovery_review._physical',slow):
            self.module.run_scan();self.assertTrue(entered.wait(1))
            worker=self.module.recovery_review_worker;self.module.run_scan()
            self.assertIs(self.module.recovery_review_worker,worker)
            self.assertFalse(self.module.combo_mode.isEnabled());self.assertFalse(self.module.recovery_panel.journal.isEnabled())
            self.assertFalse(self.module.shutdown_ready());terminals=[];worker.finished_signal.connect(terminals.append)
            ticks=[];QTimer.singleShot(0,lambda:ticks.append(True));start=time.monotonic();self.module.cancel()
            self.assertLess(time.monotonic()-start,.1);self.assertTrue(pump(lambda:ticks))
            release.set();self.assertTrue(pump(lambda:self.module.recovery_review_worker is None))
        self.assertEqual(len(terminals),1);self.assertTrue(self.module.recovery_panel.result['cancelled'])
        self.assertTrue(self.module.shutdown_ready());self.assertTrue(self.module.btn_run.isEnabled())

    def test_programmatic_path_change_drops_old_result_and_does_not_restart(self):
        self.prepare();entered,release,slow=self.blocked()
        with patch('modules.duplicate_finder.logic.recovery_review._physical',slow):
            self.module.run_scan();self.assertTrue(entered.wait(1))
            self.module.recovery_panel.journal.setText(str(self.journal)+'-different')
            release.set();self.assertTrue(pump(lambda:self.module.recovery_review_worker is None))
        self.assertIsNone(self.module.recovery_panel.result)
        self.assertIn('Elige un diario',self.module.status.text())
        self.module.recovery_panel.journal.clear();self.assertFalse(self.module.btn_run.isEnabled())

    def test_destroyed_view_retains_worker_and_drops_late_ui(self):
        self.prepare();entered,release,slow=self.blocked()
        with patch('modules.duplicate_finder.logic.recovery_review._physical',slow):
            self.module.run_scan();self.assertTrue(entered.wait(1));worker=self.module.recovery_review_worker
            delete(self.view);self.assertTrue(self.module._closed);self.assertIn(worker,_LIVE_WORKERS)
            release.set();self.assertTrue(pump(lambda:self.module.recovery_review_worker is None))
        self.assertTrue(self.module.shutdown_ready());self.assertNotIn(worker,_LIVE_WORKERS)

    def test_dashboard_waits_for_cancelled_reader_without_blocking_or_closing_owner_early(self):
        self.prepare();entered,release,slow=self.blocked();host=self.dashboard()
        with patch('modules.duplicate_finder.logic.recovery_review._physical',slow),patch('main.close_library_services') as close_owner:
            self.module.run_scan();self.assertTrue(entered.wait(1));event=QCloseEvent();start=time.monotonic();host.closeEvent(event)
            self.assertLess(time.monotonic()-start,.1);self.assertFalse(event.isAccepted());self.assertFalse(close_owner.called)
            ticks=[];QTimer.singleShot(0,lambda:ticks.append(True));self.assertTrue(pump(lambda:ticks))
            release.set();self.assertTrue(pump(lambda:host._shutdown_finished));close_owner.assert_called_once_with(permanent=True)
        self.assertIsNone(self.module.recovery_panel.result)

    def test_choose_only_captures_path_and_panel_display_has_no_gui_sql_or_stat(self):
        self.prepare();value=self.run_review()
        self.module.recovery_panel.journal.clear()
        with patch.object(QFileDialog,'getOpenFileName',return_value=(str(self.journal),'Recovery journal')):
            self.module.recovery_panel.choose.click()
        self.assertIsNone(self.module.recovery_review_worker);self.assertIsNone(self.module.recovery_panel.result)
        with patch.object(self.service,'read',side_effect=AssertionError('GUI SQL')),patch.object(Path,'stat',side_effect=AssertionError('GUI stat')):
            self.module.recovery_panel.show_result(value,self.module.reason)
            self.module.recovery_panel.tree.setCurrentItem(self.module.recovery_panel.tree.topLevelItem(0))
        for control in self.view.findChildren(QLineEdit)+self.view.findChildren(QPushButton):self.assertTrue(control.toolTip())
        language=prior.Language('en')
        panel=RecoveryPanel(lambda key,default:language.tr('dup.'+key,default),self.module.accent_color)
        panel.show_result(value,self.module.reason)
        self.assertEqual(panel.tree.topLevelItem(0).text(1),'Retirement verified')
        panel.deleteLater()

    def test_missing_service_is_human_error_and_does_not_start_worker(self):
        self.prepare();self.module.context['data_service']=None;self.module.run_scan()
        self.assertIsNone(self.module.recovery_review_worker)
        self.assertIn('servicio de biblioteca nativa',self.module.status.text())


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('RecoveryGUI.'+name,sys.modules[__name__]) for name in RecoveryGUI.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
