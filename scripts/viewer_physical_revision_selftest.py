"""Open-view physical refresh and coalescing with private Windows images/Native."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication
from core.metadata.bundle import MetadataBundle
from modules.gallery.ui.viewer_window import AdvancedViewer
import curation_snapshot_selftest as fixtures


class Refresh(fixtures.Fixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.views, self.releases = [], []
        self.addCleanup(self.close_views)

    def close_views(self):
        for release in self.releases:
            release.set()
        for view in self.views:
            view.close()
        self.assertTrue(fixtures.pump(lambda: all(view.preview.shutdown_ready() and
            view.curation_reader.shutdown_ready() and view.tag_completions.reader.shutdown_ready() for view in self.views)))

    def source(self, name='one', color='red'):
        path = (Path(self.temp.name)/(name+'.png')).as_posix()
        Image.new('RGB', (32,16), color).save(path)
        self.register(path)
        return path

    def viewer(self, paths):
        view = AdvancedViewer(paths, service=self.service)
        self.views.append(view)
        self.assertTrue(fixtures.pump(lambda: view._curation_ready and not view.tag_completions.busy))
        return view

    def publish(self, path, color, metadata='changed'):
        Image.new('RGB', (32,16), color).save(path)
        self.assertTrue(self.service.submit('update_file_metadata', path, dict(positive=metadata)).result(3))

    def blue(self, view):
        return (view._preview_ready and view._preview_image is not None and
                view._preview_image.pixelColor(0,0) == QColor('blue') and view._curation_ready)

    def test_confirmed_physical_change_updates_open_preview_and_fullscreen(self):
        path = self.source()
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract', return_value=MetadataBundle(tags=['embedded'],rating=5)):
            view = self.viewer([path])
            view.input_tag.setText('unfinished draft')
            view.toggle_fullscreen()
            self.publish(path, 'blue')
            self.assertTrue(fixtures.pump(lambda: self.blue(view)))
        self.assertFalse(view.fs_window.lbl_image.pixmap().isNull())
        self.assertEqual(view.fs_window.lbl_image.pixmap().toImage().pixelColor(0,0), QColor('blue'))
        self.assertEqual(view.input_tag.text(), 'unfinished draft')
        self.assertEqual((view._current_rating, view.tags_layout.count()), (0,0))
        self.assertEqual(view._preview_revision, self.service.preview_revision_snapshot())

    def test_local_curation_refresh_does_not_decode_or_extract_file(self):
        path = self.source()
        view = self.viewer([path])
        preview = view._preview_image
        with patch('core.image_preview.QImageReader', side_effect=AssertionError('curation decode')), patch(
                'modules.gallery.ui.viewer_window.MetadataExtractor.extract', side_effect=AssertionError('curation metadata')):
            self.service.submit('edit_curation', path, add_tags=['curated'], rating=4).result(3)
            self.assertTrue(fixtures.pump(lambda: view._current_rating == 4 and view.tags_layout.count() == 1))
        self.assertEqual(view._preview_image, preview)

    def test_unknown_external_commit_conservatively_refreshes_physical_preview(self):
        path = self.source()
        view = self.viewer([path])
        Image.new('RGB',(32,16),'blue').save(path)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('UPDATE files SET rating=3')
            conn.commit()
        self.assertTrue(fixtures.pump(lambda: self.blue(view) and view._current_rating == 3))

    def test_close_during_slow_physical_refresh_discards_late_result(self):
        path = self.source()
        view = self.viewer([path])
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def slow(path):
            entered.set()
            release.wait(4)
            return MetadataBundle()
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract', slow):
            self.publish(path, 'blue')
            self.assertTrue(fixtures.pump(entered.is_set))
            view.close()
            self.assertFalse(view.preview.shutdown_ready())
            release.set()
            self.assertTrue(fixtures.pump(view.preview.shutdown_ready))
        self.assertTrue(view._closed)
        self.assertIsNone(view._preview_image)

    def test_navigation_during_slow_refresh_keeps_new_current_path(self):
        one, two = self.source('one'), self.source('two','green')
        view = self.viewer([one,two])
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def slow(path):
            if path == one:
                entered.set()
                release.wait(4)
            return MetadataBundle()
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract', slow):
            self.publish(one,'blue')
            self.assertTrue(fixtures.pump(entered.is_set))
            view.next_image()
            release.set()
            self.assertTrue(fixtures.pump(lambda: view._curation_ready and view._preview_path == two))
        self.assertEqual(view.current_idx,1)
        self.assertEqual(view._preview_image.pixelColor(0,0), QColor('green'))

    def test_physical_notice_waits_for_accepted_curation_edit_and_preserves_it(self):
        path = self.source()
        view = self.viewer([path])
        calls = []
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract', side_effect=lambda path: (calls.append(path) or MetadataBundle())):
            view._pending_edit = ('private-simulated-accepted-ticket',path,'draft',False)
            try:
                self.publish(path,'blue')
                view.revisions.poll()
                self.assertFalse(calls)
                self.assertIsNotNone(view.revisions._pending)
                self.service.submit('edit_curation',path,add_tags=['curated'],rating=4).result(3)
            finally:
                view._pending_edit = None
            self.assertTrue(fixtures.pump(lambda: self.blue(view) and view._current_rating == 4))
        self.assertEqual(view.tags_layout.count(),1)
        self.assertEqual(calls,[path])

    def test_failed_physical_reload_keeps_native_curation_and_qt_responsive(self):
        path = self.source()
        view = self.viewer([path])
        self.service.submit('edit_curation',path,add_tags=['curated'],rating=4).result(3)
        self.assertTrue(fixtures.pump(lambda: view._current_rating == 4))
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def broken(path):
            entered.set(); release.wait(4)
            raise ValueError('private extraction failure')
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract',broken):
            self.publish(path,'blue')
            self.assertTrue(fixtures.pump(entered.is_set))
            ticks=[]
            QTimer.singleShot(0,lambda:ticks.append(True))
            self.assertTrue(fixtures.pump(lambda:bool(ticks)))
            release.set()
            self.assertTrue(fixtures.pump(lambda:view._curation_ready))
        self.assertIsNone(view._preview_image)
        self.assertEqual((view._current_rating,view.tags_layout.count()),(4,1))

    def test_physical_commit_during_initial_extraction_is_not_missed(self):
        path = self.source()
        calls = []
        def metadata(source):
            calls.append(source)
            if len(calls) == 1:
                self.publish(source, 'blue', 'initial-publication')
            return MetadataBundle()
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract', metadata):
            view = self.viewer([path])
            self.assertTrue(fixtures.pump(lambda:self.blue(view)))
        self.assertEqual(calls, [path,path])
        self.assertEqual(view._preview_revision,self.service.preview_revision_snapshot())

    def test_multiple_publications_during_slow_preview_coalesce_to_latest(self):
        path = self.source()
        view = self.viewer([path])
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []
        def slow(source):
            calls.append(source)
            if len(calls) == 1:
                entered.set(); release.wait(4)
            return MetadataBundle()
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract',slow):
            self.publish(path,'blue','first')
            self.assertTrue(fixtures.pump(entered.is_set))
            self.publish(path,'green','second')
            self.publish(path,'blue','third')
            view.revisions.poll()
            self.assertEqual(calls,[path])
            self.assertIsNotNone(view.revisions._pending)
            release.set()
            self.assertTrue(fixtures.pump(lambda:self.blue(view) and len(calls)==2))
        self.assertEqual(view._preview_revision,self.service.preview_revision_snapshot())


if __name__ == '__main__':
    unittest.main(verbosity=2)
