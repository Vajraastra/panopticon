"""Recorded provenance: private SQLite, responsive Qt and stale-result isolation."""
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
from modules.librarian.module import LibrarianModule
import curation_snapshot_selftest as helpers

APP = QApplication.instance() or QApplication([])


class Provenance(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.module = LibrarianModule()
        self.module._auto_scan_pending = False
        self.module.on_load({'data_service': self.service})
        self.module.get_view()
        self.panel = self.module.provenance
        self.releases = []
        self.addCleanup(self.close)
        self.assertTrue(helpers.pump(lambda: self.module._overview_ready))

    def close(self):
        for release in self.releases:
            release.set()
        self.module.on_unload()
        self.assertTrue(helpers.pump(self.module.shutdown_ready))
        self.module.view.close()
        self.module.view.deleteLater()
        APP.processEvents()

    def register_origin(self, path, artist='Artista ñ', url='https://example.test/post/1'):
        self.service.submit('register_files_minimal', [dict(path=path,
            filename=path.rsplit('/', 1)[-1], size=1, created=1,
            artist=artist, origin_url=url)]).result(3)

    def settle(self):
        self.assertTrue(helpers.pump(lambda: not self.panel.reader._active))

    def test_click_shows_literal_copyable_values_and_full_path(self):
        path = 'C:/fixture/ñ.png'
        self.register_origin(path, '<b>Artista</b>', 'https://example.test/?a=1&b=2')
        with patch('modules.librarian.module.WorkerPreviewDialog') as preview:
            self.module.on_thumbnail_clicked(path)
            preview.return_value.exec.assert_called_once()
        self.settle()
        for key, expected in [('path', path), ('artist', '<b>Artista</b>'),
                              ('origin_url', 'https://example.test/?a=1&b=2')]:
            self.assertEqual(self.panel.fields[key].text(), expected)
            self.assertTrue(self.panel.fields[key].isReadOnly())
            self.assertTrue(self.panel.fields[key].toolTip())

    def test_missing_origin_and_unregistered_path_are_distinct(self):
        self.register_origin('C:/fixture/a.png', None, None)
        self.panel.show_path('C:/fixture/a.png'); self.settle()
        self.assertIn('No artist', self.panel.status.text())
        self.panel.show_path('C:/fixture/missing.png'); self.settle()
        self.assertIn('no active', self.panel.status.text())
        self.assertEqual(self.panel.fields['artist'].text(), '')
        self.assertEqual(self.service.read('count_files'), 1)

    def test_read_is_bounded_and_rejects_invalid_path(self):
        self.register_origin('C:/fixture/long.png', 'a'*1100, 'u'*5000)
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

    def test_scope_change_invalidates_details_and_revision_refreshes_them(self):
        path = 'C:/fixture/a.png'
        self.register_origin(path, 'before')
        self.panel.show_path(path); self.settle()
        self.register_origin(path, 'after')
        self.module._on_native_revision({})
        self.settle()
        self.assertEqual(self.panel.fields['artist'].text(), 'after')
        self.module.load_folder_paths('C:/other')
        self.assertIsNone(self.panel.path)
        self.assertEqual(self.panel.fields['artist'].text(), '')

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
            self.module.on_unload()
            self.assertFalse(self.module.shutdown_ready())
            release.set()
            self.assertTrue(helpers.pump(self.module.shutdown_ready))
        APP.processEvents()
        self.assertEqual(self.panel.fields['artist'].text(), '')


if __name__ == '__main__':
    unittest.main()
