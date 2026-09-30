"""Flujo Qt real, SQLite/archivos temporales; no biblioteca ni Cherry reales."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
from shiboken6 import isValid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QPushButton, QSpinBox, QComboBox, QLineEdit
from PySide6.QtGui import QCloseEvent
from core.paths import ProjectPaths
from core.theme_manager import ThemeManager
from core.event_bus import EventBus
from modules.duplicate_finder.module import DuplicateFinderModule, _LIVE_WORKERS
from modules.duplicate_finder.logic.exact_review import ExactDuplicateReview, _probe
from modules.duplicate_finder.logic.visual_review import _hash
import duplicate_exact_review_selftest as prior
from curation_snapshot_selftest import pump


class Language:
    def __init__(self, code='es'):
        self.values = json.loads((Path(__file__).resolve().parents[1]/'locales'/f'{code}.json').read_text(encoding='utf-8'))

    def tr(self, key, default=None):
        return self.values.get(key, default or key)


class GUI(prior.Review):
    def setUp(self):
        super().setUp()
        self.events, self.releases = [], []
        self.root_patch = patch.object(ProjectPaths, 'root', return_value=Path(self.temp.name))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.theme = ThemeManager(str(Path(self.temp.name)/'theme.json'))
        self.module = DuplicateFinderModule()
        self.bus = EventBus()
        self.bus.subscribe('library_changed', self.events.append)
        self.module.on_load(dict(data_service=self.service, locale_manager=Language(), event_bus=self.bus, theme_manager=self.theme))
        self.view = self.module.get_view()
        self.module.folder_path = str(self.root)
        self.module.lbl_folder.setText(str(self.root))
        self.module.recovery.setText(str((Path(self.temp.name)/'recovery').resolve()))
        self.addCleanup(self.shutdown)

    def shutdown(self):
        for release in self.releases:
            release.set()
        self.module.on_unload()
        if self.module.worker is not None:
            self.assertTrue(self.module.worker.wait(3000))
        if self.module.retirement_worker is not None:
            self.assertTrue(self.module.retirement_worker.wait(3000))  # Sólo fixture, I/O liberado.
        if self.module.native_worker is not None:
            self.assertTrue(self.module.native_worker.wait(3000))
        self.assertTrue(pump(lambda: not _LIVE_WORKERS))
        if isValid(self.view):
            self.view.close()
        QApplication.processEvents()

    def scan_gui(self):
        self.module.run_scan()
        self.assertTrue(pump(lambda: self.module.worker is None))
        self.assertFalse(self.module._busy)
        return self.module.review

    def child(self, path):
        for index in range(self.module.tree.topLevelItemCount()):
            group = self.module.tree.topLevelItem(index)
            for child in range(group.childCount()):
                item = group.child(child)
                if item.data(0, Qt.UserRole)[1] == path.as_posix():
                    return item
        raise AssertionError('visible fixture copy missing')

    def accepted_retirement(self):
        with patch.object(self.module, '_confirm_retirement', return_value=True):
            self.module.delete_files()
        self.assertTrue(pump(lambda: not self.module._acting))

    def test_exact_auto_select_marks_raw_even_when_curated_is_last(self):
        raw, curated = self.pair()
        result = self.scan_gui()
        self.assertTrue(result['complete'])
        self.assertEqual(result['mode'], 'hash')
        self.assertEqual(self.module.btn_auto_select.text(), 'Seleccionar copias recomendadas')
        self.assertEqual(self.module.btn_delete.text(), 'Retirar copias seleccionadas')
        self.assertFalse(self.child(curated).flags() & Qt.ItemIsUserCheckable)
        self.module.auto_select_duplicates()
        self.assertEqual(self.module._checked_paths, {raw.as_posix()})
        self.assertEqual(self.child(raw).checkState(0), Qt.Checked)
        self.assertTrue(raw.exists() and curated.exists())
        self.assertTrue(self.module.btn_delete.isEnabled())

    def test_complete_documented_state_visible_without_gui_io(self):
        raw, curated = self.pair()
        self.scan_gui()
        with (patch.object(self.service, 'read', side_effect=AssertionError('GUI SQL')),
              patch.object(Path, 'stat', side_effect=AssertionError('GUI filesystem'))):
            self.module.tree.setCurrentItem(self.child(curated))
            value = json.loads(self.module.details.toPlainText())
        self.assertEqual(value['copy']['state']['file']['rating'], 5)
        self.assertEqual(value['copy']['state']['tags'][0]['name'], 'documented')
        self.assertIn('source', value['copy']['state']['tags'][0])
        self.assertIn('category', value['copy']['state']['tags'][0])
        self.assertIsNotNone(value['copy']['fingerprint'])

    def test_manual_conflicting_groups_have_no_checkboxes_or_discard(self):
        a = self.file('a.png', tags=['alpha'])
        b = self.file('b.png', tags=['beta'])
        self.scan_gui()
        self.module.auto_select_duplicates()
        self.assertEqual(self.module._checked_paths, set())
        self.assertFalse(self.module.btn_delete.isEnabled())
        self.assertFalse(self.child(a).flags() & Qt.ItemIsUserCheckable)
        self.assertFalse(self.child(b).flags() & Qt.ItemIsUserCheckable)

    def test_incomplete_or_unknown_identity_never_enables_retirement(self):
        raw, curated = self.pair()
        self.file('unknown.png', register=False)
        self.scan_gui()
        self.assertFalse(self.module.btn_auto_select.isEnabled())
        self.assertFalse(self.module.btn_delete.isEnabled())
        with patch.object(self.service, 'read', side_effect=RuntimeError('fixture-read-error')):
            result = self.scan_gui()
        self.assertFalse(result['complete'])
        self.assertIn('incompleta', self.module.status.text())
        self.assertFalse(self.module.btn_auto_select.isEnabled())
        self.assertTrue(raw.exists() and curated.exists())

    def test_weight_change_consumes_approval_and_requires_rescan(self):
        raw, curated = self.pair()
        self.scan_gui()
        self.module.auto_select_duplicates()
        captured = deepcopy(self.module.review['policy'])
        self.module.weights['manual_tag_weight'].setValue(99)
        self.assertFalse(self.module.btn_delete.isEnabled())
        self.assertFalse(self.module.btn_auto_select.isEnabled())
        self.assertEqual(self.module.review['policy'], captured)
        self.assertEqual(self.module._checked_paths, set())
        with patch.object(self.module, '_confirm_retirement', side_effect=AssertionError('stale approval')):
            self.module.delete_files()
        self.scan_gui()
        self.assertEqual(self.module.review['policy']['manual_tag_weight'], 99)

    def test_guard_against_double_start_and_controls_frozen_during_io(self):
        self.pair()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def blocked(path, event):
            entered.set()
            self.assertTrue(release.wait(3))
            return _probe(path, event)
        with patch('modules.duplicate_finder.logic.exact_review._probe', blocked):
            self.module.run_scan()
            first = self.module.worker
            self.assertTrue(entered.wait(1))
            self.module.run_scan()
            self.assertIs(self.module.worker, first)
            self.assertFalse(self.module.btn_run.isEnabled())
            self.assertFalse(self.module.combo_mode.isEnabled())
            self.assertFalse(self.module.recovery.isEnabled())
            self.assertTrue(self.module.btn_cancel.isEnabled())
            release.set()
            self.assertTrue(pump(lambda: self.module.worker is None))
        self.assertTrue(self.module.btn_run.isEnabled())

    def test_cancel_scan_remains_responsive_then_delivers_terminal(self):
        raw, curated = self.pair()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def blocked(path, event):
            entered.set()
            self.assertTrue(release.wait(3))
            return _probe(path, event)
        with patch('modules.duplicate_finder.logic.exact_review._probe', blocked):
            self.module.run_scan()
            self.assertTrue(entered.wait(1))
            ticks = []
            QTimer.singleShot(0, lambda: ticks.append(True))
            started = time.monotonic()
            self.module.cancel()
            self.assertLess(time.monotonic()-started, .1)
            self.assertTrue(pump(lambda: ticks))
            self.assertTrue(self.module._busy)
            release.set()
            self.assertTrue(pump(lambda: self.module.worker is None))
        self.assertTrue(self.module.review['cancelled'])
        self.assertIn('cancelado', self.module.status.text())
        self.assertTrue(raw.exists() and curated.exists())

    def test_late_result_after_unload_cannot_touch_view(self):
        raw, curated = self.pair()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def blocked(path, event):
            entered.set()
            self.assertTrue(release.wait(3))
            return _probe(path, event)
        with patch('modules.duplicate_finder.logic.exact_review._probe', blocked):
            self.module.run_scan()
            worker = self.module.worker
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.module.on_unload()
            self.assertLess(time.monotonic()-started, .1)
            self.assertIn(worker, _LIVE_WORKERS)
            self.module.status.setText('closed fixture sentinel')
            release.set()
            self.assertTrue(pump(lambda: self.module.worker is None))
        self.assertEqual(self.module.status.text(), 'closed fixture sentinel')
        self.assertIsNone(self.module.review)
        self.assertTrue(raw.exists() and curated.exists())

    def test_error_terminal_reenables_start_without_discard_controls(self):
        self.pair()
        with patch.object(ExactDuplicateReview, 'scan', side_effect=RuntimeError('fixture scan error')):
            result = self.scan_gui()
        self.assertEqual(result['error'], 'fixture scan error')
        self.assertTrue(self.module.btn_run.isEnabled())
        self.assertFalse(self.module.btn_delete.isEnabled())
        self.assertFalse(self.module.btn_auto_select.isEnabled())

    def test_recoverable_retirement_runs_outside_qt_and_publishes_refresh(self):
        raw, curated = self.pair()
        self.scan_gui()
        self.module.auto_select_duplicates()
        read, submit = self.service.read, self.service.submit
        calls = []
        def outside_read(*args, **kwargs):
            calls.append(threading.get_ident())
            return read(*args, **kwargs)
        def outside_submit(*args, **kwargs):
            calls.append(threading.get_ident())
            return submit(*args, **kwargs)
        with (patch.object(self.service, 'read', outside_read), patch.object(self.service, 'submit', outside_submit)):
            self.accepted_retirement()
        self.assertTrue(calls)
        self.assertNotIn(threading.get_ident(), calls)
        self.assertFalse(raw.exists())
        self.assertTrue(curated.exists())
        self.assertEqual(self.events, [str(self.root)])
        self.assertIn('journal.json', self.module.action_status.toPlainText())
        self.assertFalse(self.module.btn_delete.isEnabled())
        self.assertFalse(self.module.btn_auto_select.isEnabled())
        self.assertEqual(self.service.read('count_files'), 1)

    def test_declined_concrete_confirmation_has_no_side_effects(self):
        raw, curated = self.pair()
        self.scan_gui()
        self.module.auto_select_duplicates()
        with patch.object(QMessageBox, 'exec', return_value=QMessageBox.No) as modal:
            self.module.delete_files()
        self.assertEqual(modal.call_count, 1)
        self.assertFalse(self.module._acting)
        self.assertTrue(raw.exists() and curated.exists())
        self.assertEqual(self.service.read('count_files'), 2)

    def test_modal_option_change_invalidates_approval_before_admission(self):
        raw, curated = self.pair()
        self.scan_gui()
        self.module.auto_select_duplicates()
        def changed(selected, recovery):
            self.module.weights['rating_weight'].setValue(100)
            return True
        with patch.object(self.module, '_confirm_retirement', changed):
            self.module.delete_files()
        self.assertFalse(self.module._acting)
        self.assertTrue(raw.exists() and curated.exists())

    def test_recovery_path_is_captured_before_modal_not_redirected_after_it(self):
        raw, curated = self.pair()
        self.scan_gui()
        self.module.auto_select_duplicates()
        original = Path(self.module.recovery.text())
        def changed(selected, recovery):
            self.assertEqual(recovery, str(original))
            self.module.recovery.setText(str(original.parent/'redirected'))
            return True
        with patch.object(self.module, '_confirm_retirement', changed):
            self.module.delete_files()
        self.assertTrue(pump(lambda: not self.module._acting))
        self.assertTrue(list(original.glob('*/journal.json')))
        self.assertFalse((original.parent/'redirected').exists())
        self.assertTrue(curated.exists())

    def test_curation_changed_before_retirement_requires_new_scan(self):
        raw, curated = self.pair()
        self.scan_gui()
        self.module.auto_select_duplicates()
        self.service.submit('edit_curation', curated.as_posix(), add_tags=['new-data']).result(3)
        self.accepted_retirement()
        self.assertTrue(raw.exists() and curated.exists())
        self.assertIn('duplicate_group_changed_since_review', self.module.action_status.toPlainText())
        self.assertFalse(self.module.btn_delete.isEnabled())

    def test_cancel_action_before_admission_delivers_terminal_and_unblocks_gui(self):
        raw, curated = self.pair()
        self.scan_gui()
        self.module.auto_select_duplicates()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def blocked(path, event):
            entered.set()
            self.assertTrue(release.wait(3))
            return _probe(path, event)
        with (patch('modules.duplicate_finder.logic.retirement._probe', blocked),
              patch.object(self.module, '_confirm_retirement', return_value=True)):
            self.module.delete_files()
            self.assertTrue(entered.wait(1))
            self.module.cancel()
            release.set()
            self.assertTrue(pump(lambda: not self.module._acting))
        self.assertTrue(raw.exists() and curated.exists())
        self.assertEqual(self.service.read('count_files'), 2)
        self.assertTrue(self.module.btn_run.isEnabled())

    def test_close_after_accepted_commit_finishes_moves_without_ui_delivery(self):
        raw, curated = self.pair()
        self.scan_gui()
        self.module.auto_select_duplicates()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        submit = self.service.submit
        def accepted(*args, **kwargs):
            future = submit(*args, **kwargs)
            future.result(3)
            entered.set()
            self.assertTrue(release.wait(3))
            return future
        with (patch.object(self.service, 'submit', accepted),
              patch.object(self.module, '_confirm_retirement', return_value=True)):
            self.module.delete_files()
            self.assertTrue(entered.wait(1))
            self.module.on_unload()
            self.module.action_status.setPlainText('closed action sentinel')
            release.set()
            self.assertTrue(pump(lambda: self.module.retirement_worker is None))
        self.assertFalse(raw.exists())
        self.assertTrue(curated.exists())
        self.assertEqual(self.module.action_status.toPlainText(), 'closed action sentinel')
        self.assertTrue(list(Path(self.module.recovery.text()).glob('*/journal.json')))

    def dashboard(self):
        with patch('core.logging_config.setup_logging'):
            from main import MainWindow
        host = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(host)
        host.loaded_modules = {'Duplicate Finder': self.module}
        host.module_windows = {}
        host._shutting_down = host._shutdown_finished = host._shutdown_retry_scheduled = False
        host.tr = Language().tr
        self.addCleanup(host.deleteLater)
        return host

    def test_dashboard_keeps_owner_alive_until_accepted_retirement_finishes(self):
        raw, curated = self.pair()
        self.scan_gui()
        self.module.auto_select_duplicates()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        submit = self.service.submit
        def accepted(*args, **kwargs):
            future = submit(*args, **kwargs)
            future.result(3)
            entered.set()
            self.assertTrue(release.wait(3))
            return future
        host = self.dashboard()
        beats = []
        timer = QTimer()
        timer.timeout.connect(lambda: beats.append(True))
        timer.start(2)
        self.addCleanup(timer.stop)
        with (patch.object(self.service, 'submit', accepted),
              patch.object(self.module, '_confirm_retirement', return_value=True),
              patch.object(self.module, 'on_unload', wraps=self.module.on_unload) as unload,
              patch('main.close_library_services') as close_owner):
            self.module.delete_files()
            self.assertTrue(entered.wait(1))
            event = QCloseEvent()
            start = time.monotonic()
            host.closeEvent(event)
            self.assertLess(time.monotonic()-start, .1)
            self.assertFalse(event.isAccepted())
            self.assertFalse(self.module.shutdown_ready())
            self.assertFalse(close_owner.called)
            self.assertTrue(pump(lambda: len(beats) >= 2))
            host.closeEvent(QCloseEvent())
            unload.assert_called_once()
            release.set()
            self.assertTrue(pump(lambda: host._shutdown_finished))
            close_owner.assert_called_once_with(permanent=True)
            unload.assert_called_once()
        self.assertFalse(raw.exists())
        self.assertTrue(curated.exists())
        journals = list(Path(self.module.recovery.text()).glob('*/journal.json'))
        self.assertEqual(json.loads(journals[0].read_text(encoding='utf-8'))['phase'], 'complete')

    def test_dashboard_waits_for_cancelled_scan_without_blocking_gui(self):
        self.pair()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def blocked(path, event):
            entered.set()
            self.assertTrue(release.wait(3))
            return _probe(path, event)
        host = self.dashboard()
        with (patch('modules.duplicate_finder.logic.exact_review._probe', blocked),
              patch('main.close_library_services') as close_owner):
            self.module.run_scan()
            self.assertTrue(entered.wait(1))
            event = QCloseEvent()
            start = time.monotonic()
            host.closeEvent(event)
            self.assertLess(time.monotonic()-start, .1)
            self.assertFalse(event.isAccepted())
            self.assertFalse(close_owner.called)
            release.set()
            self.assertTrue(pump(lambda: host._shutdown_finished))
            close_owner.assert_called_once_with(permanent=True)
        self.assertIsNone(self.module.review)

    def test_retirement_worker_captures_approval_and_reports_error_once(self):
        from modules.duplicate_finder.logic.retirement_worker import DuplicateRetirementWorker
        review, selected = {'fixture': ['approved']}, ['original']
        worker = DuplicateRetirementWorker(None, review=review, selected=selected,
                                          recovery_folder='C:/fixture', token=41)
        review['fixture'].append('changed')
        selected.clear()
        self.assertEqual(worker.arguments['review'], {'fixture': ['approved']})
        self.assertEqual(worker.arguments['selected'], ['original'])
        terminal, native = [], []
        worker.finished_signal.connect(lambda *value: terminal.append(value))
        worker.finished.connect(lambda: native.append(True))
        with patch('modules.duplicate_finder.logic.retirement_worker.DuplicateRetirementTasks.read',
                   side_effect=ValueError('fixture error')):
            worker.start()
            self.assertTrue(pump(lambda: native and terminal))
        self.assertEqual(len(terminal), 1)
        self.assertEqual(len(native), 1)
        self.assertEqual(terminal[0][:2], (41, None))
        self.assertIsInstance(terminal[0][2], ValueError)
        self.assertTrue(worker.wait(3000))

    def test_rows_paginated_100_and_cross_page_selection_preserved(self):
        paths = [self.file(f'{index:03}.png') for index in range(105)]
        self.scan_gui()
        count = sum(self.module.tree.topLevelItem(i).childCount() for i in range(self.module.tree.topLevelItemCount()))
        self.assertEqual(count, 100)
        self.module.auto_select_duplicates()
        selected = self.module._checked_paths.copy()
        self.assertEqual(len(selected), 104)
        self.module._navigate(1)
        self.assertEqual(self.module.tree.topLevelItem(0).childCount(), 5)
        last = self.child(paths[-1])
        last.setCheckState(0, Qt.Unchecked)
        self.module._navigate(-1)
        self.assertEqual(self.module._checked_paths, selected-{paths[-1].as_posix()})

    def test_visual_preview_cannot_auto_select_or_retire(self):
        from PIL import Image
        a, b = self.root/'a.png', self.root/'b.png'
        Image.new('RGB', (16, 16), 'red').save(a)
        b.write_bytes(a.read_bytes())
        self.module.combo_mode.setCurrentIndex(1)
        result = self.scan_gui()
        self.assertEqual(result['mode'], 'visual')
        self.assertTrue(result['groups'])
        self.module.auto_select_duplicates()
        self.assertEqual(self.module._checked_paths, set())
        self.assertFalse(self.module.btn_delete.isEnabled())
        self.assertFalse(self.module.btn_auto_select.isEnabled())
        self.assertTrue(a.exists() and b.exists())

    def test_all_interactive_controls_have_tooltips_and_semantic_styles(self):
        controls = []
        for kind in (QPushButton, QSpinBox, QComboBox, QLineEdit):
            controls.extend(self.view.findChildren(kind))
        missing = [(type(control).__name__, control.objectName()) for control in controls if not control.toolTip().strip()]
        self.assertEqual(missing, [])
        self.assertTrue(all('color: black' not in control.styleSheet() for control in controls))
        self.assertEqual(set(self.module.weights), {'manual_tag_weight', 'other_tag_weight', 'rating_weight', 'generation_weight', 'provenance_weight'})

    def test_physical_failure_is_reported_as_incomplete_with_catalog_refresh(self):
        raw, curated = self.pair()
        self.scan_gui()
        self.module.auto_select_duplicates()
        with patch('modules.duplicate_finder.logic.retirement.os.rename', side_effect=OSError('fixture busy')):
            self.accepted_retirement()
        self.assertIn('Retiro incompleto', self.module.action_status.toPlainText())
        self.assertIn('journal.json', self.module.action_status.toPlainText())
        self.assertTrue(raw.exists() and curated.exists())
        self.assertEqual(self.service.read('count_files'), 1)
        self.assertEqual(self.events, [str(self.root)])

    def test_relative_recovery_path_rejected_before_confirmation(self):
        raw, curated = self.pair()
        self.scan_gui()
        self.module.auto_select_duplicates()
        self.module.recovery.setText('relative/recovery')
        with patch.object(self.module, '_confirm_retirement', side_effect=AssertionError('invalid path confirmation')):
            self.module.delete_files()
        self.assertIn('carpeta absoluta', self.module.action_status.toPlainText())
        self.assertTrue(raw.exists() and curated.exists())

    def test_real_widget_destruction_during_scan_retains_worker_until_finished(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        self.pair()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def blocked(path, event):
            entered.set()
            self.assertTrue(release.wait(3))
            return _probe(path, event)
        with patch('modules.duplicate_finder.logic.exact_review._probe', blocked):
            self.module.run_scan()
            worker = self.module.worker
            self.assertTrue(entered.wait(1))
            self.view.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.assertFalse(isValid(self.view))
            self.assertTrue(self.module._closed)
            self.assertIn(worker, _LIVE_WORKERS)
            release.set()
            self.assertTrue(pump(lambda: self.module.worker is None))
        self.assertIsNone(self.module.review)

    def test_gallery_handoff_uses_complete_group_without_gui_reads_or_retirement(self):
        raw, curated = self.pair()
        self.scan_gui()
        self.module.tree.setCurrentItem(self.child(curated))
        received = []
        self.module.request_open_gallery.connect(lambda paths, title: received.append((paths, title)))
        with (patch.object(self.service, 'read', side_effect=AssertionError('GUI SQL')),
              patch.object(Path, 'stat', side_effect=AssertionError('GUI filesystem'))):
            self.module.open_group()
        self.assertEqual(received, [([raw.as_posix(), curated.as_posix()], 'Revisión de duplicados')])
        self.assertEqual(self.module._checked_paths, set())
        self.assertTrue(raw.exists() and curated.exists())

    def test_gallery_handoff_never_truncates_large_group(self):
        raw, curated = self.pair()
        self.scan_gui()
        self.module.tree.setCurrentItem(self.child(curated))
        report = next(iter(self.module.review['reports'].values()))
        report['copies'] = [dict(path=f'C:/fixture/{index}.png') for index in range(1001)]
        received = []
        self.module.request_open_gallery.connect(lambda *args: received.append(args))
        self.module.open_group()
        self.assertEqual(received, [])
        self.assertIn('No se enviaron', self.module.action_status.toPlainText())

    def test_main_window_connects_duplicate_group_to_gallery_through_standard_signal(self):
        # main configura logging al importar; no abrir un handler sobre la fixture.
        with patch('core.logging_config.setup_logging'):
            from main import MainWindow
        raw, curated = self.pair()
        self.scan_gui()
        self.module.tree.setCurrentItem(self.child(curated))
        sent, gallery = [], object()
        class Host:
            def get_module(host, name):
                return {'Gallery': gallery, 'Duplicate Finder': self.module}.get(name)
            def switch_to_module(host, destination, paths):
                sent.append((destination, paths))
        MainWindow.setup_integrations(Host())
        self.module.open_group()
        self.assertEqual(sent, [(gallery, [raw.as_posix(), curated.as_posix()])])


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('GUI.'+name, sys.modules[__name__])
        for name in GUI.__dict__ if name.startswith('test_'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
