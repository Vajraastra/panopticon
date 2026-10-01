"""Private real Qt journal review, explicit reconciliation and native-close tests."""
from concurrent.futures import Future
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QObject, QThread, QTimer, QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton
from shiboken6 import delete, isValid
from core.components import producer_recovery as ui
from core.native_threads import native_threads_ready
from core.paths import ProjectPaths, CachePaths
from core.theme_manager import ThemeManager
from scripts import producer_journal_review_selftest as fixtures
from scripts import producer_reconciliation_selftest as metadata_fixtures


def pump(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        if predicate():
            return True
        time.sleep(.003)
    return False


class RecoveryUI(unittest.TestCase):
    derived = fixtures.Review.derived
    metadata = fixtures.Review.metadata
    inspect = fixtures.Review.inspect
    pending_metadata = metadata_fixtures.Reconciliation.pending_metadata

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')

    def setUp(self):
        fixtures.Review.setUp(self)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(ProjectPaths, 'root', return_value=self.root))
        self.stack.enter_context(patch.object(CachePaths, 'get_cache_root', return_value=self.root))
        self.stack.enter_context(patch.object(CachePaths, 'get_models_root', return_value=self.root/'models'))
        self.stack.enter_context(patch.object(CachePaths, 'open_folder', side_effect=AssertionError('external explorer')))
        self.dialogs, self.windows, self.releases = [], [], []
        self.language = 'es'
        self.translations = {language: json.loads((Path(__file__).resolve().parents[1]/'locales'/(language+'.json')).read_text(encoding='utf-8'))
                             for language in ('en', 'es')}
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for release in self.releases:
            release.set()
        for dialog in self.dialogs:
            if isValid(dialog):
                dialog.close()
        self.assertTrue(pump(native_threads_ready))
        for dialog in self.dialogs:
            if isValid(dialog):
                dialog.close()
        for window in self.windows:
            if isValid(window):
                window.close()
                self.assertTrue(pump(lambda: window._shutdown_finished))
                window.deleteLater()
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def tr(self, key, default=None):
        return self.translations[self.language].get(key, default or key)

    def dialog(self, journal=None):
        dialog = ui.ProducerRecoveryDialog(self.service, self.tr)
        self.dialogs.append(dialog)
        dialog.setStyleSheet(ThemeManager().get_stylesheet())
        dialog.show()
        if journal:
            self.assertTrue(dialog.select_journal(journal))
        return dialog

    def settle(self, dialog):
        self.assertTrue(pump(lambda: dialog._worker is None and dialog.shutdown_ready()))

    def reviewed(self, journal):
        dialog = self.dialog(journal)
        dialog.review_current()
        self.settle(dialog)
        self.assertIsNotNone(dialog._report, dialog.status_label.text())
        return dialog

    def gate(self, target='review', finish_first=False):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = getattr(ui, target)
        calls = []
        def block(*args, **kwargs):
            self.assertIsNot(QThread.currentThread(), self.app.thread())
            calls.append((args, kwargs))
            result = original(*args, **kwargs) if finish_first else None
            entered.set()
            if not release.wait(4):
                raise TimeoutError('private worker gate')
            return result if finish_first else original(*args, **kwargs)
        self.stack.enter_context(patch.object(ui, target, block))
        return entered, release, calls

    def test_pending_cropper_review_and_apply_use_open_native_owner_once(self):
        journal = self.derived(fail_commit=True)
        before = journal.read_bytes(), self.source.read_bytes(), self.output.read_bytes()
        dialog = self.reviewed(journal)
        self.assertEqual(ui.available_action(dialog._report), 'register_derived_copy')
        self.assertIn('rating: 5', dialog.details.toPlainText())
        self.assertTrue(dialog.action_button.isEnabled())
        submit = self.service.submit
        with patch.object(self.service, 'submit', wraps=submit) as writes:
            dialog.apply_review()
            dialog.apply_review()
            self.settle(dialog)
        self.assertEqual(sum(call.args[0]=='register_derived_copy' for call in writes.call_args_list), 1)
        report = self.inspect(journal)
        self.assertEqual(report['status'], 'confirmed_current')
        self.assertNotEqual(report['source']['copy_id'], report['output']['copy_id'])
        self.assertEqual(report['output']['rating'], 0)
        self.assertEqual((journal.read_bytes(), self.source.read_bytes(), self.output.read_bytes()), before)
        self.assertFalse(dialog.action_button.isEnabled())
        self.assertIn('Acción registrada', dialog.details.toPlainText())

    def test_pending_metadata_commit_keeps_original_identity_curation_and_backup(self):
        journal = self.pending_metadata()
        dialog = self.reviewed(journal)
        self.assertEqual(ui.available_action(dialog._report), 'commit_metadata_edit')
        dialog.apply_review()
        self.settle(dialog)
        report = self.inspect(journal)
        self.assertEqual(report['source']['copy_id'], report['output']['copy_id'])
        self.assertEqual(report['output']['rating'], 5)
        self.assertTrue(report['backup_matches_source'])
        self.assertEqual(len(self.service.read('get_generation_history_page', self.snapshot['id'])['records']), 1)

    def test_historical_changed_receipt_acknowledges_without_native_writes(self):
        journal = self.derived()
        self.output.unlink()
        dialog = self.reviewed(journal)
        self.assertEqual(dialog._report['status'], 'confirmed_history_current_changed')
        self.assertEqual(ui.available_action(dialog._report), 'acknowledge_historical_receipt')
        with patch.object(self.service, 'submit', side_effect=AssertionError('historical replay')):
            dialog.apply_review()
            self.settle(dialog)
        self.assertFalse(self.output.exists())
        self.assertIn('la copia actual cambió', dialog.details.toPlainText())
        self.assertEqual(len(list(journal.parent.glob('reconciliation-*.json'))), 1)

    def test_export_records_review_only(self):
        journal = self.metadata(export=True)
        dialog = self.reviewed(journal)
        self.assertEqual(ui.available_action(dialog._report), 'record_export_review')
        with patch.object(self.service, 'submit', side_effect=AssertionError('export registration')):
            dialog.apply_review()
            self.settle(dialog)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertIn('Bytes de exportación verificados', dialog.details.toPlainText())

    def test_review_has_no_native_write_or_reconciliation_record(self):
        journal = self.derived(fail_commit=True)
        with patch.object(self.service, 'submit', side_effect=AssertionError('read-only review wrote')):
            dialog = self.reviewed(journal)
        self.assertEqual(dialog._report['status'], 'published_bytes_without_receipt')
        self.assertFalse(list(journal.parent.glob('reconciliation-*.json')))

    def test_lost_native_reply_is_confirmed_without_second_registration(self):
        journal = self.derived(fail_commit=True)
        dialog = self.reviewed(journal)
        submit = self.service.submit
        writes = []
        def lose_reply(operation, *args, **kwargs):
            if operation != 'register_derived_copy':
                return submit(operation, *args, **kwargs)
            writes.append(operation)
            submit(operation, *args, **kwargs).result()
            future = Future()
            future.set_exception(RuntimeError('private lost reply'))
            return future
        with patch.object(self.service, 'submit', lose_reply):
            dialog.apply_review()
            self.settle(dialog)
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.inspect(journal)['status'], 'confirmed_current')
        self.assertIn('Acción registrada', dialog.status_label.text())

    def test_native_write_failure_keeps_intent_and_requires_new_explicit_review(self):
        journal = self.derived(fail_commit=True)
        dialog = self.reviewed(journal)
        submit = self.service.submit
        def fail(operation, *args, **kwargs):
            if operation != 'register_derived_copy':
                return submit(operation, *args, **kwargs)
            future = Future()
            future.set_exception(RuntimeError('private unavailable writer'))
            return future
        with patch.object(self.service, 'submit', fail):
            dialog.apply_review()
            self.settle(dialog)
        self.assertIn('private unavailable writer', dialog.status_label.text())
        self.assertIsNone(self.inspect(journal)['receipt']['receipt'])
        self.assertFalse(dialog.action_button.isEnabled())
        record = json.loads(next(journal.parent.glob('reconciliation-*.json')).read_bytes())
        self.assertEqual(record['phase'], 'review_required')
        self.assertTrue(self.output.exists())
        self.assertTrue(dialog.review_button.isEnabled())

    def test_existing_reconciliation_guard_is_preserved_for_manual_review(self):
        journal = self.derived()
        guard = journal.parent/'producer-reconcile.lock'
        guard.write_bytes(b'old incomplete guard')
        dialog = self.reviewed(journal)
        dialog.apply_review()
        self.settle(dialog)
        self.assertIn('lock_requires_review', dialog.status_label.text())
        self.assertEqual(guard.read_bytes(), b'old incomplete guard')

    def test_unstable_review_has_no_available_action(self):
        journal = self.derived()
        report = self.inspect(journal)
        report.update(stable=False, status='changed_during_review')
        with patch.object(ui, 'review', return_value=report):
            dialog = self.reviewed(journal)
        self.assertFalse(dialog.action_button.isEnabled())
        self.assertIn('cambió durante', dialog.status_label.text())

    def test_source_curation_changed_after_review_blocks_action_and_permits_fresh_review(self):
        journal = self.derived(fail_commit=True)
        dialog = self.reviewed(journal)
        # Native identity retains its recorded Windows alias, not resolve()'s spelling.
        self.service.submit('edit_curation', self.snapshot['path'], rating=4).result()
        dialog.apply_review()
        self.settle(dialog)
        self.assertIn('changed_since_review', dialog.status_label.text())
        self.assertIsNone(self.inspect(journal)['receipt']['receipt'])
        dialog.review_current()
        self.settle(dialog)
        self.assertFalse(dialog.action_button.isEnabled())
        self.assertFalse(dialog._report['source_revision_matches'])

    def test_changed_output_bytes_disable_action(self):
        journal = self.derived(fail_commit=True)
        self.output.write_bytes(b'changed externally')
        dialog = self.reviewed(journal)
        self.assertFalse(dialog.action_button.isEnabled())
        self.assertIsNone(ui.available_action(dialog._report))

    def test_selection_invalidates_review_and_cannot_change_during_worker(self):
        journal = self.derived()
        dialog = self.reviewed(journal)
        dialog.select_journal(self.root/'not-a-journal.json')
        self.assertIsNone(dialog._report)
        self.assertFalse(dialog.action_button.isEnabled())
        self.assertEqual(dialog.details.toPlainText(), '')
        dialog.select_journal(journal)
        entered, release, calls = self.gate()
        dialog.review_current()
        self.assertTrue(entered.wait(1))
        self.assertFalse(dialog.select_journal(self.root/'another.json'))
        dialog.review_current()
        dialog.apply_review()
        self.assertEqual(len(calls), 1)
        release.set()
        self.settle(dialog)
        self.assertEqual(dialog._path, str(journal))

    def test_qt_remains_responsive_while_hashing_and_close_cancels_before_admission(self):
        journal = self.derived(fail_commit=True)
        dialog = self.dialog(journal)
        entered, release, calls = self.gate()
        dialog.review_current()
        self.assertTrue(entered.wait(1))
        beats = []
        timer = QTimer()
        timer.timeout.connect(lambda: beats.append(1))
        timer.start(5)
        self.assertTrue(pump(lambda: len(beats) >= 3))
        started = time.monotonic()
        dialog.close()
        self.assertLess(time.monotonic()-started, .1)
        self.assertTrue(isValid(dialog))
        self.assertFalse(dialog.shutdown_ready())
        self.assertTrue(dialog._worker.cancel_event.is_set())
        release.set()
        self.assertTrue(pump(lambda: not isValid(dialog)))
        timer.stop()
        self.assertIsNone(self.inspect(journal)['receipt']['receipt'])

    def test_reject_accept_and_escape_done_wait_for_native_worker(self):
        journal = self.derived()
        for result in (ui.QDialog.Rejected, ui.QDialog.Accepted):
            with self.subTest(result=result):
                dialog = self.dialog(journal)
                entered, release, calls = self.gate()
                dialog.review_current()
                self.assertTrue(entered.wait(1))
                dialog.done(result)
                dialog.done(ui.QDialog.Accepted)
                self.assertEqual(dialog._close_result, result)
                self.assertTrue(isValid(dialog))
                release.set()
                self.assertTrue(pump(lambda: not isValid(dialog)))

    def test_chooser_reentry_and_close_ignore_late_selection(self):
        dialog = self.dialog()
        def nested(*args):
            dialog.choose()
            dialog.close()
            return str(self.root/'late.json'), ''
        with patch.object(QFileDialog, 'getOpenFileName', side_effect=nested) as chooser:
            dialog.choose()
        self.assertEqual(chooser.call_count, 1)
        self.assertIsNone(dialog._path)
        self.assertTrue(pump(lambda: not isValid(dialog)))

    def test_chooser_cancel_preserves_existing_evidence(self):
        journal = self.derived()
        dialog = self.reviewed(journal)
        token = dialog._report['review_fingerprint']
        with patch.object(QFileDialog, 'getOpenFileName', return_value=('', '')):
            dialog.choose()
        self.assertEqual(dialog._report['review_fingerprint'], token)
        self.assertTrue(dialog.action_button.isEnabled())

    def test_bad_journal_then_valid_selection_recovers_without_new_service(self):
        dialog = self.dialog(self.root/'missing.json')
        dialog.review_current()
        self.settle(dialog)
        self.assertIn('requiere atención', dialog.status_label.text())
        self.assertFalse(dialog.action_button.isEnabled())
        journal = self.derived()
        dialog.select_journal(journal)
        dialog.review_current()
        self.settle(dialog)
        self.assertTrue(dialog.action_button.isEnabled())
        self.assertIs(dialog.service, self.service)

    def test_worker_start_failure_is_terminal_and_retry_is_possible(self):
        journal = self.derived()
        dialog = self.dialog(journal)
        with patch.object(ui, 'start_worker', side_effect=RuntimeError('private start failure')):
            dialog.review_current()
        self.assertIsNone(dialog._worker)
        self.assertIn('private start failure', dialog.status_label.text())
        dialog.review_current()
        self.settle(dialog)
        self.assertTrue(dialog.action_button.isEnabled())

    def test_close_before_reconciliation_admission_preserves_pending_operation(self):
        journal = self.derived(fail_commit=True)
        dialog = self.reviewed(journal)
        entered, release, calls = self.gate('reconcile')
        dialog.apply_review()
        self.assertTrue(entered.wait(1))
        dialog.reject()
        release.set()
        self.assertTrue(pump(lambda: not isValid(dialog)))
        self.assertIsNone(self.inspect(journal)['receipt']['receipt'])
        self.assertFalse(list(journal.parent.glob('reconciliation-*.json')))

    def test_close_after_native_admission_drains_and_keeps_receipt(self):
        journal = self.derived(fail_commit=True)
        dialog = self.reviewed(journal)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        submit = self.service.submit
        def slow(operation, *args, **kwargs):
            if operation != 'register_derived_copy':
                return submit(operation, *args, **kwargs)
            future = Future()
            self.assertTrue(future.set_running_or_notify_cancel())
            def finish():
                entered.set()
                if not release.wait(4):
                    future.set_exception(TimeoutError('private admitted gate'))
                    return
                try:
                    future.set_result(submit(operation, *args, **kwargs).result())
                except Exception as error:
                    future.set_exception(error)
            thread = threading.Thread(target=finish)
            thread.start()
            self.addCleanup(thread.join, 4)
            return future
        self.stack.enter_context(patch.object(self.service, 'submit', slow))
        dialog.apply_review()
        self.assertTrue(entered.wait(1))
        dialog.close()
        self.assertTrue(isValid(dialog))
        self.assertFalse(dialog.shutdown_ready())
        release.set()
        self.assertTrue(pump(lambda: not isValid(dialog)))
        self.assertEqual(self.inspect(journal)['status'], 'confirmed_current')
        record = json.loads(next(journal.parent.glob('reconciliation-*.json')).read_bytes())
        self.assertEqual(record['phase'], 'complete')

    def test_late_result_after_close_does_not_touch_widgets_or_reenable_actions(self):
        journal = self.derived()
        dialog = self.dialog(journal)
        entered, release, calls = self.gate(finish_first=True)
        dialog.review_current()
        self.assertTrue(entered.wait(1))
        before = dialog.details.toPlainText()
        dialog.request_shutdown()
        release.set()
        self.settle(dialog)
        self.assertEqual(dialog.details.toPlainText(), before)
        self.assertIsNone(dialog._report)
        self.assertFalse(dialog.isEnabled())

    def test_forced_widget_destruction_retains_worker_and_cancels_late_delivery(self):
        journal = self.derived()
        dialog = self.dialog(journal)
        entered, release, calls = self.gate()
        dialog.review_current()
        self.assertTrue(entered.wait(1))
        worker = dialog._worker
        delete(dialog)
        self.assertFalse(isValid(dialog))
        self.assertTrue(worker.cancel_event.is_set())
        self.assertFalse(native_threads_ready())
        release.set()
        self.assertTrue(pump(native_threads_ready))
        self.assertTrue(pump(lambda: not isValid(worker)))

    def test_controls_wait_for_real_qt_cleanup_after_terminal_and_native_finished(self):
        journal = self.derived()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = ui.ProducerRecoveryWorker.run
        def cleanup(*args):
            entered.set()
            release.wait(4)
        def run(worker):
            resource = QObject()
            resource.destroyed.connect(cleanup, Qt.DirectConnection)
            resource.deleteLater()
            original(worker)
        self.stack.enter_context(patch.object(ui.ProducerRecoveryWorker, 'run', run))
        dialog = self.dialog(journal)
        dialog.review_current()
        self.assertTrue(entered.wait(1))
        self.assertTrue(pump(lambda: dialog._terminal is not None))
        self.assertTrue(dialog._worker.isFinished())
        self.assertFalse(dialog.shutdown_ready())
        self.assertFalse(dialog.action_button.isEnabled())
        self.assertFalse(dialog.review_button.isEnabled())
        release.set()
        self.settle(dialog)
        self.assertTrue(dialog.action_button.isEnabled())

    def test_stale_terminal_sender_cannot_replace_current_evidence(self):
        journal = self.derived()
        dialog = self.reviewed(journal)
        token = dialog._report['review_fingerprint']
        dialog._completed(dict(value=None, error='stale'))
        self.assertEqual(dialog._report['review_fingerprint'], token)
        self.assertNotIn('stale', dialog.status_label.text())

    def test_english_spanish_tooltips_and_light_dark_semantic_styles(self):
        journal = self.derived()
        for language in ('es', 'en'):
            self.language = language
            dialog = self.reviewed(journal)
            for button in dialog.findChildren(QPushButton):
                self.assertTrue(button.toolTip())
            self.assertNotIn('tool.producer_recovery.', dialog.details.toPlainText())
            for theme in ('light', 'midnight'):
                manager = ThemeManager(config_path=str(self.root/'private-theme.json'))
                manager.set_theme(theme)
                dialog.setStyleSheet(manager.get_stylesheet())
                self.assertIn('QPlainTextEdit', manager.get_stylesheet())
                self.assertEqual(manager.current_theme, theme)
            self.assertIn('Recibo histórico' if language=='es' else 'historical receipt', dialog.details.toPlainText())

    def window(self):
        with patch('core.logging_config.setup_logging'):
            import main as product
        self.stack.enter_context(patch.object(product, 'get_library_service', return_value=self.service))
        self.stack.enter_context(patch.object(product.MainWindow, 'load_available_modules'))
        self.stack.enter_context(patch.object(product, 'request_close_library_services'))
        self.stack.enter_context(patch.object(product, 'library_services_shutdown_ready', return_value=True))
        self.stack.enter_context(patch.object(product, 'close_library_services'))
        window = product.MainWindow()
        window.tr = self.tr
        self.windows.append(window)
        window.show()
        return window

    def test_dashboard_opens_one_dialog_and_reopens_after_completed_close(self):
        window = self.window()
        window.open_producer_recovery()
        first = window._producer_recovery_dialog
        self.dialogs.append(first)
        window.open_producer_recovery()
        self.assertIs(window._producer_recovery_dialog, first)
        self.assertIs(first.service, self.service)
        first.reject()
        self.assertTrue(pump(lambda: not isValid(first)))
        window.open_producer_recovery()
        second = window._producer_recovery_dialog
        self.dialogs.append(second)
        self.assertIsNot(first, second)
        window._shutting_down = True
        window.open_producer_recovery()
        self.assertIs(window._producer_recovery_dialog, second)

    def test_dashboard_close_cancels_review_then_closes_native_writer_after_join(self):
        journal = self.derived()
        window = self.window()
        window.open_producer_recovery()
        dialog = window._producer_recovery_dialog
        self.dialogs.append(dialog)
        dialog.select_journal(journal)
        entered, release, calls = self.gate()
        dialog.review_current()
        self.assertTrue(entered.wait(1))
        import main as product
        with patch.object(product, 'request_close_library_services') as close_writer:
            window.close()
            self.assertTrue(dialog._closed)
            self.assertFalse(window._shutdown_finished)
            self.assertFalse(close_writer.called)
            window.open_producer_recovery()
            self.assertIs(window._producer_recovery_dialog, dialog)
            release.set()
            self.assertTrue(pump(lambda: window._shutdown_finished))
            self.assertTrue(close_writer.called)

    def test_dashboard_keeps_writer_open_until_accepted_reconciliation_finishes(self):
        journal = self.derived(fail_commit=True)
        window = self.window()
        window.open_producer_recovery()
        dialog = window._producer_recovery_dialog
        self.dialogs.append(dialog)
        dialog.select_journal(journal)
        dialog.review_current()
        self.settle(dialog)
        # The action is committed; keep its worker alive before terminal delivery.
        entered, release, calls = self.gate('reconcile', finish_first=True)
        dialog.apply_review()
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.inspect(journal)['status'], 'confirmed_current')
        import main as product
        with patch.object(product, 'request_close_library_services') as close_writer:
            window.close()
            self.assertFalse(close_writer.called)
            self.assertFalse(window._shutdown_finished)
            release.set()
            self.assertTrue(pump(lambda: window._shutdown_finished))
            self.assertTrue(close_writer.called)


if __name__ == '__main__':
    unittest.main(verbosity=2)
