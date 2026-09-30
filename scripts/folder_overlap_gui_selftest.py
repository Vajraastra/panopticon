"""Reporte Qt de pares; sólo fixtures, ninguna fusión ni movimiento."""
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication,QPushButton,QPlainTextEdit,QFileDialog
from shiboken6 import delete
from curation_snapshot_selftest import pump
import duplicate_gui_selftest as prior


class OverlapGUI(prior.GUI):
    def enter(self,folders=None):
        self.module.combo_mode.setCurrentIndex(3)
        self.module.overlap_panel.folders.setPlainText('\n'.join(folders or [(self.root/name).as_posix() for name in ('A','Z')]))
    def compare(self):
        self.module.run_scan()
        self.assertTrue(pump(lambda:self.module.native_worker is None))
        return self.module.overlap_panel.result

    def test_report_classifies_pairs_and_preserves_all_native_curation_and_files(self):
        raw,curated=self.pair()
        unknown=self.file('A/unknown.png',register=False)
        self.register(unknown.as_posix())
        before=self.service.read('get_duplicate_snapshot',[raw.as_posix(),curated.as_posix()])
        self.enter()
        with patch.object(self.service,'submit',side_effect=AssertionError('report wrote')):
            result=self.compare()
        self.assertEqual(result['pairs'][0]['classification'],'high_overlap')
        self.assertEqual(result['folders'][0]['unknown_copies'],1)
        self.assertEqual(self.module.overlap_panel.tree.topLevelItemCount(),1)
        self.assertFalse(self.module.btn_delete.isEnabled())
        self.assertFalse(self.module.btn_auto_select.isEnabled())
        self.assertTrue(self.module.btn_delete.isHidden())
        self.assertFalse(self.service.read('get_duplicate_search_status')['prepared'])
        self.assertEqual(self.service.read('get_duplicate_snapshot',[raw.as_posix(),curated.as_posix()]),before)
        self.assertTrue(raw.exists() and curated.exists())

    def test_complete_batch_32_scopes_has_496_visible_pairs_even_if_empty(self):
        self.enter([f'E:/fixture/profile-{i}' for i in range(32)])
        result=self.compare()
        self.assertEqual(len(result['pairs']),496)
        self.assertEqual(self.module.overlap_panel.tree.topLevelItemCount(),496)
        self.assertTrue(all(p['classification']=='insufficient_hashes' for p in result['pairs']))

    def test_threshold_or_folder_change_invalidates_report_and_requeries_full_scope(self):
        self.pair()
        self.file('Z/different.png',data=b'different')
        self.enter()
        self.assertEqual(self.compare()['pairs'][0]['classification'],'partial_overlap')
        self.module.overlap_panel.threshold.setValue(50)
        self.assertIsNone(self.module.overlap_panel.result)
        self.assertEqual(self.module.overlap_panel.tree.topLevelItemCount(),0)
        self.assertEqual(self.compare()['pairs'][0]['classification'],'high_overlap')
        self.module.overlap_panel.folders.clear()
        self.assertIsNone(self.module.overlap_panel.result)
        self.assertFalse(self.module.btn_run.isEnabled())

    def test_nested_scopes_show_human_error_without_partial_report_and_allow_retry(self):
        self.pair()
        self.enter([self.root.as_posix(),(self.root/'A').as_posix()])
        self.assertIsNone(self.compare())
        self.assertIn('anidadas',self.module.status.text())
        self.assertTrue(self.module.btn_run.isEnabled())
        self.enter()
        self.assertEqual(self.compare()['pairs'][0]['jaccard'],1)

    def test_read_budget_failure_publishes_no_prefix_and_reenables_controls(self):
        self.pair()
        self.file('A/another.png')
        self.enter()
        with patch('core.folder_overlap.MAX_ACTIVE_COPIES',2):
            self.assertIsNone(self.compare())
        self.assertEqual(self.module.overlap_panel.tree.topLevelItemCount(),0)
        self.assertIn('no se publicó un reporte parcial',self.module.status.text())
        self.assertTrue(self.module.btn_run.isEnabled())
        self.assertTrue(self.module.overlap_panel.threshold.isEnabled())

    def test_selection_and_complete_details_do_no_gui_sql_or_filesystem(self):
        self.pair()
        self.enter()
        self.compare()
        with (patch.object(self.service,'read',side_effect=AssertionError('GUI SQL')),
              patch.object(Path,'stat',side_effect=AssertionError('GUI filesystem'))):
            self.module.overlap_panel.tree.setCurrentItem(self.module.overlap_panel.tree.topLevelItem(0))
            value=self.module.overlap_panel.details.toPlainText()
        self.assertIn('Jaccard: 100.00%',value)
        self.assertIn('Copias activas: 1',value)
        self.assertIn('Hashes únicos: 1',value)
        self.assertIn('Con hash: 1 · Desconocidas: 0',value)
        self.assertIn(self.root.as_posix(),value)

    def test_background_query_double_start_guard_capture_and_cancel_keep_qt_responsive(self):
        self.pair()
        self.enter()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        native=self.service.read
        threads=[]
        main=threading.get_ident()
        def blocked(operation,*args,**kwargs):
            threads.append(threading.get_ident())
            entered.set()
            self.assertTrue(release.wait(3))
            return native(operation,*args,**kwargs)
        with patch.object(self.service,'read',blocked):
            self.module.run_scan()
            self.assertTrue(entered.wait(1))
            worker=self.module.native_worker
            captured=worker.arguments['folders'][:]
            self.module.run_scan()
            self.assertIs(self.module.native_worker,worker)
            self.assertFalse(self.module.overlap_panel.folders.isEnabled())
            self.assertFalse(self.module.shutdown_ready())
            ticks=[]
            QTimer.singleShot(0,lambda:ticks.append(True))
            self.assertTrue(pump(lambda:ticks))
            self.module.overlap_panel.folders.setPlainText('E:/new-a\nE:/new-b')
            self.assertEqual(worker.arguments['folders'],captured)
            self.module.cancel()
            release.set()
            self.assertTrue(pump(lambda:self.module.native_worker is None))
        self.assertNotIn(main,threads)
        self.assertIsNone(self.module.overlap_panel.result)
        self.assertTrue(self.module.btn_run.isEnabled())

    def test_dashboard_cancels_read_but_waits_nonblocking_before_owner_shutdown(self):
        self.pair()
        self.enter()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        native=self.service.read
        def blocked(operation,*args,**kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return native(operation,*args,**kwargs)
        host=self.dashboard()
        with (patch.object(self.service,'read',blocked),patch('main.close_library_services') as close_owner):
            self.module.run_scan()
            self.assertTrue(entered.wait(1))
            event=QCloseEvent()
            started=time.monotonic()
            host.closeEvent(event)
            self.assertLess(time.monotonic()-started,.1)
            self.assertFalse(event.isAccepted())
            self.assertFalse(close_owner.called)
            release.set()
            self.assertTrue(pump(lambda:host._shutdown_finished))
            close_owner.assert_called_once_with(permanent=True)
        self.assertIsNone(self.module.overlap_panel.result)

    def test_widget_destruction_retains_worker_until_terminal_and_drops_late_signal(self):
        self.pair()
        self.enter()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        native=self.service.read
        def blocked(operation,*args,**kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return native(operation,*args,**kwargs)
        with patch.object(self.service,'read',blocked):
            self.module.run_scan()
            self.assertTrue(entered.wait(1))
            delete(self.view)
            self.assertTrue(self.module._closed)
            release.set()
            self.assertTrue(pump(lambda:self.module.native_worker is None))
        self.assertTrue(self.module.shutdown_ready())

    def test_picker_only_adds_scope_and_controls_have_localized_meaningful_tips(self):
        self.enter()
        with patch.object(QFileDialog,'getExistingDirectory',return_value='E:/other'):
            self.module.overlap_panel.choose_folder()
        self.assertEqual(len(self.module.overlap_panel.paths()),3)
        self.assertEqual(self.service.read('count_files'),0)
        self.assertEqual(self.module.btn_run.text(),'Comparar pares de carpetas')
        for button in self.module.overlap_panel.findChildren(QPushButton):
            self.assertTrue(button.toolTip())
            self.assertNotEqual(button.text(),button.toolTip())
        for field in self.module.overlap_panel.findChildren(QPlainTextEdit):self.assertTrue(field.toolTip())
        self.module.overlap_panel.folders.setPlainText('\n'.join(f'E:/f{i}' for i in range(33)))
        self.assertFalse(self.module.btn_run.isEnabled())
        self.assertIsNone(self.compare())
        self.assertIn('32 carpetas',self.module.status.text())


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('OverlapGUI.'+name,sys.modules[__name__])
        for name in OverlapGUI.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
