"""Gallery explicit registration/export UI with private files, Native and Qt."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from core.library_service import LibraryService
from core.library_async import LibraryReader
from core.metadata.bundle import MetadataBundle
from core.metadata.stamper import MetadataStamper
from core.external_registration_tasks import ExternalRegistrationTasks
from core.curation_tasks import CurationExportTasks
from core.curation_sidecar import read_curation_page
from modules.gallery.ui.viewer_window import AdvancedViewer
from gallery_data_selftest import pump


class GalleryRegistration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / 'images'
        self.folder.mkdir()
        self.source = self.folder / 'external.png'
        self.other = self.folder / 'other.png'
        for path in (self.source, self.other):
            with Image.new('RGB', (24, 20), 'red') as image:
                image.save(path)
        self.bundle = MetadataBundle(positive_prompt='original prompt', model='fixture-model',
                                     seed='19', tags=['embedded'], rating=4)
        self.assertTrue(MetadataStamper.stamp(self.source, self.bundle, prepared=True))
        self.service = LibraryService(self.root / 'private.db')
        self.addCleanup(self.service.close)
        self.views = []
        self.releases = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for release in self.releases:
            release.set()
        for view in self.views:
            view.close()
            readers = view.findChildren(LibraryReader)
            self.assertTrue(pump(lambda: all(reader.shutdown_ready() for reader in readers) and view.preview.shutdown_ready()))
            view.deleteLater()
        QApplication.processEvents()

    def viewer(self, paths=None):
        view = AdvancedViewer(paths or [str(self.source), str(self.other)], service=self.service)
        self.views.append(view)
        self.assertTrue(pump(lambda: view._curation_ready and not view.curation_reader._active))
        return view

    def finish(self, view):
        self.assertTrue(pump(lambda: not view._registration_pending and view._curation_ready))

    def test_explicit_registration_enables_independent_curation_without_file_edits(self):
        before = self.source.read_bytes()
        view = self.viewer()
        self.assertTrue(view.btn_register.isEnabled())
        self.assertFalse(view.btn_add_tag.isEnabled())
        view.register_current_image()
        self.finish(view)
        self.assertTrue(view._registered)
        self.assertFalse(view.btn_register.isVisible())
        self.assertTrue(view.btn_add_tag.isEnabled())
        self.assertEqual(view._current_rating, 4)
        view.input_tag.setText('independent')
        view.add_tag()
        self.assertTrue(pump(lambda: view._pending_edit is None and view._curation_ready))
        self.assertIn('independent', self.service.read('get_curation_snapshot', [str(self.source)])[0]['tags'])
        self.assertEqual(self.source.read_bytes(), before)

    def test_registration_worker_blocks_reentry_navigation_and_keeps_gui_responsive(self):
        view = self.viewer()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        real = ExternalRegistrationTasks.read
        calls, threads = [], []
        def slow(task, *args, **kwargs):
            calls.append(args)
            threads.append(threading.get_ident())
            entered.set()
            self.assertTrue(release.wait(3))
            return real(task, *args, **kwargs)
        with patch.object(ExternalRegistrationTasks, 'read', slow):
            start = time.monotonic()
            view.register_current_image()
            self.assertLess(time.monotonic() - start, .2)
            self.assertTrue(entered.wait(1))
            view.register_current_image()
            view.next_image()
            self.assertEqual(view.current_idx, 0)
            self.assertFalse(view.btn_add_tag.isEnabled())
            ticks = []
            QTimer.singleShot(0, lambda: ticks.append(True))
            self.assertTrue(pump(lambda: ticks))
            release.set()
            self.finish(view)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(threading.get_ident(), threads)

    def test_registration_error_is_visible_and_existing_history_is_not_overwritten(self):
        self.service.submit('register_files_minimal', [dict(path=str(self.source), filename=self.source.name,
                             size=1, created=1, hash_original='a'*64)]).result(3)
        key = self.service.read('get_file_id', str(self.source))
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('UPDATE files SET is_offline=1 WHERE id=?', (key,))
            conn.commit()
        before = self.service.read('get_copy_version_state', key)
        view = self.viewer()
        view.register_current_image()
        self.finish(view)
        self.assertTrue(view._write_error)
        self.assertIn('could not be confirmed', view.lbl_curation_status.text())
        self.assertIn('has_native_history', view.lbl_curation_status.toolTip())
        self.assertEqual(self.service.read('get_copy_version_state', key), before)

    def test_close_cancels_unadmitted_registration_and_discards_late_ui(self):
        view = self.viewer()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        real = ExternalRegistrationTasks.read
        def slow(task, *args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return real(task, *args, **kwargs)
        with patch.object(ExternalRegistrationTasks, 'read', slow):
            view.register_current_image()
            self.assertTrue(entered.wait(1))
            before = view.lbl_curation_status.text()
            start = time.monotonic()
            view.close()
            self.assertLess(time.monotonic() - start, .2)
            self.assertFalse(view.registration_reader.shutdown_ready())
            release.set()
            self.assertTrue(pump(view.registration_reader.shutdown_ready))
        self.assertEqual(self.service.read('count_files'), 0)
        self.assertEqual(view.lbl_curation_status.text(), before)

    def test_close_after_admission_keeps_committed_copy_without_late_ui(self):
        view = self.viewer()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        real = self.service.submit
        def admitted(operation, *args, **kwargs):
            value = real(operation, *args, **kwargs)
            if operation == 'register_external_copy_observed':
                self.assertTrue(value.result(3))
                entered.set()
                self.assertTrue(release.wait(3))
            return value
        with patch.object(self.service, 'submit', side_effect=admitted):
            view.register_current_image()
            self.assertTrue(entered.wait(1))
            before = view.lbl_curation_status.text()
            view.close()
            release.set()
            self.assertTrue(pump(view.registration_reader.shutdown_ready))
        self.assertEqual(self.service.read('count_files'), 1)
        self.assertEqual(view.lbl_curation_status.text(), before)

    def test_explicit_folder_export_preserves_image_and_cherry_catalog(self):
        view = self.viewer()
        view.register_current_image()
        self.finish(view)
        before = self.source.read_bytes()
        catalog = self.folder / 'catalog.db'
        catalog.write_bytes(b'private Cherry marker; do not open or change')
        view.export_current_folder()
        self.assertTrue(pump(lambda: not view._export_pending))
        self.assertFalse(view._write_error, view.lbl_curation_status.toolTip())
        page = read_curation_page(self.folder)
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['copies'][0]['relative_path'], self.source.name)
        self.assertEqual(page['copies'][0]['rating'], 4)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(catalog.read_bytes(), b'private Cherry marker; do not open or change')
        self.assertIn('1 copies exported', view.lbl_curation_status.text())
        self.assertTrue(view.btn_export.isEnabled())

    def test_export_is_off_gui_blocks_reentry_and_closes_cooperatively(self):
        view = self.viewer()
        view.register_current_image()
        self.finish(view)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        real = CurationExportTasks.read
        calls = []
        def slow(task, *args, **kwargs):
            calls.append(threading.get_ident())
            entered.set()
            self.assertTrue(release.wait(3))
            return real(task, *args, **kwargs)
        with patch.object(CurationExportTasks, 'read', slow):
            view.export_current_folder()
            self.assertTrue(entered.wait(1))
            view.export_current_folder()
            view.next_image()
            self.assertEqual(view.current_idx, 0)
            view.close()
            self.assertFalse(view.export_reader.shutdown_ready())
            release.set()
            self.assertTrue(pump(view.export_reader.shutdown_ready))
        self.assertEqual(len(calls), 1)
        self.assertNotIn(threading.get_ident(), calls)
        self.assertFalse((self.folder / 'panopticon.curation.db').exists())


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main()
