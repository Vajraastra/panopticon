"""Explicit private Qt purge previews/confirmation; real Windows temporary backups."""
from concurrent.futures import Future
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox
from shiboken6 import isValid,delete
from core.components import producer_recovery as ui
from core import metadata_backup_purge as backend
from core.native_threads import native_threads_ready
from scripts import producer_recovery_gui_selftest as fixtures


class BackupUI(unittest.TestCase):
    setUpClass=classmethod(fixtures.RecoveryUI.setUpClass.__func__)
    setUp=fixtures.RecoveryUI.setUp
    cleanup=fixtures.RecoveryUI.cleanup
    tr=fixtures.RecoveryUI.tr
    dialog=fixtures.RecoveryUI.dialog
    settle=fixtures.RecoveryUI.settle
    reviewed=fixtures.RecoveryUI.reviewed
    gate=fixtures.RecoveryUI.gate
    window=fixtures.RecoveryUI.window
    metadata=fixtures.RecoveryUI.metadata
    derived=fixtures.RecoveryUI.derived
    inspect=fixtures.RecoveryUI.inspect
    pending_metadata=fixtures.RecoveryUI.pending_metadata

    def previewed(self,journal):
        dialog=self.reviewed(journal)
        self.assertTrue(dialog.backup_preview_button.isEnabled())
        dialog.preview_backup()
        self.settle(dialog)
        self.assertIsNotNone(dialog._purge_preview,dialog.status_label.text())
        self.assertTrue(dialog.backup_purge_button.isEnabled())
        return dialog

    def confirm(self,dialog,answer=QMessageBox.Yes):
        with patch.object(QMessageBox,'question',return_value=answer) as question:
            dialog.purge_backup()
        self.settle(dialog)
        return question

    def test_preview_never_deletes_and_explicit_confirmation_names_exact_backup(self):
        journal=self.metadata()
        backup=Path(json.loads(journal.read_bytes())['backup'])
        original=self.source.read_bytes(),journal.read_bytes()
        dialog=self.previewed(journal)
        self.assertTrue(backup.exists())
        self.assertFalse(list(journal.parent.glob('backup-purge-*.json')))
        evidence=dialog._purge_preview['evidence']
        question=self.confirm(dialog)
        self.assertEqual(question.call_count,1)
        text=question.call_args.args[2]
        for value in (evidence['backup'],evidence['current'],evidence['operation_id'],evidence['backup_sha256'],evidence['current_sha256']):
            self.assertIn(value,text)
        self.assertEqual(question.call_args.args[4],QMessageBox.No)
        self.assertFalse(backup.exists())
        self.assertEqual((self.source.read_bytes(),journal.read_bytes()),original)
        self.assertIn('eliminó definitivamente',dialog.status_label.text())
        self.assertFalse(dialog.backup_purge_button.isEnabled())
        self.assertFalse(dialog.backup_preview_button.isEnabled())
        self.assertEqual(self.inspect(journal)['output']['rating'],5)

    def test_declining_confirmation_keeps_backup_and_current_preview(self):
        journal=self.metadata()
        dialog=self.previewed(journal)
        before=dialog._purge_preview
        self.confirm(dialog,QMessageBox.No)
        self.assertIs(dialog._purge_preview,before)
        self.assertTrue(Path(before['evidence']['backup']).exists())
        self.assertTrue(dialog.backup_purge_button.isEnabled())
        self.assertFalse(list(journal.parent.glob('backup-purge-*.json')))

    def test_purge_button_cannot_start_without_verified_preview(self):
        journal=self.metadata()
        dialog=self.reviewed(journal)
        self.assertFalse(dialog.backup_purge_button.isEnabled())
        with patch.object(QMessageBox,'question',side_effect=AssertionError('confirmation without evidence')):
            dialog.purge_backup()
        self.assertIsNone(dialog._worker)

    def test_pending_metadata_and_other_producers_cannot_offer_backup_purge(self):
        journal=self.pending_metadata()
        dialog=self.reviewed(journal)
        self.assertFalse(dialog.backup_preview_button.isEnabled())
        self.assertFalse(dialog.backup_purge_button.isEnabled())

    def test_derived_copy_cannot_offer_original_backup_purge(self):
        journal=self.derived(fail_commit=True)
        dialog=self.reviewed(journal)
        self.assertFalse(dialog.backup_preview_button.isEnabled())

    def test_metadata_export_backup_is_not_an_original_edit_purge(self):
        journal=self.metadata(export=True)
        dialog=self.reviewed(journal)
        self.assertFalse(dialog.backup_preview_button.isEnabled())
        dialog.preview_backup()
        self.assertIsNone(dialog._worker)

    def test_selecting_another_journal_invalidates_purge_preview(self):
        journal=self.metadata()
        dialog=self.previewed(journal)
        self.assertTrue(dialog.select_journal(self.root/'different.json'))
        self.assertIsNone(dialog._purge_preview)
        self.assertIsNone(dialog._report)
        self.assertFalse(dialog.backup_purge_button.isEnabled())

    def test_new_review_consumes_old_purge_preview(self):
        journal=self.metadata()
        dialog=self.previewed(journal)
        dialog.review_current()
        self.settle(dialog)
        self.assertIsNone(dialog._purge_preview)
        self.assertFalse(dialog.backup_purge_button.isEnabled())
        self.assertTrue(dialog.backup_preview_button.isEnabled())

    def test_changed_curation_during_confirmation_rejects_deletion(self):
        journal=self.metadata()
        dialog=self.previewed(journal)
        backup=Path(dialog._purge_preview['evidence']['backup'])
        def changed(*args):
            self.service.submit('edit_curation',self.snapshot['path'],rating=3).result()
            return QMessageBox.Yes
        with patch.object(QMessageBox,'question',side_effect=changed):
            dialog.purge_backup()
        self.settle(dialog)
        self.assertTrue(backup.exists())
        self.assertIn('review_changed',dialog.status_label.text())
        self.assertFalse(dialog.backup_purge_button.isEnabled())

    def test_double_confirmation_reentry_and_selection_are_blocked(self):
        journal=self.metadata()
        dialog=self.previewed(journal)
        path=dialog._path
        def nested(*args):
            dialog.purge_backup()
            self.assertFalse(dialog.select_journal(self.root/'another.json'))
            dialog.preview_backup()
            dialog.review_current()
            dialog.apply_review()
            self.assertIsNone(dialog._worker)
            return QMessageBox.Yes
        with patch.object(QMessageBox,'question',side_effect=nested) as question:
            dialog.purge_backup()
        self.settle(dialog)
        self.assertEqual(question.call_count,1)
        self.assertEqual(dialog._path,path)
        self.assertIn('eliminó definitivamente',dialog.status_label.text())

    def test_close_during_confirmation_cannot_admit_late_deletion(self):
        journal=self.metadata()
        dialog=self.previewed(journal)
        backup=Path(dialog._purge_preview['evidence']['backup'])
        def close(*args):
            dialog.close()
            return QMessageBox.Yes
        with patch.object(QMessageBox,'question',side_effect=close):
            dialog.purge_backup()
        self.assertTrue(fixtures.pump(lambda:not isValid(dialog)))
        self.assertTrue(backup.exists())
        self.assertFalse(list(journal.parent.glob('backup-purge-*.json')))

    def test_forced_widget_destruction_during_confirmation_cannot_admit_deletion(self):
        journal=self.metadata()
        dialog=self.previewed(journal)
        backup=Path(dialog._purge_preview['evidence']['backup'])
        def destroy(*args):
            delete(dialog)
            return QMessageBox.Yes
        with patch.object(QMessageBox,'question',side_effect=destroy):
            dialog.purge_backup()
        self.assertFalse(isValid(dialog))
        self.assertTrue(backup.exists())

    def test_preview_worker_keeps_qt_responsive_and_close_preserves_backup(self):
        journal=self.metadata()
        dialog=self.reviewed(journal)
        entered,release,calls=self.gate('backup_preview')
        dialog.preview_backup()
        self.assertTrue(entered.wait(1))
        self.assertFalse(dialog.backup_purge_button.isEnabled())
        ticks=[]
        timer=QTimer();timer.timeout.connect(lambda:ticks.append(1));timer.start(5)
        self.assertTrue(fixtures.pump(lambda:len(ticks)>=3))
        dialog.close()
        self.assertFalse(dialog.shutdown_ready())
        release.set()
        self.assertTrue(fixtures.pump(lambda:not isValid(dialog)))
        timer.stop()
        self.assertTrue(Path(json.loads(journal.read_bytes())['backup']).exists())

    def test_close_before_purge_worker_admission_preserves_backup(self):
        journal=self.metadata()
        dialog=self.previewed(journal)
        backup=Path(dialog._purge_preview['evidence']['backup'])
        entered,release,calls=self.gate('backup_purge')
        with patch.object(QMessageBox,'question',return_value=QMessageBox.Yes):
            dialog.purge_backup()
        self.assertTrue(entered.wait(1))
        dialog.reject()
        release.set()
        self.assertTrue(fixtures.pump(lambda:not isValid(dialog)))
        self.assertTrue(backup.exists())

    def test_dashboard_close_drains_admitted_purge_approval_then_preserves_backup(self):
        journal=self.metadata()
        window=self.window()
        window.open_producer_recovery()
        dialog=window._producer_recovery_dialog;self.dialogs.append(dialog)
        dialog.select_journal(journal);dialog.review_current();self.settle(dialog)
        dialog.preview_backup();self.settle(dialog)
        backup=Path(dialog._purge_preview['evidence']['backup'])
        entered,release=threading.Event(),threading.Event();self.releases.append(release)
        submit=self.service.submit
        threads=[]
        def slow(operation,*args,**kwargs):
            future=Future();future.set_running_or_notify_cancel()
            def finish():
                entered.set();release.wait(4)
                try:future.set_result(submit(operation,*args,**kwargs).result())
                except Exception as error:future.set_exception(error)
            thread=threading.Thread(target=finish);threads.append(thread);thread.start()
            return future
        self.stack.enter_context(patch.object(self.service,'submit',slow))
        with patch.object(QMessageBox,'question',return_value=QMessageBox.Yes):
            dialog.purge_backup()
        self.assertTrue(entered.wait(1))
        import main as product
        try:
            with patch.object(product,'request_close_library_services') as close_writer:
                window.close()
                self.assertFalse(close_writer.called)
                self.assertFalse(window._shutdown_finished)
                release.set()
                self.assertTrue(fixtures.pump(lambda:window._shutdown_finished))
                self.assertTrue(close_writer.called)
        finally:
            release.set()
            for thread in threads:thread.join(4)
        self.assertTrue(backup.exists())

    def test_failed_final_evidence_reports_confirmed_deletion_in_gui(self):
        journal=self.metadata()
        dialog=self.previewed(journal)
        backup=Path(dialog._purge_preview['evidence']['backup'])
        save=backend._save
        def fail(path,data,**kwargs):
            if data['phase'] in ('complete','review_required'):
                raise OSError('private final evidence failure')
            return save(path,data,**kwargs)
        with patch.object(backend,'_save',fail):
            self.confirm(dialog)
        self.assertFalse(backup.exists())
        self.assertIn('Eliminación del respaldo confirmada',dialog.status_label.text())
        self.assertIn('requiere atención',dialog.status_label.text())
        self.assertFalse(dialog.backup_purge_button.isEnabled())

    def test_already_deleted_backup_cannot_enable_second_purge(self):
        journal=self.metadata()
        dialog=self.previewed(journal)
        self.confirm(dialog)
        dialog.review_current();self.settle(dialog)
        self.assertIsNotNone(dialog._report)
        self.assertFalse(dialog.backup_preview_button.isEnabled())
        self.assertFalse(dialog.backup_purge_button.isEnabled())

    def test_close_after_delete_mark_finishes_evidence_before_destroying_dialog(self):
        journal=self.metadata()
        dialog=self.previewed(journal)
        backup=Path(dialog._purge_preview['evidence']['backup'])
        entered,release=threading.Event(),threading.Event();self.releases.append(release)
        save=backend._save
        def delayed(path,data,**kwargs):
            if data['phase']=='complete':
                entered.set()
                if not release.wait(4):raise TimeoutError('private final evidence gate')
            return save(path,data,**kwargs)
        with patch.object(backend,'_save',delayed):
            with patch.object(QMessageBox,'question',return_value=QMessageBox.Yes):
                dialog.purge_backup()
            self.assertTrue(entered.wait(1))
            self.assertFalse(backup.exists())
            dialog.close()
            self.assertTrue(isValid(dialog))
            self.assertFalse(dialog.shutdown_ready())
            release.set()
            self.assertTrue(fixtures.pump(lambda:not isValid(dialog)))
        records=list(journal.parent.glob('backup-purge-*-complete.json'))
        self.assertEqual(len(records),1)
        self.assertTrue(json.loads(records[0].read_bytes())['deleted'])

    def test_preview_start_failure_keeps_backup_and_does_not_enable_purge(self):
        journal=self.metadata()
        dialog=self.reviewed(journal)
        with patch.object(ui,'start_worker',side_effect=RuntimeError('private preview start failed')):
            dialog.preview_backup()
        self.assertIsNone(dialog._worker)
        self.assertIsNone(dialog._purge_preview)
        self.assertFalse(dialog.backup_purge_button.isEnabled())
        self.assertTrue(Path(json.loads(journal.read_bytes())['backup']).exists())

    def test_failed_preview_requires_fresh_review_before_any_action(self):
        journal=self.metadata()
        dialog=self.reviewed(journal)
        backup=Path(json.loads(journal.read_bytes())['backup'])
        backup.write_bytes(b'changed after review')
        dialog.preview_backup()
        self.settle(dialog)
        self.assertIsNone(dialog._report)
        self.assertIsNone(dialog._purge_preview)
        self.assertFalse(dialog.action_button.isEnabled())
        self.assertFalse(dialog.backup_preview_button.isEnabled())
        self.assertTrue(dialog.review_button.isEnabled())

    def test_confirmed_purge_does_not_overlap_second_job(self):
        journal=self.metadata()
        dialog=self.previewed(journal)
        entered,release,calls=self.gate('backup_purge')
        with patch.object(QMessageBox,'question',return_value=QMessageBox.Yes) as question:
            dialog.purge_backup()
            self.assertTrue(entered.wait(1))
            dialog.purge_backup()
            dialog.review_current()
            dialog.preview_backup()
            dialog.apply_review()
            self.assertEqual(question.call_count,1)
            self.assertEqual(len(calls),1)
            release.set()
            self.settle(dialog)
        self.assertIn('eliminó definitivamente',dialog.status_label.text())

    def test_older_operation_preview_shows_current_copy_and_keeps_later_backup(self):
        journal=self.metadata()
        from modules.metadata.logic.file_tasks import MetadataTasks
        tasks=MetadataTasks(self.service,self.root/'metadata')
        later=tasks.edit(tasks.load(str(self.source),self.events),'later prompt','later bad')
        self.assertEqual(later['status'],'complete',later)
        later_backup=Path(json.loads(Path(later['journal']).read_bytes())['backup'])
        dialog=self.previewed(journal)
        self.assertIn(str(self.snapshot['id']),dialog.details.toPlainText())
        self.assertIn(dialog._purge_preview['evidence']['current_sha256'],dialog.details.toPlainText())
        self.confirm(dialog)
        self.assertTrue(later_backup.exists())
        self.assertEqual(len(self.service.read('get_generation_history_page',self.snapshot['id'])['records']),2)

    def test_backups_controls_and_confirmation_have_english_spanish_text_and_tooltips(self):
        journal=self.metadata()
        for language in ('es','en'):
            self.language=language
            dialog=self.previewed(journal)
            for button in (dialog.backup_preview_button,dialog.backup_purge_button):
                self.assertTrue(button.toolTip())
                self.assertNotIn('tool.producer_recovery.',button.text())
            question=self.confirm(dialog,QMessageBox.No)
            self.assertIn('¿Eliminar este respaldo?' if language=='es' else 'Delete this backup?',question.call_args.args[2])
            self.assertIn('no se pueden reconstruir' if language=='es' else 'cannot be reconstructed',dialog.details.toPlainText())


if __name__=='__main__':
    unittest.main(verbosity=2)
