"""Regresiones Windows/SQLite sobre fixtures: nunca visita una biblioteca real."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication, QMessageBox
from core import catalog_reader
from core.inventory import InventoryStatus as Status
from core.paths import ProjectPaths
from modules.librarian.logic import indexer
from core.database_manager import DatabaseManager
from core.library_service import close_library_services
from modules.librarian.module import LibrarianModule


class IndexerSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / 'collection'
        self.folder.mkdir()
        self.db = DatabaseManager(self.root / 'central.db')
        self.addCleanup(close_library_services)
        self.inspect_conn = sqlite3.connect(self.db.db_path)
        self.addCleanup(self.inspect_conn.close)
        self.artist = patch.object(catalog_reader, '_INDEX_PATH', self.root / 'absent-index.db')
        self.artist.start()
        self.addCleanup(self.artist.stop)

    def image(self, name, folder=None):
        path = (folder or self.folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'image fixture')
        return path

    def seed(self, path):
        norm = path.as_posix()
        self.assertTrue(self.db.register_files_minimal([{
            'path': norm, 'filename': path.name, 'size': 13, 'created': 1,
        }]))
        self.assertTrue(self.db.update_file_rating(norm, 5))
        self.assertTrue(self.db.add_tag_to_file(norm, 'keep-curation'))
        return self.row(path)

    def row(self, path):
        return self.inspect_conn.execute('SELECT id,rating FROM files WHERE path=?',
                                    (path.as_posix(),)).fetchone()

    def assert_preserved(self, path, before):
        self.assertEqual(self.row(path), before)
        tags = self.inspect_conn.execute('SELECT t.name FROM tags t JOIN file_tags ft '
                                    'ON ft.tag_id=t.id WHERE ft.file_id=?', (before[0],)).fetchall()
        self.assertEqual(tags, [('keep-curation',)])

    def catalog(self, folder=None, rows=()):
        folder = folder or self.folder
        path = folder / 'catalog.db'
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE files(hash TEXT PRIMARY KEY,filename TEXT,url_source TEXT,'
                         'date_added INTEGER,file_size INTEGER)')
            conn.executemany('INSERT INTO files VALUES(?,?,?,?,?)', rows)
            conn.commit()
        return path

    def run_worker(self, folders=None, **kwargs):
        worker = indexer.IndexerWorker(self.db, folders or [str(self.folder)], deep_clean=True)
        finished, messages = [], []
        worker.finished_signal.connect(lambda: finished.append(True))
        worker.progress_signal.connect(messages.append)
        with (patch.multiple(worker, **kwargs) if kwargs else nullcontext()):
            worker.run()
        self.assertEqual(finished, [True])
        self.assertEqual('finished' in indexer.IndexerWorker.__dict__, False)
        if worker.outcome != Status.COMPLETE:
            self.assertFalse(any(m.startswith('Indexing complete:') for m in messages))
        return worker

    def test_offline_root_preserves_curation(self):
        path = self.folder / 'gone.png'
        before = self.seed(path)
        self.folder.rmdir()
        worker = self.run_worker()
        self.assertEqual(worker.outcome, Status.UNAVAILABLE)
        self.assert_preserved(path, before)

    def test_cancelled_walk_preserves_unvisited_rows(self):
        path = self.folder / 'unvisited.png'
        before = self.seed(path)
        worker = indexer.IndexerWorker(self.db, [str(self.folder)], deep_clean=True)
        finished = []
        worker.finished_signal.connect(lambda: finished.append(True))
        def interrupted(*args, **kwargs):
            worker.stop()
            yield str(self.folder), [], []
        with patch.object(indexer.os, 'walk', interrupted):
            worker.run()
        self.assertEqual(worker.outcome, Status.CANCELLED)
        self.assertEqual(finished, [True])
        self.assert_preserved(path, before)

    def test_walk_permission_error_preserves_missing_rows(self):
        path = self.folder / 'unseen.png'
        before = self.seed(path)
        def broken(folder, onerror):
            onerror(PermissionError('injected inaccessible child'))
            yield folder, [], []
        with patch.object(indexer.os, 'walk', broken):
            worker = self.run_worker()
        self.assertEqual(worker.outcome, Status.ERROR)
        self.assert_preserved(path, before)

    def test_file_stat_error_suppresses_cleanup(self):
        denied = self.image('denied.png')
        missing = self.folder / 'missing.png'
        before = self.seed(missing)
        original = os.stat
        def failing(path, *args, **kwargs):
            if Path(path) == denied:
                raise PermissionError('injected image stat')
            return original(path, *args, **kwargs)
        with patch.object(indexer.os, 'stat', failing):
            worker = self.run_worker()
        self.assertEqual(worker.outcome, Status.ERROR)
        self.assert_preserved(missing, before)

    def test_empty_accessible_folder_removes_confirmed_absence(self):
        path = self.folder / 'deleted.png'
        self.seed(path)
        worker = self.run_worker()
        self.assertEqual(worker.outcome, Status.COMPLETE)
        self.assertIsNone(self.row(path))
        self.assertEqual(self.inspect_conn.execute('SELECT count(*) FROM files_fts').fetchone()[0], 0)

    def test_valid_walk_ingests_and_removes_only_missing(self):
        live = self.image('live.png')
        before = self.seed(live)
        missing = self.folder / 'deleted.png'
        self.seed(missing)
        worker = self.run_worker()
        self.assertEqual(worker.outcome, Status.COMPLETE)
        self.assert_preserved(live, before)
        self.assertIsNone(self.row(missing))

    def test_catalog_sql_error_closes_handle_and_preserves(self):
        path = self.image('live.png')
        before = self.seed(path)
        catalog = self.folder / 'catalog.db'
        with closing(sqlite3.connect(catalog)) as conn:
            conn.execute('CREATE TABLE unrelated(value TEXT)')
            conn.commit()
        worker = self.run_worker()
        self.assertEqual(worker.outcome, Status.ERROR)
        self.assert_preserved(path, before)
        # En Windows una conexión abierta impediría renombrar este archivo.
        catalog.rename(self.folder / 'renamed.db')

    def test_nested_bad_catalog_blocks_parent_sweep(self):
        child = self.folder / 'artist'
        child.mkdir()
        (child / 'catalog.db').write_bytes(b'not sqlite')
        missing = self.folder / 'unseen.png'
        before = self.seed(missing)
        worker = self.run_worker()
        self.assertEqual(worker.outcome, Status.ERROR)
        self.assert_preserved(missing, before)

    def test_valid_empty_catalog_preserves_uncatalogued_physical_file(self):
        live = self.image('uncatalogued.png')
        before = self.seed(live)
        self.catalog()
        worker = self.run_worker()
        self.assertEqual(worker.outcome, Status.COMPLETE)
        self.assert_preserved(live, before)

    def test_catalog_read_is_unchanged_and_missing_files_are_history(self):
        live = self.image('live.png')
        self.catalog(rows=[('sha-live', live.name, 'fixture-url', 1, 13),
                           ('sha-gone', 'gone.png', None, 1, 13)])
        raw_before = (self.folder / 'catalog.db').read_bytes()
        worker = self.run_worker()
        self.assertEqual(worker.outcome, Status.COMPLETE)
        self.assertEqual((self.folder / 'catalog.db').read_bytes(), raw_before)
        self.assertEqual(self.inspect_conn.execute('SELECT hash_original FROM files').fetchall(), [('sha-live',)])
        with closing(sqlite3.connect(self.folder / 'catalog.db')) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM files').fetchone()[0], 2)

    def test_catalog_cancellation_preserves_rows(self):
        live = self.image('live.png')
        before = self.seed(live)
        self.catalog(rows=[('sha', live.name, None, 1, 13)])
        result = catalog_reader.read_image_files(self.folder, cancelled=lambda: True)
        self.assertEqual(result.status, Status.CANCELLED)
        worker = self.run_worker(_discover_tree=lambda *args: result)
        self.assertEqual(worker.outcome, Status.CANCELLED)
        self.assert_preserved(live, before)

    def test_cancel_after_discovery_before_cleanup(self):
        path = self.folder / 'deleted.png'
        before = self.seed(path)
        worker = indexer.IndexerWorker(self.db, [str(self.folder)], deep_clean=True)
        discover = worker._discover_tree
        def cancel(*args):
            result = discover(*args)
            worker.stop()
            return result
        with patch.object(worker, '_discover_tree', cancel):
            worker.run()
        self.assertEqual(worker.outcome, Status.CANCELLED)
        self.assert_preserved(path, before)

    def test_batch_failure_blocks_sweep(self):
        self.image('new.png')
        missing = self.folder / 'deleted.png'
        before = self.seed(missing)
        with patch.object(self.db, 'register_files_minimal', return_value=False):
            worker = self.run_worker()
        self.assertEqual(worker.outcome, Status.ERROR)
        self.assert_preserved(missing, before)

    def test_cancel_between_batches_stops_ingestion_and_sweep(self):
        for i in range(101):
            self.image(f'new-{i}.png')
        missing = self.folder / 'unvisited.png'
        before = self.seed(missing)
        worker = indexer.IndexerWorker(self.db, [str(self.folder)], deep_clean=True)
        worker.count_signal.connect(lambda current, total: worker.stop())
        worker.run()
        self.assertEqual(worker.outcome, Status.CANCELLED)
        self.assertEqual(self.inspect_conn.execute('SELECT count(*) FROM files').fetchone()[0], 101)
        self.assert_preserved(missing, before)

    def test_disconnect_while_checking_candidates_blocks_sweep(self):
        missing = self.folder / 'gone.png'
        before = self.seed(missing)
        known = self.db.get_known_files_in_folder
        def disconnect(folder):
            paths = known(folder)
            self.folder.rename(self.root / 'offline')
            return paths
        with patch.object(self.db, 'get_known_files_in_folder', disconnect):
            worker = self.run_worker()
        self.assertEqual(worker.outcome, Status.UNAVAILABLE)
        self.assert_preserved(missing, before)

    def test_partial_catalog_retains_valid_observations_without_sweep(self):
        live = self.image('live.png')
        denied = self.image('denied.png')
        missing = self.folder / 'missing.png'
        before = self.seed(missing)
        self.catalog(rows=[('live-sha', live.name, None, 1, 13),
                           ('denied-sha', denied.name, None, 1, 13)])
        original = Path.stat
        def failing(path, *args, **kwargs):
            if path == denied:
                raise PermissionError('injected catalog file stat')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'stat', failing):
            worker = self.run_worker()
        self.assertEqual(worker.outcome, Status.ERROR)
        self.assertIsNotNone(self.row(live))
        self.assert_preserved(missing, before)

    def test_failed_cleanup_rolls_back_and_reports_error(self):
        missing = self.folder / 'gone.png'
        before = self.seed(missing)
        self.inspect_conn.execute('DROP TABLE files_fts')
        self.inspect_conn.commit()
        worker = self.run_worker()
        self.assertEqual(worker.outcome, Status.ERROR)
        self.assert_preserved(missing, before)

    def test_failed_folder_does_not_block_healthy_folder(self):
        missing = self.folder / 'offline.png'
        before = self.seed(missing)
        self.folder.rmdir()
        healthy = self.root / 'healthy'
        healthy.mkdir()
        removed = healthy / 'deleted.png'
        self.seed(removed)
        worker = self.run_worker(folders=[str(self.folder), str(healthy)])
        self.assertEqual(worker.outcome, Status.UNAVAILABLE)
        self.assertEqual(worker.folder_statuses[str(healthy)], Status.COMPLETE)
        self.assert_preserved(missing, before)
        self.assertIsNone(self.row(removed))

    def test_root_replaced_between_discovery_and_cleanup(self):
        missing = self.folder / 'deleted.png'
        before = self.seed(missing)
        worker = indexer.IndexerWorker(self.db, [str(self.folder)], deep_clean=True)
        discover = worker._discover_tree
        def replace(*args):
            result = discover(*args)
            self.folder.rename(self.root / 'previous-root')
            self.folder.mkdir()
            return result
        with patch.object(worker, '_discover_tree', replace):
            worker.run()
        self.assertEqual(worker.outcome, Status.UNAVAILABLE)
        self.assert_preserved(missing, before)

    def test_candidate_permission_error_preserves_all_candidates(self):
        path = self.folder / 'gone.png'
        before = self.seed(path)
        with patch.object(indexer.os, 'lstat', side_effect=PermissionError('injected candidate')):
            worker = self.run_worker()
        self.assertEqual(worker.outcome, Status.ERROR)
        self.assert_preserved(path, before)

    def test_unexpected_failure_still_signals_completion(self):
        worker = self.run_worker(_discover_tree=lambda *args: (_ for _ in ()).throw(RuntimeError('injected')))
        self.assertEqual(worker.outcome, Status.ERROR)

    def test_actual_qthread_finishes_without_shadowing_finished(self):
        self.image('new.png')
        worker = indexer.IndexerWorker(self.db, [str(self.folder)], deep_clean=True)
        worker.start()
        self.assertTrue(worker.wait(5000))
        self.assertFalse(worker.isRunning())
        self.assertEqual(worker.outcome, Status.COMPLETE)
        self.assertIsNotNone(self.row(self.folder / 'new.png'))

    def test_ui_smoke_and_failed_scan_does_not_show_success(self):
        with patch.object(ProjectPaths, 'root', return_value=self.root):
            module = LibrarianModule()
            module.on_load({'event_bus': None})
            view = module.get_view()
            try:
                module.indexer_thread = indexer.IndexerWorker(module.db, [])
                module.indexer_thread.outcome = Status.CANCELLED
                with patch.object(QMessageBox, 'information') as success, patch.object(QMessageBox, 'warning') as warning:
                    module.scan_finished()
                    success.assert_not_called()
                    warning.assert_called_once()
                module.indexer_thread.outcome = Status.COMPLETE
                with patch.object(QMessageBox, 'information') as success, patch.object(QMessageBox, 'warning') as warning:
                    module.scan_finished()
                    success.assert_called_once()
                    warning.assert_not_called()
            finally:
                module._dirty_timer.stop()
                close_library_services()
                view.close()
                view.deleteLater()
                self.app.processEvents()

    def test_locale_keys_and_placeholders(self):
        root = Path(__file__).resolve().parents[1]
        en = json.loads((root / 'locales/en.json').read_text(encoding='utf-8'))
        es = json.loads((root / 'locales/es.json').read_text(encoding='utf-8'))
        keys = {key for key in en if key.startswith('lib.scan.')}
        self.assertEqual(keys, {key for key in es if key.startswith('lib.scan.')})
        for key in keys:
            en[key].format(folder='fixture', count=3)
            es[key].format(folder='fixture', count=3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
