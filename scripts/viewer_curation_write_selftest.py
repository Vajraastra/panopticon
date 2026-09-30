"""Curación transaccional y visor Qt, sólo DB/imágenes temporales."""
import hashlib
import sqlite3
import sys
import threading
import time
import unittest
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import curation_snapshot_selftest as helpers
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from core.library_connections import open_reader
from core.library_repository import LibraryRepository
from core.library_service import LibraryError, LibraryService


class AtomicEdit(helpers.Fixture):
    path = 'C:/images/a.png'

    def snapshot(self):
        return self.service.read('get_curation_snapshot', (self.path,))[0]

    def test_batch_delta_normalizes_and_syncs_fts_once_preserving_other_data(self):
        self.service.submit('register_files_minimal', [dict(path=self.path, filename='a.png',
                            size=123, created=1, hash_original='original-sha')]).result(3)
        self.service.submit('add_tag_to_file', self.path, 'old').result(3)
        sync = LibraryRepository._fts_sync_file
        calls = []
        def tracked(repo, *args):
            calls.append(threading.get_ident())
            return sync(repo, *args)
        with patch.object(LibraryRepository, '_fts_sync_file', tracked):
            self.assertTrue(self.service.submit('edit_curation', self.path,
                add_tags=[' ALPHA ', 'alpha', 'Beta'], remove_tags=['OLD'], rating=4).result(3))
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0], threading.get_ident())
        self.assertEqual((self.snapshot()['tags'], self.snapshot()['rating']), (('alpha', 'beta'), 4))
        with closing(open_reader(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT hash_original,hash_mod,file_size FROM files').fetchone(),
                             ('original-sha', None, 123))
            self.assertEqual(conn.execute('SELECT tags FROM files_fts').fetchone()[0], 'alpha beta')

    def test_repeat_delta_and_rating_only_do_not_rewrite_fts(self):
        self.register(self.path)
        self.service.submit('edit_curation', self.path, add_tags=['alpha']).result(3)
        with patch.object(LibraryRepository, '_fts_sync_file', side_effect=AssertionError('redundant FTS')):
            self.service.submit('edit_curation', self.path, add_tags=['Alpha'],
                                remove_tags=['absent'], rating=0).result(3)
            self.service.submit('edit_curation', self.path, rating=5).result(3)
        self.assertEqual(self.snapshot()['rating'], 5)

    def test_failure_after_fts_write_rolls_back_tags_rating_and_index(self):
        self.register(self.path)
        self.service.submit('edit_curation', self.path, add_tags=['old'], rating=2).result(3)
        before = self.snapshot()
        sync = LibraryRepository._fts_sync_file
        def fail_after_write(repo, *args):
            sync(repo, *args)
            raise sqlite3.OperationalError('injected_disk_error')
        with patch.object(LibraryRepository, '_fts_sync_file', fail_after_write):
            with self.assertRaisesRegex(LibraryError, 'injected_disk_error'):
                self.service.submit('edit_curation', self.path, add_tags=['new'],
                                    remove_tags=['old'], rating=5).result(3)
        self.assertEqual(self.snapshot(), before)
        with closing(open_reader(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT name FROM tags ORDER BY name').fetchall(), [('old',)])
            self.assertEqual(conn.execute('SELECT tags FROM files_fts').fetchone(), ('old',))
        self.service.submit('edit_curation', self.path, rating=3).result(3)
        self.assertEqual(self.snapshot()['rating'], 3)

    def test_invalid_payload_fails_before_sql(self):
        bad = [dict(add_tags='string'), dict(add_tags=[''] ), dict(add_tags=[None]),
               dict(add_tags=['x' * 257]), dict(add_tags=['x'] * 257),
               dict(add_tags=['x'] * 128, remove_tags=['y'] * 129),
               dict(add_tags=['A'], remove_tags=['a']), dict(rating=True),
               dict(rating=6), dict(rating=-1), dict(rating=1.5),
               dict(rating=1, expected_copy_id=True),
               dict(rating=1, expected_copy_id=2**63),
               dict(rating=1, expected_copy_id='1'), {}]
        with closing(open_reader(self.service.db_path)) as conn:
            statements = []
            conn.set_trace_callback(statements.append)
            for payload in bad:
                with self.assertRaises(ValueError):
                    LibraryRepository(conn).edit_curation(self.path, **payload)
            with self.assertRaises(ValueError):
                LibraryRepository(conn).edit_curation('', rating=0)
            self.assertEqual(statements, [])

    def test_missing_file_does_not_register_or_create_tags(self):
        with self.assertRaisesRegex(LibraryError, 'file_not_registered'):
            self.service.submit('edit_curation', self.path, add_tags=['new'], rating=1).result(3)
        self.assertEqual(self.service.read('count_files'), 0)
        self.assertEqual(self.service.read('get_all_tags'), [])

    def test_deltas_preserve_independent_edits_from_other_consumers(self):
        self.register(self.path)
        self.service.submit('edit_curation', self.path, add_tags=['a'], rating=3).result(3)
        self.service.submit('edit_curation', self.path, add_tags=['other-consumer']).result(3)
        self.service.submit('edit_curation', self.path, add_tags=['b'], remove_tags=['a']).result(3)
        self.assertEqual(self.snapshot()['tags'], ('b', 'other-consumer'))
        self.assertEqual(self.snapshot()['rating'], 3)

    def test_zero_empty_persist_after_service_reopen(self):
        self.register(self.path)
        self.service.submit('edit_curation', self.path, add_tags=['a'], rating=5).result(3)
        self.service.submit('edit_curation', self.path, remove_tags=['a'], rating=0).result(3)
        self.service.close()
        with_service = LibraryService(self.service.db_path)
        try:
            row = with_service.read('get_curation_snapshot', (self.path,))[0]
            self.assertEqual((row['rating'], row['tags']), (0, ()))
        finally:
            with_service.close()


class AsyncEdit(helpers.Fixture):
    setUpClass = classmethod(helpers.CurationQt.setUpClass.__func__)
    stop_views = helpers.CurationQt.stop_views
    viewer = helpers.CurationQt.viewer
    grid = helpers.CurationQt.grid

    def setUp(self):
        super().setUp()
        self.views = []
        self.readers = []
        self.addCleanup(self.stop_views)
        self.extract = patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract',
                             return_value=helpers.MetadataBundle(tags=['file-tag'], rating=5))
        self.extract.start()
        self.addCleanup(self.extract.stop)

    def ready(self, view):
        self.assertTrue(helpers.pump(lambda: view._curation_ready))

    def completed(self, view):
        self.assertTrue(helpers.pump(lambda: view._pending_edit is None and view._curation_ready))

    def blocked_writer(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        edit = LibraryRepository.edit_curation
        def blocked(repo, *args, **kwargs):
            entered.set()
            release.wait(3)
            return edit(repo, *args, **kwargs)
        return entered, release, patch.object(LibraryRepository, 'edit_curation', blocked)

    def test_add_remove_rating_leave_image_bytes_unchanged_and_no_reextract(self):
        path = str(Path(self.temp.name) / 'original.png')
        from PIL import Image
        Image.new('RGB', (8, 8), 'red').save(path)
        before = hashlib.sha256(Path(path).read_bytes()).digest()
        self.register(path)
        view = self.viewer([path])
        self.ready(view)
        with patch('modules.gallery.ui.viewer_window.MetadataExtractor.extract',
                   side_effect=AssertionError('write reread image')):
            view.input_tag.setText('Alpha, beta, ALPHA')
            view.add_tag()
            self.completed(view)
            self.assertEqual(view.tags_layout.count(), 2)
            self.assertEqual(view.input_tag.text(), '')
            view.remove_tag('alpha')
            self.completed(view)
            self.assertEqual(view.tags_layout.count(), 1)
            view.cycle_current_rating()
            self.completed(view)
            self.assertEqual(view._current_rating, 1)
        self.assertEqual(hashlib.sha256(Path(path).read_bytes()).digest(), before)
        self.assertFalse(hasattr(view, '_persist_metadata'))

    def test_blocked_write_responsive_no_duplicate_and_navigation_keeps_target(self):
        paths = ['C:/images/a.png', 'C:/images/b.png']
        self.register(*paths)
        self.service.submit('edit_curation', paths[1], rating=4).result(3)
        view = self.viewer(paths)
        self.ready(view)
        entered, release, blocker = self.blocked_writer()
        with blocker:
            start = time.monotonic()
            view.cycle_current_rating()
            self.assertLess(time.monotonic() - start, .1)
            self.assertTrue(entered.wait(3))
            view.cycle_current_rating()
            self.assertEqual(view.writer._ticket, 1)
            ticks = []
            QTimer.singleShot(0, lambda: ticks.append(True))
            self.assertTrue(helpers.pump(lambda: ticks))
            view.next_image()
            self.ready(view)
            self.assertFalse(view.input_tag.isEnabled())
            self.assertFalse(view.tags_container.isEnabled())
            release.set()
            self.completed(view)
        self.assertEqual(view._current_rating, 4)
        rows = self.service.read('get_curation_snapshot', paths)
        self.assertEqual([r['rating'] for r in rows], [1, 4])

    def test_open_viewer_cannot_curate_replacement_version_with_stale_copy_id(self):
        from PIL import Image
        from core.copy_version_tasks import CopyVersionTasks
        path = str(Path(self.temp.name) / 'version.png')
        Image.new('RGB', (8, 8), 'red').save(path)
        self.register(path)
        self.service.submit('edit_curation', path, add_tags=['documented'], rating=4).result(3)
        view = self.viewer([path])
        self.ready(view)
        old_id = view._registered_id
        approval = self.service.read('get_copy_version_state', old_id)['fingerprint']
        Image.new('RGB', (8, 8), 'blue').save(path)
        CopyVersionTasks(self.service).read('make_copy_online', copy_id=old_id,
                                           expected_fingerprint=approval)
        new_id = self.service.read('get_curation_snapshot', (path,))[0]['id']
        self.assertNotEqual(new_id, old_id)
        saved = []
        view.curation_saved.connect(lambda: saved.append(True))
        view.input_tag.setText('stale-editor-tag')
        view.add_tag()
        self.completed(view)
        from core.locale_manager import LocaleManager
        self.assertIn(LocaleManager().tr('gallery.viewer.copy_changed',
            'The registered copy changed; reload it before editing'), view.lbl_curation_status.text())
        self.assertEqual(saved, [])
        current = self.service.read('get_curation_snapshot', (path,))[0]
        self.assertEqual((current['rating'], current['tags']), (0, ()))
        old = self.service.read('get_copy_version_state', old_id)['state']
        self.assertEqual(old['file']['rating'], 4)
        self.assertEqual([t['name'] for t in old['tags']], ['documented'])
        self.assertEqual(view._registered_id, new_id)
        view.input_tag.setText('new-version-tag')
        view.add_tag()
        self.completed(view)
        self.assertEqual(self.service.read('get_curation_snapshot', (path,))[0]['tags'],
                         ('new-version-tag',))

    def test_close_keeps_accepted_write_and_refreshes_parent_after_commit(self):
        path = 'C:/images/a.png'
        self.register(path)
        _, grid = self.grid([path])
        self.assertTrue(helpers.pump(lambda: not grid.reader._active))
        view = self.viewer([path])
        view.curation_saved.connect(grid.refresh)
        delivered = []
        view.curation_saved.connect(lambda: delivered.append(threading.get_ident()))
        self.ready(view)
        entered, release, blocker = self.blocked_writer()
        with blocker:
            view.cycle_current_rating()
            self.assertTrue(entered.wait(3))
            status = view.lbl_curation_status.text()
            start = time.monotonic()
            view.reject()
            self.assertLess(time.monotonic() - start, .1)
            release.set()
            self.assertTrue(helpers.pump(lambda: delivered and not grid.reader._active))
        self.assertEqual(delivered, [threading.get_ident()])
        self.assertEqual(view.lbl_curation_status.text(), status)
        self.assertTrue(view.writer._closed)
        self.assertEqual(grid.grid_layout.itemAt(0).widget().rating, 1)

    def test_queue_rejection_is_visible_retains_input_and_confirmed_state(self):
        path = 'C:/images/a.png'
        self.register(path)
        view = self.viewer([path])
        self.ready(view)
        failed = Future()
        failed.set_exception(LibraryError('edit_curation', 'queue_full'))
        with patch.object(self.service, 'submit', return_value=failed):
            view.input_tag.setText('alpha, beta')
            view.add_tag()
            self.completed(view)
        self.assertIn('queue_full', view.lbl_curation_status.text())
        self.assertIn(path, view.lbl_curation_status.text())
        self.assertEqual(view.input_tag.text(), 'alpha, beta')
        self.assertEqual(view.tags_layout.count(), 0)
        self.assertEqual(self.service.read('get_all_tags'), [])
        self.assertTrue(view.input_tag.isEnabled())

    def test_db_error_restores_controls_without_false_stars_or_success(self):
        path = 'C:/images/a.png'
        self.register(path)
        view = self.viewer([path])
        self.ready(view)
        with patch.object(LibraryRepository, 'edit_curation', side_effect=sqlite3.OperationalError('disk_error')):
            view.cycle_current_rating()
            self.completed(view)
        self.assertEqual(view._current_rating, 0)
        self.assertIn('disk_error', view.lbl_curation_status.text())
        self.assertTrue(view.lbl_rating_stars.isEnabled())

    def test_missing_external_is_read_only_then_registered_navigation_restores_controls(self):
        path = 'C:/images/a.png'
        self.register(path)
        view = self.viewer(['C:/external.png', path])
        self.ready(view)
        self.assertFalse(view.input_tag.isEnabled())
        self.assertEqual(view._current_rating, 5)
        view.input_tag.setText('new')
        view.add_tag()
        view.remove_tag('file-tag')
        view.cycle_current_rating()
        self.assertEqual(view.writer._ticket, 0)
        view.next_image()
        self.ready(view)
        self.assertTrue(view.input_tag.isEnabled())
        self.assertEqual(self.service.read('count_files'), 1)

    def test_removed_row_failure_remains_visible_and_does_not_recreate_file(self):
        path = 'C:/images/a.png'
        self.register(path)
        view = self.viewer([path])
        self.ready(view)
        self.service.submit('remove_files', [path]).result(3)
        view.cycle_current_rating()
        self.completed(view)
        from core.locale_manager import LocaleManager
        self.assertIn(LocaleManager().tr('gallery.viewer.copy_changed',
            'The registered copy changed; reload it before editing'), view.lbl_curation_status.text())
        self.assertFalse(view.input_tag.isEnabled())
        self.assertEqual(self.service.read('count_files'), 0)

    def test_deleted_owner_does_not_cancel_accepted_write(self):
        path = 'C:/images/a.png'
        self.register(path)
        view = self.viewer([path])
        self.ready(view)
        entered, release, blocker = self.blocked_writer()
        with blocker:
            view.cycle_current_rating()
            self.assertTrue(entered.wait(3))
            view.reject()
            # Destrucción real del QObject mientras el Future sigue en servicio.
            import shiboken6
            for reader in (view.curation_reader, view.tags_reader):
                if reader._thread:
                    reader._thread.join(3)
                self.readers.remove(reader)
            self.views.remove(view)
            shiboken6.delete(view)
            release.set()
            self.service.close()
        with closing(open_reader(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT rating FROM files').fetchone(), (1,))


if __name__ == '__main__':
    unittest.main(verbosity=2)
