"""Purga Qt explícita: preview, modal capturado, cancelación y cierre sin bloqueo."""
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
from modules.duplicate_finder.logic.pinned_file import PinnedFile
from modules.duplicate_finder.logic.recovery_review import _physical
from modules.duplicate_finder.module import _LIVE_WORKERS
from curation_snapshot_selftest import pump
import duplicate_recovery_gui_selftest as prior


class PurgeGUI(prior.RecoveryGUI):
    def ready(self):
        self.source,self.keeper=self.prepare();self.run_review()
        self.destination=Path(self.module.recovery_panel.selected_restore()['destination'])
        self.assertTrue(self.module.recovery_panel.prepare_purge.isEnabled())
        self.assertFalse(self.module.recovery_panel.purge.isEnabled())
        self.module.prepare_selected_purge()
        self.assertTrue(pump(lambda:self.module.recovery_purge_worker is None))
        self.assertTrue(self.module.recovery_panel.purge.isEnabled(),self.module.status.text())
        self.assertIn('Vista previa lista',self.module.status.text())

    def launch(self):
        with patch.object(self.module,'_confirm_purge',return_value=True):self.module.purge_selected_copy()
        worker=self.module.recovery_purge_worker;self.assertIsNotNone(worker)
        terminal=[];worker.finished_signal.connect(terminal.append)
        return worker,terminal

    def finish(self,terminal):
        self.assertTrue(pump(lambda:self.module.recovery_purge_worker is None))
        self.assertEqual(len(terminal),1);self.assertFalse(self.module._acting)
        return terminal[0]

    def held_owner(self):
        entered,release=threading.Event(),threading.Event();self.releases.append(release)
        approve=LibraryRepository.approve_duplicate_purge
        def hold(repo,*args,**kwargs):
            value=approve(repo,*args,**kwargs);entered.set();self.assertTrue(release.wait(3));return value
        return entered,release,hold

    def test_preview_is_explicit_readonly_and_confirmed_purge_keeps_native_and_receipts(self):
        self.source,self.keeper=self.prepare();self.run_review()
        self.assertIsNone(self.module.recovery_purge_worker);self.assertIsNone(self.module.recovery_panel.purge_preview)
        self.assertFalse(self.module.recovery_panel.purge.isEnabled())
        with patch.object(self.service,'submit',side_effect=AssertionError('preview wrote')):
            self.module.prepare_selected_purge();self.assertTrue(pump(lambda:self.module.recovery_purge_worker is None))
        self.destination=Path(self.module.recovery_panel.selected_restore()['destination'])
        self.assertTrue(self.module.recovery_panel.purge.isEnabled())
        self.assertIn(self.keeper.as_posix(),self.module.recovery_panel.details.toPlainText())
        receipt=self.root/'catalog.db';receipt.write_bytes(b'Cherry receipts')
        original=self.journal.read_bytes();request=self.module.recovery_panel.selected_purge()
        before=self.service.read('get_copy_version_state',request['copy_id'])
        _,terminal=self.launch();value=self.finish(terminal)
        self.assertTrue(value['complete'],value);self.assertTrue(value['deleted'])
        self.assertFalse(self.destination.exists());self.assertEqual(self.keeper.read_bytes(),b'same bytes')
        self.assertEqual(self.journal.read_bytes(),original);self.assertEqual(receipt.read_bytes(),b'Cherry receipts')
        self.assertEqual(self.service.read('get_copy_version_state',request['copy_id']),before)
        self.assertEqual(self.events,[]);self.assertIn('eliminada definitivamente',self.module.status.text())
        self.assertIn('Eliminación definitiva confirmada: Sí',self.module.recovery_panel.details.toPlainText())
        self.assertIn(value['operation_journal'],self.module.recovery_panel.details.toPlainText())
        self.assertFalse(self.module.recovery_panel.purge.isEnabled());self.assertIsNone(self.module.recovery_panel.result)
        self.assertTrue(self.module.btn_run.isEnabled())

    def test_declined_modal_has_concrete_id_paths_size_irreversible_and_default_cancel(self):
        self.ready()
        def inspect(box):
            self.assertIn(self.destination.as_posix(),box.text());self.assertIn(self.keeper.as_posix(),box.text())
            self.assertIn('10 bytes',box.text());self.assertIn('Omite la Papelera',box.text())
            self.assertEqual(box.defaultButton(),box.button(QMessageBox.No));self.assertEqual(box.textFormat(),Qt.PlainText)
            self.assertEqual(box.button(QMessageBox.Yes).text(),'Eliminar definitivamente')
            self.assertEqual(box.button(QMessageBox.No).text(),'Cancelar')
            self.assertTrue(box.button(QMessageBox.Yes).toolTip());return QMessageBox.No
        with patch.object(QMessageBox,'exec',inspect):self.module.purge_selected_copy()
        self.assertIsNone(self.module.recovery_purge_worker);self.assertTrue(self.destination.exists())
        self.assertTrue(self.module.recovery_panel.purge.isEnabled());self.assertEqual(list(self.recovery.glob('*/purge-*.json')),[])

    def test_path_or_mode_changed_inside_modal_cannot_redirect_approval(self):
        self.ready()
        def changed(args,evidence):self.module.recovery_panel.journal.setText(str(self.journal)+'-different');return True
        with patch.object(self.module,'_confirm_purge',changed):self.module.purge_selected_copy()
        self.assertIsNone(self.module.recovery_purge_worker);self.assertTrue(self.destination.exists())
        self.assertIsNone(self.module.recovery_panel.purge_preview)
        self.module.recovery_panel.journal.setText(str(self.journal));self.run_review();self.module.prepare_selected_purge()
        self.assertTrue(pump(lambda:self.module.recovery_purge_worker is None))
        def mode(args,evidence):self.module.combo_mode.setCurrentIndex(0);return True
        with patch.object(self.module,'_confirm_purge',mode):self.module.purge_selected_copy()
        self.assertIsNone(self.module.recovery_purge_worker);self.assertTrue(self.destination.exists())

    def test_selection_change_consumes_preview_and_modal_cannot_redirect_target(self):
        raw,keeper=self.pair();extra=self.file('B/extra.png')
        self.recovery=(Path(self.temp.name)/'recovery').resolve();self.retire(raw,extra)
        self.module.combo_mode.setCurrentIndex(5);self.module.recovery_panel.journal.setText(str(self.journal));self.run_review()
        self.module.prepare_selected_purge();self.assertTrue(pump(lambda:self.module.recovery_purge_worker is None))
        self.assertTrue(self.module.recovery_panel.purge.isEnabled())
        def selected(args,evidence):
            self.module.recovery_panel.tree.setCurrentItem(self.module.recovery_panel.tree.topLevelItem(1));return True
        with patch.object(self.module,'_confirm_purge',selected):self.module.purge_selected_copy()
        self.assertIsNone(self.module.recovery_purge_worker);self.assertIsNone(self.module.recovery_panel.purge_preview)
        self.assertFalse(self.module.recovery_panel.purge.isEnabled())
        self.assertEqual(len(list(self.recovery.glob('*/00000*.png'))),2)

    def test_double_start_cancel_pending_owner_and_locked_files_keep_gui_responsive(self):
        self.ready();entered,release,hold=self.held_owner()
        with patch.object(LibraryRepository,'approve_duplicate_purge',hold):
            worker,terminal=self.launch();self.assertTrue(entered.wait(1))
            self.module.purge_selected_copy();self.module.prepare_selected_purge();self.module.restore_selected_copy()
            self.assertIs(self.module.recovery_purge_worker,worker);self.assertFalse(self.module.shutdown_ready())
            self.assertFalse(self.module.combo_mode.isEnabled());self.assertFalse(self.module.recovery_panel.tree.isEnabled())
            for path in (self.destination,self.keeper):
                with self.assertRaises(OSError):path.write_bytes(b'race')
                with self.assertRaises(OSError):path.unlink()
            ticks=[];QTimer.singleShot(0,lambda:ticks.append(True));started=time.monotonic();self.module.cancel()
            self.assertLess(time.monotonic()-started,.1);self.assertTrue(pump(lambda:ticks));release.set();result=self.finish(terminal)
        self.assertTrue(result['approved']);self.assertTrue(result['cancelled']);self.assertFalse(result['deleted'])
        self.assertTrue(self.destination.exists());self.assertIn('No se eliminó ningún archivo',self.module.status.text())
        self.assertFalse(self.module.recovery_panel.purge.isEnabled());self.assertEqual(self.events,[])

    def test_dashboard_waits_for_held_owner_and_cancel_preserves_recovery(self):
        self.ready();entered,release,hold=self.held_owner();host=self.dashboard()
        with patch.object(LibraryRepository,'approve_duplicate_purge',hold),patch('main.close_library_services') as close_owner:
            worker,terminal=self.launch();self.assertTrue(entered.wait(1));event=QCloseEvent();started=time.monotonic();host.closeEvent(event)
            self.assertLess(time.monotonic()-started,.1);self.assertFalse(event.isAccepted());self.assertFalse(close_owner.called)
            self.assertFalse(self.module.shutdown_ready());release.set();self.assertTrue(pump(lambda:host._shutdown_finished))
            close_owner.assert_called_once_with(permanent=True)
        self.assertTrue(self.destination.exists());self.assertEqual(self.events,[])
        logs=list(self.recovery.glob('*/purge-*.json'));self.assertEqual(len(logs),1)
        self.assertTrue(json.loads(logs[0].read_bytes())['approved']);self.assertFalse(json.loads(logs[0].read_bytes())['deleted'])

    def test_destroyed_view_preserves_worker_handles_and_discards_late_widgets(self):
        self.ready();entered,release,hold=self.held_owner()
        with patch.object(LibraryRepository,'approve_duplicate_purge',hold):
            worker,terminal=self.launch();self.assertTrue(entered.wait(1));delete(self.view)
            self.assertTrue(self.module._closed);self.assertIn(worker,_LIVE_WORKERS);release.set()
            self.assertTrue(pump(lambda:self.module.recovery_purge_worker is None))
        self.assertNotIn(worker,_LIVE_WORKERS);self.assertTrue(self.destination.exists());self.assertEqual(self.events,[])

    def test_close_after_delete_mark_completes_effect_and_journal_without_late_ui(self):
        self.ready();entered,release=threading.Event(),threading.Event();self.releases.append(release)
        mark=PinnedFile.mark_delete
        def held(victim):mark(victim);entered.set();self.assertTrue(release.wait(3))
        with patch.object(PinnedFile,'mark_delete',held):
            worker,terminal=self.launch();self.assertTrue(entered.wait(1));delete(self.view)
            self.assertFalse(self.module.shutdown_ready());release.set()
            self.assertTrue(pump(lambda:self.module.recovery_purge_worker is None))
        self.assertFalse(self.destination.exists());self.assertEqual(self.keeper.read_bytes(),b'same bytes')
        logs=list(self.recovery.glob('*/purge-*.json'));self.assertTrue(json.loads(logs[0].read_bytes())['deleted'])
        self.assertEqual(self.events,[])

    def test_stale_terminal_cannot_restore_consumed_preview_or_touch_new_path(self):
        self.ready();entered,release,hold=self.held_owner()
        with patch.object(LibraryRepository,'approve_duplicate_purge',hold):
            worker,terminal=self.launch();self.assertTrue(entered.wait(1))
            self.module.recovery_panel.journal.setText(str(self.journal)+'-different');release.set();value=self.finish(terminal)
        self.assertTrue(value['deleted']);self.assertIsNone(self.module.recovery_panel.purge_preview)
        self.assertIn('Elige un diario',self.module.status.text());self.assertEqual(self.events,[])

    def test_false_future_and_late_caption_display_failure_and_require_new_preview(self):
        self.ready();future=Future();future.set_result(False)
        with patch.object(self.service,'submit',return_value=future):_,terminal=self.launch();value=self.finish(terminal)
        self.assertFalse(value['deleted']);self.assertTrue(self.destination.exists())
        self.assertIn('No se confirmó la aprobación nativa',self.module.status.text())
        self.assertIn('Eliminación definitiva confirmada: No',self.module.recovery_panel.details.toPlainText())
        self.assertFalse(self.module.recovery_panel.purge.isEnabled())
        self.run_review();self.module.prepare_selected_purge();self.assertTrue(pump(lambda:self.module.recovery_purge_worker is None))
        self.keeper.with_suffix('.txt').write_bytes(b'late caption')
        _,terminal=self.launch();value=self.finish(terminal)
        self.assertFalse(value['deleted']);self.assertTrue(self.destination.exists());self.assertFalse(self.module.recovery_panel.purge.isEnabled())

    def test_preview_cancel_and_selection_change_while_running_drop_evidence(self):
        self.source,self.keeper=self.prepare();self.run_review();entered,release,slow=self.blocked()
        with patch('modules.duplicate_finder.logic.recovery_review._physical',side_effect=slow):
            self.module.prepare_selected_purge();self.assertTrue(entered.wait(1));self.module.cancel();release.set()
            self.assertTrue(pump(lambda:self.module.recovery_purge_worker is None))
        self.assertIsNone(self.module.recovery_panel.purge_preview);self.assertFalse(self.module.recovery_panel.purge.isEnabled())
        self.assertIn('cancelada',self.module.status.text());self.assertTrue(self.module.recovery_panel.prepare_purge.isEnabled())

    def test_closed_parent_inside_modal_or_selection_changed_during_preview_has_no_late_action(self):
        self.ready()
        def closed(args,evidence):delete(self.view);return True
        with patch.object(self.module,'_confirm_purge',closed):self.module.purge_selected_copy()
        self.assertTrue(self.module._closed);self.assertIsNone(self.module.recovery_purge_worker)
        self.assertTrue(self.destination.exists())

    def test_selection_changed_during_preview_discards_old_evidence_and_explains_retry(self):
        raw,keeper=self.pair();extra=self.file('B/extra.png')
        self.recovery=(Path(self.temp.name)/'recovery').resolve();self.retire(raw,extra)
        self.module.combo_mode.setCurrentIndex(5);self.module.recovery_panel.journal.setText(str(self.journal));self.run_review()
        entered,release,slow=self.blocked()
        with patch('modules.duplicate_finder.logic.recovery_review._physical',side_effect=slow):
            self.module.prepare_selected_purge();self.assertTrue(entered.wait(1))
            self.module.recovery_panel.tree.setCurrentItem(self.module.recovery_panel.tree.topLevelItem(1))
            release.set();self.assertTrue(pump(lambda:self.module.recovery_purge_worker is None))
        self.assertIsNone(self.module.recovery_panel.purge_preview)
        self.assertFalse(self.module.recovery_panel.purge.isEnabled());self.assertIn('Cambió la selección',self.module.status.text())

    def test_confirmation_preview_and_result_have_no_gui_db_or_stat_and_tooltips(self):
        self.ready();arguments=self.module.recovery_panel.selected_purge();preview=self.module.recovery_panel.purge_preview
        fake=dict(deleted=False,delete_marked=False,approved=False,destination=arguments['destination'],keeper=preview['evidence']['keeper'],operation_journal=None,error='duplicate_purge_approval_unconfirmed')
        with patch.object(self.service,'read',side_effect=AssertionError('GUI SQL')),patch.object(Path,'stat',side_effect=AssertionError('GUI stat')),patch.object(QMessageBox,'exec',return_value=QMessageBox.No):
            self.assertFalse(self.module._confirm_purge(arguments,preview['evidence']))
            self.module.recovery_panel.show_purge_preview(preview,self.module.reason)
            self.module.recovery_panel.show_purge(fake,self.module.reason)
        self.assertIn('Papelera',self.module.recovery_panel.purge.toolTip())
        for button in self.view.findChildren(QPushButton):self.assertTrue(button.toolTip())


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('PurgeGUI.'+name,sys.modules[__name__]) for name in PurgeGUI.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
