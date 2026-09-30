"""Exportación explícita desde Qt con carpetas/SQLite temporales."""
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
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from core.curation_tasks import CurationExportTasks
from core.curation_sidecar import NAME, read_curation_page
from core.event_bus import EventBus
from core.library_repository import LibraryRepository
import curation_snapshot_selftest as helpers
import librarian_data_selftest as prior


class ExportQt(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.modules = []
        self.releases = []
        self.bus = EventBus()
        self.addCleanup(self.shutdown)
        self.folder = Path(self.temp.name) / 'colección ñ'
        self.folder.mkdir()

    shutdown = prior.LibrarianQt.shutdown
    view = prior.LibrarianQt.view
    ready = prior.LibrarianQt.ready

    def selected(self, count=203):
        paths = [(self.folder / f'{i:04}.png').as_posix() for i in range(count)]
        self.service.submit('register_files_minimal', [dict(path=path, filename=Path(path).name,
            size=1, created=1, hash_original='a' * 64) for path in paths]).result(3)
        module = self.view()
        self.ready(module)
        module.load_folder_paths(self.folder.as_posix())
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        return module, paths

    def hold(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = CurationExportTasks.read
        calls = []
        def read(tasks, operation, **kwargs):
            self.assertNotEqual(threading.get_ident(), threading.main_thread().ident)
            calls.append((operation, dict(kwargs)))
            entered.set()
            self.assertTrue(release.wait(3))
            return original(tasks, operation, **kwargs)
        return entered, release, calls, patch.object(CurationExportTasks, 'read', read)

    def test_loading_and_browsing_do_not_export_automatically(self):
        with patch.object(CurationExportTasks, 'read', side_effect=AssertionError('automatic export')) as export:
            module, _ = self.selected()
            self.assertTrue(module.btn_export_curation.isEnabled())
            export.assert_not_called()
        self.assertFalse((self.folder / NAME).exists())

    def test_export_does_not_block_qt_and_double_start_is_ignored(self):
        module, _ = self.selected()
        entered, release, calls, hook = self.hold()
        with hook:
            before = time.monotonic()
            module.export_folder_curation()
            self.assertLess(time.monotonic() - before, .3)
            self.assertTrue(entered.wait(1))
            self.assertFalse(module.btn_export_curation.isEnabled())
            module.export_folder_curation()
            tick = []
            QTimer.singleShot(0, lambda: tick.append(True))
            self.assertTrue(helpers.pump(lambda: tick))
            release.set()
            self.assertTrue(helpers.pump(lambda: module._exporting is None))
        self.assertEqual(len(calls), 1)
        self.assertIn('203', module.lbl_export.text())
        self.assertTrue(module.btn_export_curation.isEnabled())

    def test_entire_folder_export_is_not_current_page_or_handoff_cap(self):
        module, paths = self.selected(1001)
        self.assertEqual(len(module.total_paths), 100)
        module.export_folder_curation()
        self.assertTrue(helpers.pump(lambda: module._exporting is None))
        self.assertIn('1001', module.lbl_export.text())
        self.assertEqual(read_curation_page(self.folder)['total'], len(paths))
        self.assertEqual(self.service.read('count_files'), len(paths))

    def test_filter_or_headless_set_requires_explicit_folder_selection(self):
        module = self.view()
        self.ready(module)
        self.assertFalse(module.btn_export_curation.isEnabled())
        module, paths = self.selected()
        module.active_tags[:] = ['missing']
        module.perform_search()
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        module.export_folder_curation()
        self.assertFalse(module.btn_export_curation.isEnabled())
        self.assertIsNone(module.export_reader._thread)
        module.active_tags.clear()
        module.perform_search()
        self.assertTrue(helpers.pump(lambda: module._browse_ready))
        self.assertTrue(module.btn_export_curation.isEnabled())
        module.run_headless(paths)
        module.export_folder_curation()
        self.assertFalse(module.btn_export_curation.isEnabled())
        self.assertIsNone(module.export_reader._thread)

    def test_folder_change_cannot_redirect_captured_export_or_result(self):
        module, _ = self.selected()
        entered, release, calls, hook = self.hold()
        other = self.folder.parent / 'otra carpeta'
        other.mkdir()
        with hook:
            module.export_folder_curation()
            self.assertTrue(entered.wait(1))
            module.load_folder_paths(other.as_posix())
            self.assertTrue(helpers.pump(lambda: module._browse_ready))
            release.set()
            self.assertTrue(helpers.pump(lambda: module._exporting is None))
        self.assertEqual(calls[0][1]['folder'], self.folder.as_posix())
        self.assertTrue((self.folder / NAME).exists())
        self.assertFalse((other / NAME).exists())
        self.assertIn(str(self.folder / NAME), module.lbl_export.text())

    def test_error_visible_then_retry_publishes_and_clears_busy(self):
        module, _ = self.selected()
        with patch.object(CurationExportTasks, 'read', side_effect=ValueError('fixture_failure')):
            module.export_folder_curation()
            self.assertTrue(helpers.pump(lambda: module._exporting is None))
            self.assertIn('fixture_failure', module.lbl_export.text())
        self.assertTrue(module.btn_export_curation.isEnabled())
        self.assertFalse((self.folder / NAME).exists())
        module.export_folder_curation()
        self.assertTrue(helpers.pump(lambda: module._exporting is None))
        self.assertTrue((self.folder / NAME).exists())
        self.assertNotIn('fixture_failure', module.lbl_export.text())

    def test_unload_cancels_without_wait_or_late_widget_delivery(self):
        module, _ = self.selected()
        entered, release, _, hook = self.hold()
        with hook:
            module.export_folder_curation()
            self.assertTrue(entered.wait(1))
            status = module.lbl_export.text()
            before = time.monotonic()
            module.on_unload()
            self.assertLess(time.monotonic() - before, .1)
            self.assertTrue(module.export_reader._cancel.is_set())
            release.set()
            module.export_reader._thread.join(3)
            self.assertFalse(module.export_reader._thread.is_alive())
            QApplication.processEvents()
        self.assertEqual(module.lbl_export.text(), status)
        self.assertFalse((self.folder / NAME).exists())

    def test_pending_folder_write_prevents_export_until_commit_and_browse_confirmed(self):
        module, _ = self.selected()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = LibraryRepository.add_watched_folder
        def held(repo, path):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(repo, path)
        with patch.object(LibraryRepository, 'add_watched_folder', held):
            module._submit_folder_write('add_watched_folder', self.folder.as_posix())
            self.assertTrue(entered.wait(1))
            self.assertFalse(module.btn_export_curation.isEnabled())
            module.export_folder_curation()
            self.assertIsNone(module.export_reader._thread)
            release.set()
            self.assertTrue(helpers.pump(lambda: module._folder_write is None and module._browse_ready))
        self.assertTrue(module.btn_export_curation.isEnabled())

    def test_real_tooltip_theme_and_bilingual_status_formats(self):
        module, _ = self.selected()
        self.assertTrue(module.btn_export_curation.toolTip())
        self.assertIn('subfolders', module.btn_export_curation.toolTip())
        self.assertIn('border', module.btn_export_curation.styleSheet())
        self.assertNotIn('black', module.btn_export_curation.styleSheet())
        root = Path(__file__).resolve().parents[1]
        for language in ('en', 'es'):
            values = json.loads((root / 'locales' / f'{language}.json').read_text(encoding='utf-8'))
            for key in ('lib.export', 'lib.export_tip', 'lib.exporting', 'lib.exported', 'lib.export_error'):
                self.assertTrue(values[key].format(folder='fixture', reason='fixture', count=203, path='fixture.db', missing=0))


class TaskContract(unittest.TestCase):
    def test_construction_and_invalid_task_or_folder_do_no_filesystem_work(self):
        with patch('core.curation_tasks.export_curation', side_effect=AssertionError('export')):
            tasks = CurationExportTasks('not-opened.db')
            with self.assertRaisesRegex(ValueError, 'unknown_curation_task'):
                tasks.read('unknown', folder='example')
            with self.assertRaisesRegex(ValueError, 'invalid_export_folder'):
                tasks.read('export_curation', folder='')

    def test_adapter_passes_captured_folder_and_cancellation_to_worker_api(self):
        event = threading.Event()
        tasks = CurationExportTasks('captured.db')
        with patch('core.curation_tasks.export_curation', return_value=dict(copies=3)) as export:
            self.assertEqual(tasks.read('export_curation', folder='captured folder', cancel_event=event), dict(copies=3))
            self.assertEqual(export.call_args.args, ('captured.db', 'captured folder'))
            self.assertFalse(export.call_args.kwargs['cancelled']())
            event.set()
            self.assertTrue(export.call_args.kwargs['cancelled']())


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main(verbosity=2)
