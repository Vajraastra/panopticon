"""Private Windows fixtures: view-independent, durable, addition-only ingestion."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from core.cherry_ingest import CherryIngestor
from core.cherry_ingest_queue import PendingFolders
from core.cherry_ingest_scan import reconcile, within
from core.event_bus import EventBus
from core.inventory_candidates import InventoryCancelled
from core.library_repository import LibraryRepository
from core.library_service import LibraryService


def pump(condition, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(.005)
    return False


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = LibraryService(self.root/'native.db')
        self.event = threading.Event()
        self.controllers = []
        self.releases = []
        artist = patch('core.cherry_ingest_scan.get_artist_info', return_value={})
        artist.start()
        self.addCleanup(artist.stop)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for release in self.releases:
            release.set()
        for item in self.controllers:
            item.request_shutdown()
        for item in self.controllers:
            self.assertTrue(pump(item.shutdown_ready))
        self.service.close()
        self.temp.cleanup()

    def watch(self, folder=None):
        folder = self.root/'collection' if folder is None else folder
        folder.mkdir(exist_ok=True)
        self.service.submit('add_watched_folder', str(folder)).result(2)
        return folder

    def seed(self, folder, count=1):
        folder.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(folder/'catalog.db')) as conn:
            conn.execute('CREATE TABLE files(hash TEXT PRIMARY KEY,filename TEXT,url_source TEXT,date_added REAL,file_size INTEGER)')
            conn.executemany('INSERT INTO files VALUES(?,?,?,?,?)',
                ((hashlib.sha256(str(i).encode()).hexdigest(), f'{i}.png', 'private', 1, 7) for i in range(count)))
            conn.commit()
        for i in range(count):
            (folder/f'{i}.png').write_bytes(b'private')
        return folder

    def controller(self):
        bus = EventBus()
        item = CherryIngestor(self.service, bus)
        self.controllers.append(item)
        return item, bus

    def count(self):
        return self.service.read('count_files')


class QueueTests(Fixture):
    def test_queue_survives_close_and_ack_requires_current_revision(self):
        path = self.root/'pending.db'
        with closing(PendingFolders(path)) as pending:
            pending.mark(['first', 'first'])
            old = pending.page()[0]
            pending.mark(['first'])
            new = pending.page()[0]
            self.assertNotEqual(old, new)
            pending.acknowledge(*old)
            self.assertEqual(pending.page(), [new])
        with closing(PendingFolders(path)) as pending:
            self.assertEqual(pending.page(), [new])
            pending.acknowledge(*new)
            self.assertEqual(pending.page(), [])

    def test_bad_path_is_atomic_and_capacity_rolls_back(self):
        with closing(PendingFolders(self.root/'pending.db')) as pending:
            with self.assertRaises(ValueError):
                pending.mark(['good', 'bad\0'])
            self.assertEqual(pending.page(), [])
            with patch('core.cherry_ingest_queue.MAX_PENDING', 2):
                pending.mark(['a', 'b'])
                with self.assertRaises(ValueError):
                    pending.mark(['c'])
            self.assertEqual([p for p, _ in pending.page()], ['a', 'b'])

    def test_failed_root_does_not_starve_later_roots(self):
        with closing(PendingFolders(self.root/'pending.db')) as pending:
            pending.mark(['a', 'b'])
            pending.failed(*pending.page()[0], 'private failure')
            self.assertEqual([p for p, _ in pending.page()], ['b'])
            self.assertEqual(len(pending.page(time.time() + 61)), 2)

    def test_foreign_schema_and_trigger_are_never_replaced(self):
        path = self.root/'foreign.db'
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE foreign_table(value TEXT)')
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            PendingFolders(path)
        self.assertEqual(path.read_bytes(), before)
        path = self.root/'owned.db'
        with closing(PendingFolders(path)):
            pass
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TRIGGER foreign_change AFTER INSERT ON pending BEGIN DELETE FROM pending; END')
        with self.assertRaises(ValueError):
            PendingFolders(path)


class ScanTests(Fixture):
    def test_catalog_ingestion_preserves_curation_provenance_and_fts(self):
        folder = self.seed(self.watch()/'artistÄ', 205)
        path = str(folder/'0.png')
        self.service.submit('register_files_minimal', [dict(path=path, filename='0.png', size=7,
            created=1, hash_original='documented', origin_url='documented', origin_site='own', artist='curated')]).result(2)
        self.service.submit('update_file_rating', path, 5).result(2)
        key = self.service.read('get_file_id', path)
        before = (folder/'catalog.db').read_bytes()
        calls = []
        original = self.service.submit
        def submit(operation, *args, **kwargs):
            if operation == 'register_catalog_files_observed':
                calls.append(len(args[0]))
            return original(operation, *args, **kwargs)
        with patch.object(self.service, 'submit', side_effect=submit), patch('core.cherry_ingest_scan.get_artist_info', return_value={}):
            self.assertEqual(reconcile(self.service, str(folder), self.event), 205)
        self.assertEqual(calls, [100, 100, 5])
        self.assertEqual(self.count(), 205)
        self.assertEqual(self.service.read('get_file_id', path), key)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT rating,hash_original,origin_url,origin_site,artist FROM files WHERE id=?', (key,)).fetchone(),
                             (5, 'documented', 'documented', 'own', 'curated'))
        self.assertEqual((folder/'catalog.db').read_bytes(), before)
        self.assertTrue(self.service.read('check_fts')['consistent'])

    def test_nested_catalog_startup_scope_skips_uncatalogued_images(self):
        watch = self.watch()
        self.seed(watch/'artistÄ')
        (watch/'unconfirmed.png').write_bytes(b'private')
        with patch('core.cherry_ingest_scan.get_artist_info', return_value={}):
            self.assertEqual(reconcile(self.service, str(watch), self.event), 1)
        self.assertEqual(self.count(), 1)

    def test_prepared_unicode_alias_reuses_identity_and_documented_origin(self):
        folder = self.seed(self.watch()/'Árbol')
        path = str(folder/'0.png')
        self.service.submit('register_files_minimal', [dict(path=path, filename='0.png', size=7,
            created=1, hash_original='documented', origin_url='documented')]).result(2)
        self.service.submit('update_file_rating', path, 5).result(2)
        key = self.service.read('get_file_id', path)
        self.service.backfill_path_identity()
        self.assertEqual(reconcile(self.service, str(folder).swapcase(), self.event), 1)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.service.read('get_file_id', path), key)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT rating,origin_url,path FROM files').fetchone(),
                             (5, 'documented', path.replace('\\', '/')))

    def test_existing_offline_copy_is_not_automatically_reactivated(self):
        folder = self.seed(self.watch()/'artist')
        path = str(folder/'0.png')
        self.service.submit('register_files_minimal', [dict(path=path, filename='0.png', size=7, created=1)]).result(2)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('UPDATE files SET is_offline=1,rating=5')
            conn.commit()
        self.assertEqual(reconcile(self.service, str(folder), self.event), 1)
        self.assertEqual(self.count(), 0)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT is_offline,rating FROM files').fetchall(), [(1, 5)])

    def test_replaced_directory_during_source_discovery_cannot_publish(self):
        folder = self.seed(self.watch()/'artist')
        relocated = folder.with_name('old-artist')
        self.assertTrue(folder.resolve().is_relative_to(self.root.resolve()))
        self.assertTrue(relocated.resolve().is_relative_to(self.root.resolve()))
        from core.cherry_ingest_scan import read_image_files
        def changed(*args, **kwargs):
            value = read_image_files(*args, **kwargs)
            # Windows protects the open SQLite handle during the source read.
            # Replace its directory after that handle closes, before Native I/O.
            folder.rename(relocated)
            folder.mkdir()
            return value
        with patch('core.cherry_ingest_scan.read_image_files', side_effect=changed), self.assertRaisesRegex(ValueError, 'folder_changed'):
            reconcile(self.service, str(folder), self.event)
        self.assertEqual(self.count(), 0)

    def test_catalog_row_without_published_file_is_not_ingested(self):
        folder = self.seed(self.watch()/'artist')
        (folder/'0.png').unlink()
        self.assertEqual(reconcile(self.service, str(folder), self.event), 0)
        self.assertEqual(self.count(), 0)

    def test_bad_catalog_keeps_valid_commits_and_never_archives(self):
        folder = self.seed(self.watch()/'artist')
        old = str(folder/'missing.png')
        self.service.submit('register_files_minimal', [dict(path=old, filename='missing.png', size=1, created=1)]).result(2)
        with closing(sqlite3.connect(folder/'catalog.db')) as conn:
            conn.execute('INSERT INTO files VALUES(?,?,?,?,?)', ('bad', '../escape.png', '', 1, 7))
            conn.commit()
        with self.assertRaisesRegex(RuntimeError, 'fuera'):
            reconcile(self.service, str(folder), self.event)
        self.assertEqual(self.count(), 2)
        self.assertIsNotNone(self.service.read('get_file_id', old))

    def test_unwatched_and_prefix_sibling_do_not_enter_native(self):
        watch = self.watch()
        folder = self.seed(self.root/'collection-other')
        self.assertEqual(reconcile(self.service, str(folder), self.event), 0)
        self.assertFalse(within(str(folder), str(watch)))
        self.assertEqual(self.count(), 0)

    def test_windows_case_variant_uses_actual_directory_rules(self):
        folder = self.seed(self.watch()/'artistÄ')
        self.assertEqual(reconcile(self.service, str(folder).swapcase(), self.event), 1)
        self.assertEqual(self.count(), 1)

    def test_watch_removed_before_writer_commit_rolls_back_entire_batch(self):
        watch = self.watch()
        folder = self.seed(watch/'artist')
        original = self.service.submit
        removed = []
        def submit(operation, *args, **kwargs):
            if operation == 'register_catalog_files_observed' and not removed:
                removed.append(True)
                original('remove_watched_folder', str(watch)).result(2)
            return original(operation, *args, **kwargs)
        with patch.object(self.service, 'submit', side_effect=submit), self.assertRaisesRegex(Exception, 'watch_removed'):
            reconcile(self.service, str(folder), self.event)
        self.assertEqual(self.count(), 0)

    def test_cancel_keeps_confirmed_prefix_without_archival(self):
        folder = self.seed(self.watch()/'artist', 205)
        original = LibraryRepository.register_catalog_files_observed
        def commit(repo, *args):
            value = original(repo, *args)
            self.event.set()  # A running command must settle even after Stop.
            return value
        with patch.object(LibraryRepository, 'register_catalog_files_observed', commit), self.assertRaises(Exception):
            reconcile(self.service, str(folder), self.event)
        self.assertEqual(self.count(), 100)

    def test_storage_limit_does_not_register_unsealed_prefix(self):
        folder = self.seed(self.watch()/'artist')
        with patch('core.inventory_spool.MAX_STREAM_BYTES', 1), self.assertRaises(Exception):
            reconcile(self.service, str(folder), self.event)
        self.assertEqual(self.count(), 0)


class ControllerTests(Fixture):
    def test_shutdown_waits_for_running_commit_and_keeps_durable_retry(self):
        self.seed(self.watch()/'artist')
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = LibraryRepository.register_catalog_files_observed
        def blocked(repo, *args):
            entered.set()
            release.wait(3)
            return original(repo, *args)
        with patch.object(LibraryRepository, 'register_catalog_files_observed', blocked):
            item, _ = self.controller()
            self.assertTrue(entered.wait(2))
            start = time.monotonic()
            item.request_shutdown()
            self.assertLess(time.monotonic()-start, .1)
            self.assertFalse(item.shutdown_ready())
            release.set()
            self.assertTrue(pump(item.shutdown_ready))
        self.assertEqual(self.count(), 1)
        with closing(PendingFolders(item.queue_path)) as pending:
            self.assertTrue(pending.page(time.time()+61))

    def test_startup_recovers_commit_without_notice_or_librarian(self):
        folder = self.seed(self.watch()/'artist')
        item, bus = self.controller()
        done = []
        bus.subscribe('library_ingested', done.append)
        self.assertTrue(pump(lambda:bool(done)))
        self.assertEqual(self.count(), 1)
        self.assertTrue(item.queue_path.is_file())
        item.request_shutdown()
        self.assertTrue(pump(item.shutdown_ready))
        with closing(PendingFolders(item.queue_path)) as pending:
            self.assertEqual(pending.page(), [])

    def test_durable_pending_survives_shutdown_and_replays_idempotently(self):
        folder = self.seed(self.watch()/'artist')
        path = Path(self.service.db_path + '.cherry-ingest.db')
        with closing(PendingFolders(path)) as pending:
            pending.mark([str(folder)])
        item, _ = self.controller()
        self.assertTrue(pump(lambda:self.count() == 1))
        item.request_shutdown()
        self.assertTrue(pump(item.shutdown_ready))
        key = self.service.read('get_file_id', str(folder/'0.png'))
        again, _ = self.controller()
        self.assertTrue(pump(lambda:self.count() == 1))
        again.request_shutdown()
        self.assertTrue(pump(again.shutdown_ready))
        self.assertEqual(self.service.read('get_file_id', str(folder/'0.png')), key)

    def test_notice_during_pass_gets_a_second_pass(self):
        watch = self.watch()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []
        def scan(*args):
            calls.append(args[1])
            if len(calls) == 1:
                entered.set()
                release.wait(3)
            return 0
        with patch('core.cherry_ingest.reconcile', side_effect=scan):
            item, bus = self.controller()
            self.assertTrue(entered.wait(2))
            bus.publish('cherry_catalog_changed', str(watch))
            release.set()
            self.assertTrue(pump(lambda:len(calls) >= 2))
            item.request_shutdown()
            self.assertTrue(pump(item.shutdown_ready))
        self.assertEqual(calls[:2], [str(watch).replace('\\', '/'), str(watch)])

    def test_overflow_requests_reconciliation_and_gui_admission_is_nonblocking(self):
        watch = self.watch()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def scan(*args):
            entered.set()
            release.wait(3)
            return 0
        with patch('core.cherry_ingest.reconcile', side_effect=scan):
            item, bus = self.controller()
            self.assertTrue(entered.wait(2))
            start = time.monotonic()
            for _ in range(300):
                bus.publish('cherry_catalog_changed', str(watch))
            self.assertLess(time.monotonic() - start, .2)
            self.assertEqual(item._notices.qsize(), 128)
            self.assertTrue(item._rescan.is_set())
            item.request_shutdown()
            self.assertFalse(item.shutdown_ready())
            release.set()
            self.assertTrue(pump(item.shutdown_ready))
        with closing(PendingFolders(item.queue_path)) as pending:
            self.assertTrue(pending.page(time.time() + 61))
        self.assertFalse(item.notice(str(watch)))

    def test_one_bad_catalog_remains_pending_while_healthy_root_succeeds(self):
        bad = self.watch(self.root/'a-bad')
        (bad/'catalog.db').write_bytes(b'not sqlite')
        good = self.seed(self.watch(self.root/'b-good'))
        item, bus = self.controller()
        done = []
        bus.subscribe('library_ingested', done.append)
        self.assertTrue(pump(lambda:any(Path(row['folder']) == good for row in done)))
        item.request_shutdown()
        self.assertTrue(pump(item.shutdown_ready))
        with closing(PendingFolders(item.queue_path)) as pending:
            self.assertEqual([p for p, _ in pending.page(time.time()+61)], [str(bad).replace('\\', '/')])

    def test_cherry_view_announces_without_gui_probe_or_library_window(self):
        from modules.cherry.ui.view import CherryView
        bus = EventBus()
        notices = []
        bus.subscribe('cherry_catalog_changed', notices.append)
        fake = SimpleNamespace(context={'cherry_ingestor': object()}, _event_bus=bus)
        with patch('core.library_probe.is_watched', side_effect=AssertionError('GUI probe forbidden')):
            CherryView._announce_folder(fake, {'folder_path':'C:/private/artist'})
        self.assertEqual(notices, ['C:/private/artist'])


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main()
