"""Private disk inventory, lazy enumeration and RO Cherry page contracts."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.inventory_spool import InventorySpool
from core.inventory_walk import entries
from core.inventory_candidates import InventoryCancelled
from core.inventory import InventoryResult, InventoryStatus
from core.catalog_reader import read_image_files
from core.catalog_inventory_pages import pages
from core.library_service import LibraryService
from core.database_manager import DatabaseManager
from modules.librarian.logic.indexer import IndexerWorker


def row(i=0):
    return dict(path=f'C:/Private/Árbol/Image{i}.png', filename=f'Image{i}.png', size=7, created=1,
                origin_url='private', hash_original='a'*64)


class Spool(unittest.TestCase):
    def test_305_records_pages_and_unicode_membership_without_full_path_set(self):
        with InventorySpool() as spool:
            directory = Path(spool._dir.name)
            for i in range(305):
                spool.append(row(i))
            spool.seal()
            self.assertEqual([len(spool[i:i+100]) for i in range(0, len(spool), 100)], [100, 100, 100, 5])
            self.assertEqual(spool[104], row(104))
            self.assertIs(InventoryResult(files=spool).paths, spool)
            self.assertEqual(spool.contains_many([row(104)['path'], 'C:/missing.png']), {row(104)['path']})
            self.assertNotIn(row(104)['path'].lower(), spool)
            self.assertEqual(len(list(spool)), 305)
        self.assertFalse(directory.exists())
        with self.assertRaises(ValueError):
            spool.path_membership()

    def test_duplicate_observations_remain_ordered_and_original_metadata_preserved(self):
        with InventorySpool() as spool:
            values = [row(), dict(row(), artist='second', origin_url=None)]
            spool.extend(values)
            spool.seal()
            self.assertEqual(spool[0:2], values)
            self.assertEqual(spool.contains_many([row()['path']]), {row()['path']})

    def test_invalid_and_nonfinite_record_makes_spool_sticky_failed(self):
        for invalid in (dict(row(), created=float('nan')), dict(row(), size=-1),
                        dict(row(), path='x\0bad'), dict(row(), artist=[])):
            with self.subTest(invalid=invalid), InventorySpool() as spool:
                spool.append(row(1))
                with self.assertRaises(ValueError):
                    spool.append(invalid)
                with self.assertRaises(ValueError):
                    spool.seal()

    def test_capacity_failure_never_seals_or_reads_approved_prefix(self):
        with InventorySpool() as spool, patch('core.inventory_spool.MAX_STREAM_BYTES', 1):
            with self.assertRaisesRegex(ValueError, 'spool_failed'):
                spool.append(row())
            with self.assertRaises(ValueError):
                spool.seal()
            with self.assertRaises(ValueError):
                spool[0:1]

    def test_index_sql_failure_does_not_seal_written_stream(self):
        with InventorySpool() as spool:
            spool._conn.execute('DROP TABLE records')
            with self.assertRaisesRegex(ValueError, 'spool_failed'):
                spool.append(row())
            with self.assertRaises(ValueError):
                spool.seal()

    def test_truncated_extra_and_same_length_payload_corruption_rejected(self):
        for mode in ('truncated', 'extra', 'substituted'):
            with self.subTest(mode=mode), InventorySpool() as spool:
                spool.append(row())
                if mode == 'truncated':
                    spool._file.truncate(1)
                elif mode == 'extra':
                    spool._file.write(b'\n')
                else:
                    spool._file.seek(0)
                    raw = spool._file.read()
                    spool._file.seek(0)
                    spool._file.write(raw.replace(b'private', b'changed'))
                with self.assertRaises(ValueError):
                    spool.seal()

    def test_corrupt_index_is_detected_by_seal(self):
        with InventorySpool() as spool:
            spool.append(row())
            spool._conn.execute("UPDATE records SET path='C:/changed.png'")
            with self.assertRaises(ValueError):
                spool.seal()

    def test_postseal_record_corruption_rejected_before_delivery(self):
        with InventorySpool() as spool:
            spool.append(row())
            spool.seal()
            spool._file.seek(0)
            spool._file.write(b'x')
            with self.assertRaises(ValueError):
                spool[0:1]
            with self.assertRaises(ValueError):
                spool.verify()

    def test_seal_prevents_writes_and_oversized_pages(self):
        with InventorySpool() as spool:
            spool.append(row())
            spool.seal()
            with self.assertRaises(ValueError):
                spool.append(row(1))
            with self.assertRaises(ValueError):
                spool[0:101]
            with self.assertRaises(ValueError):
                spool.contains_many(['C:/a.png']*257)
            with self.assertRaises(sqlite3.OperationalError):
                spool._conn.execute('DELETE FROM records')

    def test_cancel_during_verification_and_metadata_count_after_stop(self):
        event = threading.Event()
        with InventorySpool(cancel_event=event) as spool:
            spool.append(row())
            event.set()
            self.assertEqual(len(spool), 1)
            with self.assertRaises(InventoryCancelled):
                spool.seal()

    def test_failure_and_repeated_close_remove_all_own_resources(self):
        spool = InventorySpool()
        directory, file = Path(spool._dir.name), spool._file
        try:
            with spool:
                raise RuntimeError('private failure')
        except RuntimeError:
            pass
        spool.close()
        self.assertFalse(directory.exists())
        self.assertTrue(file.closed)

    def test_error_messages_and_merges_have_fixed_memory_bounds(self):
        result = InventoryResult()
        for i in range(1000):
            result.fail(InventoryStatus.ERROR, 'x'*10000)
            other = InventoryResult(issues=['y'*10000]*32, status=InventoryStatus.ERROR)
            result.merge(other)
        self.assertEqual(len(result.issues), 32)
        self.assertLessEqual(max(map(len, result.issues)), 4096)


class Traversal(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_nested_catalog_is_emitted_once_without_enumerating_its_images(self):
        (self.root/'one.png').write_bytes(b'private')
        child = self.root/'artist'
        child.mkdir()
        (child/'catalog.db').write_bytes(b'private')
        (child/'two.png').write_bytes(b'private')
        errors = []
        values = list(entries(str(self.root), ('.png',), cancelled=lambda:False, onerror=errors.append,
                              is_catalog=lambda p:(Path(p)/'catalog.db').exists()))
        self.assertEqual(sorted(v[0] for v in values), ['catalog', 'image'])
        self.assertEqual(errors, [])

    def test_cancel_closes_all_scandir_handles(self):
        child = self.root/'child'
        child.mkdir()
        (child/'one.png').write_bytes(b'private')
        event, opened = threading.Event(), []
        original = os.scandir
        class Iterator:
            def __init__(self, path):
                self.raw, self.closed = original(path), False
                opened.append(self)
            def __next__(self):
                return next(self.raw)
            def close(self):
                self.closed = True
                self.raw.close()
        with patch('core.inventory_walk.os.scandir', side_effect=Iterator):
            iterator = entries(str(self.root), ('.png',), cancelled=event.is_set,
                               onerror=lambda exc:None, is_catalog=lambda p:False)
            next(iterator)
            event.set()
            with self.assertRaises(InventoryCancelled):
                next(iterator)
        self.assertEqual(len(opened), 2)
        self.assertTrue(all(i.closed for i in opened))

    def test_depth_limit_reports_incomplete_instead_of_an_approved_prefix(self):
        child = self.root/'child'
        child.mkdir()
        (child/'one.png').write_bytes(b'private')
        errors = []
        with patch('core.inventory_walk.MAX_OPEN_DIRECTORIES', 1):
            values = list(entries(str(self.root), ('.png',), cancelled=lambda:False,
                                  onerror=errors.append, is_catalog=lambda p:False))
        self.assertEqual(values, [])
        self.assertIn('depth_exceeded', str(errors[0]))


class Catalog(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'catalog.db'

    def seed(self, count):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE files(hash TEXT PRIMARY KEY,filename TEXT,url_source TEXT,date_added REAL,file_size INTEGER)')
            conn.executemany('INSERT INTO files VALUES(?,?,?,?,?)',
                ((f'hash{i}', f'{i}.png', 'private', 1, 7) for i in range(count)))
            conn.commit()
        for i in range(count):
            (self.root/f'{i}.png').write_bytes(b'private')

    def test_305_source_rows_stream_without_list_and_leave_catalog_unchanged(self):
        self.seed(305)
        before = hashlib.sha256(self.path.read_bytes()).digest()
        received = []
        result = read_image_files(self.root, on_file=received.append)
        self.assertTrue(result.complete)
        self.assertEqual(result.files, [])
        self.assertEqual(len(received), 305)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), before)
        self.assertEqual(len(read_image_files(self.root).files), 305)

    def test_snapshots_end_before_callback_and_late_rows_stay_outside_frontier(self):
        self.seed(260)
        original = sqlite3.connect
        readers, begins = [], []
        def connect(*args, **kwargs):
            conn = original(*args, **kwargs)
            readers.append(conn)
            conn.set_trace_callback(lambda sql:begins.append(sql) if sql == 'BEGIN' else None)
            return conn
        received = []
        def consume(entry):
            self.assertFalse(readers[0].in_transaction)
            if not received:
                with closing(original(self.path)) as writer:
                    writer.execute("INSERT INTO files VALUES('late','late.png',NULL,1,7)")
                    writer.commit()
                    self.assertEqual(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0)
                (self.root/'late.png').write_bytes(b'private')
            received.append(entry)
        with patch('core.catalog_reader.sqlite3.connect', side_effect=connect):
            result = read_image_files(self.root, on_file=consume)
        self.assertTrue(result.complete)
        self.assertEqual(len(received), 260)
        self.assertEqual(len(begins), 3)
        with self.assertRaises(sqlite3.ProgrammingError):
            readers[0].execute('SELECT 1')

    def test_cancel_mid_page_preserves_prefix_and_releases_handle(self):
        self.seed(260)
        event, received = threading.Event(), []
        def consume(entry):
            received.append(entry)
            event.set()
        result = read_image_files(self.root, cancelled=event.is_set, on_file=consume)
        self.assertEqual(result.status, InventoryStatus.CANCELLED)
        self.assertEqual(len(received), 1)
        self.path.rename(self.root/'closed.db')

    def test_signed_rowids_zero_and_negative_are_included(self):
        self.seed(0)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executemany('INSERT INTO files(rowid,hash,filename) VALUES(?,?,?)',
                             ((i, str(i), f'{i}.png') for i in (-2**63, 0, 2**63-1)))
            conn.commit()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            values = [r['_inventory_id'] for block in pages(conn, lambda:False) for r in block]
            self.assertFalse(conn.in_transaction)
        self.assertEqual(values, [-2**63, 0, 2**63-1])

    def test_sink_storage_failure_marks_error_without_cherry_writes(self):
        self.seed(3)
        before = self.path.read_bytes()
        def consume(entry):
            raise OSError('private disk full')
        result = read_image_files(self.root, on_file=consume)
        self.assertEqual(result.status, InventoryStatus.ERROR)
        self.assertEqual(result.files, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_oversized_source_values_are_rejected_without_delivering_truncated_metadata(self):
        self.seed(1)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('UPDATE files SET url_source=?', ('x'*20000,))
            conn.commit()
        received = []
        result = read_image_files(self.root, on_file=received.append)
        self.assertEqual(result.status, InventoryStatus.ERROR)
        self.assertEqual(received, [])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(len(conn.execute('SELECT url_source FROM files').fetchone()[0]), 20000)


class Indexer(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root/'images'
        self.folder.mkdir()
        self.service = LibraryService(self.root/'private.db')
        self.addCleanup(self.service.close)
        self.db = DatabaseManager(service=self.service)

    def worker(self):
        return IndexerWorker(self.db, [str(self.folder)], deep_clean=True)

    def test_actual_inventory_is_spooled_and_membership_is_queried_by_page(self):
        for i in range(205):
            (self.folder/f'{i}.png').write_bytes(b'private')
        worker = self.worker()
        seen, paths = [], []
        original = worker._discover_tree
        def discover(*args):
            result = original(*args)
            self.assertIsInstance(result.files, InventorySpool)
            seen.append(result.files)
            paths.append(Path(result.files._dir.name))
            return result
        sizes = []
        original_membership = InventorySpool.contains_many
        def membership(spool, values):
            block = list(values)
            sizes.append(len(block))
            return original_membership(spool, block)
        with patch.object(worker, '_discover_tree', side_effect=discover), patch.object(
                InventorySpool, 'contains_many', membership):
            worker.run()
        self.assertEqual(worker.outcome, InventoryStatus.COMPLETE)
        self.assertEqual(self.db.count_files(), 205)
        self.assertLessEqual(max(sizes), 256)
        self.assertTrue(all(not p.exists() for p in paths))
        self.assertTrue(all(s._closed for s in seen))

    def test_storage_cap_blocks_registration_and_archival_but_keeps_curation(self):
        missing = self.folder/'missing.png'
        self.db.register_files_minimal([dict(path=missing.as_posix(), filename=missing.name, size=1, created=1)])
        self.db.update_file_rating(missing.as_posix(), 5)
        (self.folder/'new.png').write_bytes(b'private')
        worker = self.worker()
        with patch('core.inventory_spool.MAX_STREAM_BYTES', 1):
            worker.run()
        self.assertEqual(worker.outcome, InventoryStatus.ERROR)
        self.assertEqual(self.db.count_files(), 1)
        self.assertEqual(self.db.get_file_rating(missing.as_posix()), 5)
        self.assertEqual(worker.archived_count, 0)

    def test_postregistration_corruption_suppresses_cleanup_with_confirmed_rows_kept(self):
        missing = self.folder/'missing.png'
        self.db.register_files_minimal([dict(path=missing.as_posix(), filename=missing.name, size=1, created=1)])
        (self.folder/'new.png').write_bytes(b'private')
        worker = self.worker()
        original = self.db.register_files_minimal
        def register(*args, **kwargs):
            result = original(*args, **kwargs)
            worker._inventory_spool._file.seek(0)
            worker._inventory_spool._file.write(b'x')
            return result
        with patch.object(self.db, 'register_files_minimal', side_effect=register):
            worker.run()
        self.assertEqual(worker.outcome, InventoryStatus.ERROR)
        self.assertEqual(self.db.count_files(), 2)
        self.assertIsNotNone(self.db.get_file_id(missing.as_posix()))
        self.assertEqual(worker.archived_count, 0)


if __name__ == '__main__':
    unittest.main()
