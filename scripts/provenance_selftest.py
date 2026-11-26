"""Recorded provenance (core/components/provenance.py) and its place in the Gallery viewer.

Private SQLite, responsive Qt and stale-result isolation. Moved from Librarian with U1.
"""
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from core.library_repository import LibraryRepository
from core.components.provenance import ProvenancePanel
from core.library_async import LibraryReader
from modules.gallery.ui.viewer_window import AdvancedViewer
import curation_snapshot_selftest as helpers

APP = QApplication.instance() or QApplication([])


class Provenance(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.panel = ProvenancePanel(self.service, lambda key, default='': default)  # textos por defecto (en)
        self.views = []
        self.releases = []
        self.addCleanup(self.close)

    def close(self):
        for release in self.releases:
            release.set()
        self.panel.shutdown()
        self.assertTrue(helpers.pump(self.panel.reader.shutdown_ready))
        for view in self.views:
            view.close()
            readers = view.findChildren(LibraryReader)
            self.assertTrue(helpers.pump(lambda: all(r.shutdown_ready() for r in readers) and view.preview.shutdown_ready()))
            view.deleteLater()
        self.panel.deleteLater()
        APP.processEvents()

    def viewer(self, paths):
        view = AdvancedViewer(paths, service=self.service)
        self.views.append(view)
        return view

    def register_origin(self, path, artist='Artista ñ', url='https://example.test/post/1'):
        self.service.submit('register_files_minimal', [dict(path=path,
            filename=path.rsplit('/', 1)[-1], size=1, created=1,
            artist=artist, origin_url=url)]).result(3)

    def settle(self):
        self.assertTrue(helpers.pump(lambda: not self.panel.reader._active))

    def test_viewer_shows_literal_copyable_values(self):
        path = 'C:/fixture/ñ.png'
        self.register_origin(path, '<b>Artista</b>', 'https://example.test/?a=1&b=2')
        view = self.viewer([path])
        panel = view.provenance
        self.assertTrue(helpers.pump(lambda: not panel.reader._active and panel.path == path))
        self.assertFalse(panel.fields['path'].isVisible())  # el visor ya muestra la ruta
        self.assertEqual(panel.fields['path'].text(), path)
        for key, expected in [('artist', '<b>Artista</b>'),
                              ('origin_url', 'https://example.test/?a=1&b=2')]:
            self.assertEqual(panel.fields[key].text(), expected)
            self.assertTrue(panel.fields[key].isReadOnly())
            self.assertTrue(panel.fields[key].toolTip())

    def test_missing_origin_and_unregistered_path_are_distinct(self):
        self.register_origin('C:/fixture/a.png', None, None)
        self.panel.show_path('C:/fixture/a.png'); self.settle()
        self.assertIn('No artist', self.panel.status.text())
        self.panel.show_path('C:/fixture/missing.png'); self.settle()
        self.assertIn('no active', self.panel.status.text())
        self.assertEqual(self.panel.fields['artist'].text(), '')
        self.assertEqual(self.service.read('count_files'), 1)

    def test_read_is_bounded_and_rejects_invalid_path(self):
        # El registro ya acota artista/URL; los valores largos son datos históricos: sembrarlos directo.
        self.register_origin('C:/fixture/long.png', 'a', 'u')
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(Path(self.temp.name) / 'fixture.db')) as db, db:
            db.execute('UPDATE files SET artist=?, origin_url=? WHERE path LIKE ?', ('a'*1100, 'u'*5000, '%long.png'))
        value = self.service.read('get_file_provenance', 'C:\\fixture\\long.png')
        self.assertEqual(len(value['artist']), 1024)
        self.assertEqual(len(value['origin_url']), 4096)
        self.assertTrue(value['truncated'])
        for path in (None, '', 42, 'x'*4097):
            with self.assertRaisesRegex(ValueError, 'invalid_provenance_path'):
                self.service.read('get_file_provenance', path)

    def test_offline_history_is_not_shown_as_current_origin(self):
        path = 'C:/fixture/a.png'
        self.register_origin(path, 'historical')
        key = self.service.read('get_file_id', path)
        state = self.service.read('get_copy_version_state', key)
        self.service.submit('archive_copy', key, expected_fingerprint=state['fingerprint']).result(3)
        self.assertIsNone(self.service.read('get_file_provenance', path))
        self.register_origin('C:/fixture/current.png', 'current')
        value = self.service.read('get_file_provenance', 'C:/fixture/current.png')
        self.assertIsNone(self.service.read('get_file_provenance', path))
        self.assertEqual(value['artist'], 'current')

    def test_old_result_cannot_replace_latest_selection_and_gui_keeps_running(self):
        self.register_origin('C:/fixture/a.png', 'old')
        self.register_origin('C:/fixture/b.png', 'new')
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        actual = LibraryRepository.get_file_provenance
        def held(repo, path):
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            if path.endswith('a.png'):
                entered.set(); release.wait(3)
            return actual(repo, path)
        with patch.object(LibraryRepository, 'get_file_provenance', held):
            self.panel.show_path('C:/fixture/a.png')
            self.assertTrue(entered.wait(1))
            tick = []
            QTimer.singleShot(0, lambda: tick.append(True))
            self.assertTrue(helpers.pump(lambda: tick))
            self.panel.show_path('C:/fixture/b.png')
            release.set(); self.settle()
        self.assertEqual(self.panel.fields['artist'].text(), 'new')

    def test_viewer_navigation_replaces_details_and_clear_empties_them(self):
        self.register_origin('C:/fixture/a.png', 'first')
        self.register_origin('C:/fixture/b.png', 'second')
        view = self.viewer(['C:/fixture/a.png', 'C:/fixture/b.png'])
        panel = view.provenance
        self.assertTrue(helpers.pump(lambda: not panel.reader._active and panel.fields['artist'].text() == 'first'))
        view.next_image()
        self.assertTrue(helpers.pump(lambda: not panel.reader._active and panel.fields['artist'].text() == 'second'))
        panel.clear()
        self.assertIsNone(panel.path)
        self.assertEqual(panel.fields['artist'].text(), '')

    def test_error_removes_previous_values_and_allows_retry(self):
        path = 'C:/fixture/a.png'
        self.register_origin(path)
        self.panel.show_path(path); self.settle()
        with patch.object(LibraryRepository, 'get_file_provenance', side_effect=RuntimeError('private')):
            self.panel.show_path(path); self.settle()
        self.assertEqual(self.panel.fields['artist'].text(), '')
        self.assertIn('Could not', self.panel.status.text())
        self.panel.show_path(path); self.settle()
        self.assertEqual(self.panel.fields['artist'].text(), 'Artista ñ')

    def test_shutdown_waits_for_reader_and_ignores_late_result(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def held(repo, path):
            entered.set(); release.wait(3)
            return dict(artist='late', origin_url='', truncated=False)
        with patch.object(LibraryRepository, 'get_file_provenance', held):
            self.panel.show_path('C:/fixture/a.png')
            self.assertTrue(entered.wait(1))
            self.panel.shutdown()
            self.assertFalse(self.panel.reader.shutdown_ready())
            release.set()
            self.assertTrue(helpers.pump(self.panel.reader.shutdown_ready))
        APP.processEvents()
        self.assertEqual(self.panel.fields['artist'].text(), '')


if __name__ == '__main__':
    unittest.main()
