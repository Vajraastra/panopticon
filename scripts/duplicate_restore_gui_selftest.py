"""Restauración Qt explícita: aprobación capturada, owner vivo yresultado confirmado."""
from concurrent.futures import Future
import json,sys,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication,QMessageBox,QPushButton
from shiboken6 import delete
from core.library_repository import LibraryRepository
from curation_snapshot_selftest import pump
from modules.duplicate_finder.module import _LIVE_WORKERS
import duplicate_recovery_gui_selftest as prior


class RestoreGUI(prior.RecoveryGUI):
    def ready(self,failure=False):
        source,keeper=self.prepare(failure)
        self.run_review()
        self.assertTrue(self.module.recovery_panel.restore.isEnabled())
        return source,keeper

    def launch(self):
        with patch.object(self.module,'_confirm_restore',return_value=True):self.module.restore_selected_copy()
        worker=self.module.recovery_restore_worker
        self.assertIsNotNone(worker)
        terminal=[];worker.finished_signal.connect(terminal.append)
        return worker,terminal

    def finish(self,terminal):
        self.assertTrue(pump(lambda:self.module.recovery_restore_worker is None))
        self.assertEqual(len(terminal),1);self.assertFalse(self.module._acting)
        return terminal[0]

    def held_owner(self):
        entered,release=threading.Event(),threading.Event();self.releases.append(release)
        restore=LibraryRepository.restore_duplicate_copy
        def hold(repo,*args,**kwargs):
            value=restore(repo,*args,**kwargs);entered.set();self.assertTrue(release.wait(3));return value
        return entered,release,hold

    def test_confirmed_restore_preserves_same_id_curated_keeper_receipts_and_refreshes_source_parent(self):
        source,keeper=self.ready()
        copy_id=self.module.recovery_panel.selected_restore()['copy_id']
        receipt=self.root/'catalog.db';receipt.write_bytes(b'Cherry hashes/URL/alias receipts')
        original=self.journal.read_bytes()
        worker,terminal=self.launch();value=self.finish(terminal)
        self.assertTrue(value['committed']);self.assertTrue(value['complete'])
        self.assertEqual(value['copy_id'],copy_id);self.assertEqual(source.read_bytes(),keeper.read_bytes())
        self.assertEqual(receipt.read_bytes(),b'Cherry hashes/URL/alias receipts');self.assertEqual(self.journal.read_bytes(),original)
        self.assertEqual(self.events,[source.parent.as_posix()]);self.assertNotEqual(self.events,[self.root.as_posix()])
        self.assertIn('restaurada conservando su curación',self.module.status.text())
        self.assertIn('Restauración nativa confirmada: Sí',self.module.recovery_panel.details.toPlainText())
        self.assertIn(value['operation_journal'],self.module.recovery_panel.details.toPlainText())
        self.assertIsNone(self.module.recovery_panel.result);self.assertFalse(self.module.recovery_panel.restore.isEnabled())
        self.assertTrue(self.module.btn_run.isEnabled());self.assertFalse(self.module.btn_delete.isEnabled())
        self.run_review();self.assertEqual(self.module.recovery_panel.tree.topLevelItem(0).text(1),'Activa en ruta original')
        self.assertFalse(self.module.recovery_panel.restore.isEnabled())

    def test_partial_retirement_source_present_reactivates_without_file_move(self):
        source,_=self.ready(failure=True)
        worker,terminal=self.launch();value=self.finish(terminal)
        self.assertTrue(value['committed']);self.assertFalse(value['moved']);self.assertEqual(source.read_bytes(),b'same bytes')
        self.assertIn('Archivo devuelto: No',self.module.recovery_panel.details.toPlainText())

    def test_declined_concrete_confirmation_preserves_approval_and_has_no_effects(self):
        source,_=self.ready();before=list(self.recovery.glob('*/restore-*.json'))
        def inspect(box):
            self.assertIn(str(source.as_posix()),box.text());self.assertIn('Conserva el mismo ID',box.text())
            self.assertEqual(box.textFormat(),Qt.PlainText);self.assertEqual(box.defaultButton(),box.button(QMessageBox.No))
            self.assertEqual(box.button(QMessageBox.Yes).text(),'Restaurar');self.assertEqual(box.button(QMessageBox.No).text(),'Cancelar')
            return QMessageBox.No
        with patch.object(QMessageBox,'exec',inspect):self.module.recovery_panel.restore.click()
        self.assertIsNone(self.module.recovery_restore_worker);self.assertFalse(source.exists())
        self.assertEqual(list(self.recovery.glob('*/restore-*.json')),before)
        self.assertTrue(self.module.recovery_panel.restore.isEnabled());self.assertEqual(self.events,[])

    def test_path_change_inside_modal_consumes_approval_before_worker_admission(self):
        source,_=self.ready()
        def changed(arguments):
            self.module.recovery_panel.journal.setText(str(self.journal)+'-different');return True
        with patch.object(self.module,'_confirm_restore',changed):self.module.restore_selected_copy()
        self.assertIsNone(self.module.recovery_restore_worker);self.assertFalse(source.exists())
        self.assertIsNone(self.module.recovery_panel.result);self.assertFalse(self.module.recovery_panel.restore.isEnabled())

    def test_selected_copy_change_inside_modal_cannot_redirect_captured_action(self):
        raw,keeper=self.pair();extra=self.file('B/extra.png')
        self.recovery=(Path(self.temp.name)/'recovery').resolve();self.retire(raw,extra)
        self.module.combo_mode.setCurrentIndex(5);self.module.recovery_panel.journal.setText(str(self.journal));self.run_review()
        self.assertEqual(self.module.recovery_panel.tree.topLevelItemCount(),2)
        def changed(arguments):
            self.module.recovery_panel.tree.setCurrentItem(self.module.recovery_panel.tree.topLevelItem(1));return True
        with patch.object(self.module,'_confirm_restore',changed):self.module.restore_selected_copy()
        self.assertIsNone(self.module.recovery_restore_worker);self.assertFalse(raw.exists());self.assertFalse(extra.exists())

    def test_double_start_and_cancel_after_acceptance_finishes_native_commit_without_blocking(self):
        source,_=self.ready();entered,release,hold=self.held_owner()
        with patch.object(LibraryRepository,'restore_duplicate_copy',hold):
            worker,terminal=self.launch();self.assertTrue(entered.wait(1))
            self.module.restore_selected_copy();self.assertIs(self.module.recovery_restore_worker,worker)
            self.assertFalse(self.module.combo_mode.isEnabled());self.assertFalse(self.module.recovery_panel.restore.isEnabled())
            self.assertFalse(self.module.shutdown_ready());self.assertTrue(source.exists())
            self.assertEqual(self.service.read('count_files'),1)  # owner no ha confirmado COMMIT.
            ticks=[];QTimer.singleShot(0,lambda:ticks.append(True));started=time.monotonic();self.module.cancel()
            self.assertLess(time.monotonic()-started,.1);self.assertTrue(pump(lambda:ticks));release.set();value=self.finish(terminal)
        self.assertTrue(value['committed']);self.assertFalse(value['cancelled']);self.assertEqual(self.service.read('count_files'),2)

    def test_dashboard_keeps_owner_alive_until_accepted_restore_finishes(self):
        source,_=self.ready();entered,release,hold=self.held_owner();host=self.dashboard()
        with patch.object(LibraryRepository,'restore_duplicate_copy',hold),patch('main.close_library_services') as close_owner:
            worker,terminal=self.launch();self.assertTrue(entered.wait(1));event=QCloseEvent();started=time.monotonic();host.closeEvent(event)
            self.assertLess(time.monotonic()-started,.1);self.assertFalse(event.isAccepted());self.assertFalse(close_owner.called)
            self.assertFalse(self.module.shutdown_ready());ticks=[];QTimer.singleShot(0,lambda:ticks.append(True));self.assertTrue(pump(lambda:ticks))
            release.set();self.assertTrue(pump(lambda:host._shutdown_finished));close_owner.assert_called_once_with(permanent=True)
        self.assertTrue(source.exists());self.assertEqual(self.service.read('count_files'),2)
        operations=list(self.recovery.glob('*/restore-*.json'));self.assertEqual(len(operations),1)
        self.assertTrue(json.loads(operations[0].read_bytes())['committed']);self.assertEqual(self.events,[])

    def test_destroyed_view_preserves_worker_and_admitted_restore_without_late_widget_access(self):
        source,_=self.ready();entered,release,hold=self.held_owner()
        with patch.object(LibraryRepository,'restore_duplicate_copy',hold):
            worker,terminal=self.launch();self.assertTrue(entered.wait(1));delete(self.view)
            self.assertTrue(self.module._closed);self.assertIn(worker,_LIVE_WORKERS);release.set()
            self.assertTrue(pump(lambda:self.module.recovery_restore_worker is None))
        self.assertEqual(self.service.read('count_files'),2);self.assertTrue(source.exists());self.assertNotIn(worker,_LIVE_WORKERS)

    def test_stale_terminal_drops_widgets_but_publishes_only_affected_parent_refresh(self):
        source,_=self.ready();entered,release,hold=self.held_owner()
        with patch.object(LibraryRepository,'restore_duplicate_copy',hold):
            worker,terminal=self.launch();self.assertTrue(entered.wait(1))
            self.module.recovery_panel.journal.setText(str(self.journal)+'-different');release.set();value=self.finish(terminal)
        self.assertTrue(value['committed']);self.assertIsNone(self.module.recovery_panel.result)
        self.assertIn('Elige un diario',self.module.status.text());self.assertEqual(self.events,[source.parent.as_posix()])

    def test_unconfirmed_commit_has_human_partial_counts_and_requires_new_review_before_retry(self):
        source,_=self.ready();failed=Future();failed.set_result(False)
        with patch.object(self.service,'submit',return_value=failed):worker,terminal=self.launch();value=self.finish(terminal)
        self.assertTrue(value['moved']);self.assertFalse(value['committed']);self.assertTrue(source.exists())
        self.assertIn('El commit nativo no se confirmó',self.module.status.text())
        self.assertIn('Restauración nativa confirmada: No',self.module.recovery_panel.details.toPlainText())
        self.assertFalse(self.module.recovery_panel.restore.isEnabled());self.assertEqual(self.events,[source.parent.as_posix()])
        self.run_review();self.assertTrue(self.module.recovery_panel.restore.isEnabled())
        worker,terminal=self.launch();self.assertTrue(self.finish(terminal)['committed']);self.assertEqual(self.service.read('count_files'),2)

    def test_late_documentation_blocks_action_and_preserves_recovery_bytes(self):
        source,_=self.ready();source.with_suffix('.txt').write_bytes(b'late caption')
        worker,terminal=self.launch();value=self.finish(terminal)
        self.assertFalse(value['committed']);self.assertFalse(value['moved']);self.assertIsNone(value['operation_journal'])
        self.assertIn('es ambigua o cambió',self.module.status.text());self.assertFalse(source.exists())

    def test_confirmation_and_result_display_have_no_gui_db_or_stat_and_tooltips_are_meaningful(self):
        self.ready();arguments=self.module.recovery_panel.selected_restore()
        fake=dict(committed=False,moved=False,source=arguments['source'],destination=arguments['destination'],operation_journal=None,error='duplicate_restore_commit_unconfirmed')
        with patch.object(self.service,'read',side_effect=AssertionError('GUI SQL')),patch.object(Path,'stat',side_effect=AssertionError('GUI stat')),patch.object(QMessageBox,'exec',return_value=QMessageBox.No):
            self.assertFalse(self.module._confirm_restore(arguments));self.module.recovery_panel.show_restore(fake,self.module.reason)
        self.assertIn('mismo ID',self.module.recovery_panel.restore.toolTip())
        for button in self.view.findChildren(QPushButton):self.assertTrue(button.toolTip())
        self.assertFalse(self.module.recovery_panel.restore.isEnabled())


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('RestoreGUI.'+name,sys.modules[__name__]) for name in RestoreGUI.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
