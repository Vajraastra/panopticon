"""Paginación/reconciliación con DB y carpetas privadas; ninguna colección real."""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from core.database_manager import DatabaseManager
from core.inventory import InventoryStatus as Status
from core.inventory_candidates import InventoryCandidates, IdentityCache, pages, InventoryCancelled
from core.library_connections import open_reader
from core.library_inventory import page, InventoryReader
from core.library_results import ReadResultError
from core.library_service import LibraryError, close_library_services
from modules.librarian.logic import indexer


class PoisonConnection:
    def execute(self, *args):
        raise AssertionError('Invalid paging must not query SQL')


class CandidateStorage(unittest.TestCase):
    def test_unicode_and_newlines_roundtrip_in_bounded_blocks_and_delete_temp(self):
        with InventoryCandidates() as candidates:
            for n in range(530):
                candidates.add(n, 'C:/privado/ñ\n%02d.png' % n, (5, 2**100))
            blocks = list(candidates.blocks())
            self.assertEqual([len(block) for block in blocks], [256, 256, 18])
            self.assertEqual(blocks[0][0], [0, 'C:/privado/ñ\n00.png', 5, 2**100])
            self.assertEqual(sum(len(block) for block in candidates.blocks()), 530)
            stream = candidates.file
        self.assertTrue(stream.closed)

    def test_capacity_failure_keeps_confirmed_temp_records_but_closes_it(self):
        with InventoryCandidates() as candidates:
            with patch('core.inventory_candidates.MAX_CANDIDATE_BYTES', 50):
                candidates.add(1, 'C:/one.png', (1, 2))
                with self.assertRaisesRegex(ValueError, 'inventory_candidates_too_large'):
                    candidates.add(2, 'C:/a-much-longer-path-to-exceed-cap.png', (1, 2))
            self.assertEqual(candidates.count, 1)
            self.assertEqual(list(candidates.blocks()), [[[1, 'C:/one.png', 1, 2]]])
            stream = candidates.file
        self.assertTrue(stream.closed)

    def test_truncated_extra_and_invalid_temp_records_are_rejected(self):
        for kind in ['truncated', 'extra', 'invalid', 'oversized']:
            with self.subTest(kind=kind), InventoryCandidates() as candidates:
                candidates.add(1, 'C:/one.png', (1, 2))
                if kind == 'truncated':
                    candidates.file.truncate(candidates.bytes-1)
                elif kind == 'extra':
                    candidates.file.write(b'[]\n')
                elif kind == 'invalid':
                    candidates.file.seek(0)
                    candidates.file.write(b'[' + b' '* (candidates.bytes-2) + b'\n')
                else:
                    candidates.file.seek(0)
                    candidates.file.write(b'x'*65537+b'\n')
                with self.assertRaises((ValueError, UnicodeError)):
                    list(candidates.blocks())

    def test_write_failure_never_marks_temp_complete_and_close_on_exception(self):
        with InventoryCandidates() as candidates:
            stream = candidates.file
            with patch.object(stream, 'write', side_effect=OSError('private disk full')):
                with self.assertRaises(OSError):
                    candidates.add(1, 'C:/one.png', (1, 2))
            self.assertEqual((candidates.count, candidates.bytes), (0, 0))
        self.assertTrue(stream.closed)

    def test_valid_json_same_length_corruption_is_detected_before_acceptance(self):
        with InventoryCandidates() as candidates:
            candidates.add(1, 'C:/one.png', (1, 2))
            candidates.file.seek(1)
            candidates.file.write(b'2')
            with self.assertRaisesRegex(ValueError, 'invalid_inventory_candidates'):
                list(candidates.blocks())

    def test_parent_cache_is_bounded_and_reloads_evicted_parent(self):
        seen = []
        cache = IdentityCache(lambda path: (seen.append(path), len(seen)), capacity=4)
        before = cache.read('first')
        for n in range(10):
            cache.read(str(n))
            self.assertLessEqual(len(cache.values), 4)
        self.assertNotEqual(cache.read('first'), before)
        self.assertEqual(seen.count('first'), 2)

    def test_invalid_limits_ids_and_ranges_reject_before_sql(self):
        for kwargs in [dict(limit=-1), dict(limit=True), dict(limit=257), dict(after_id=True),
                       dict(upper_id=True), dict(after_id=0), dict(upper_id=1, after_id=2),
                       dict(upper_id=2**63), dict(after_id=-2**63-1, upper_id=1)]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ReadResultError):
                page(PoisonConnection(), 'C:/private', folder_like=lambda p: p, **kwargs)
        for folder in [None, '', 'x'*4097, 'C:/null\x00folder']:
            with self.assertRaises(ReadResultError):
                page(PoisonConnection(), folder, folder_like=lambda p: p)


class PagingSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root/'collection'
        self.folder.mkdir()
        self.db = DatabaseManager(self.root/'private.db')
        self.addCleanup(close_library_services)
        self.created = []
        original = tempfile.TemporaryFile
        def tracked(*args, **kwargs):
            stream = original(*args, **kwargs)
            self.created.append(stream)
            return stream
        self.tracker = patch('core.inventory_candidates.tempfile.TemporaryFile', tracked)
        self.tracker.start()
        self.addCleanup(self.tracker.stop)

    def sql(self, statement, args=()):
        with closing(sqlite3.connect(self.db.db_path)) as conn:
            rows = conn.execute(statement, args).fetchall()
            conn.commit()
            return rows

    def seed(self, count=530, folder=None):
        folder = folder or self.folder
        values = [dict(path=(folder/f'image-{n:04d}.png').as_posix(),
                       filename=f'image-{n:04d}.png', size=13, created=1) for n in range(count)]
        for start in range(0, count, 100):
            self.assertTrue(self.db.register_files_minimal(values[start:start+100]))
        return [row['path'] for row in values]

    def worker(self):
        return indexer.IndexerWorker(self.db, [str(self.folder)], deep_clean=True)

    def run_worker(self, worker=None):
        worker = worker or self.worker()
        finishes = []
        worker.finished_signal.connect(lambda: finishes.append(True))
        worker.run()
        self.assertEqual(finishes, [True])
        self.assertTrue(all(stream.closed for stream in self.created))
        self.assertEqual(self.db.service.metrics_snapshot()['reads_active'], 0)
        return worker

    def assert_online(self, count):
        self.assertEqual(self.db.count_files(), count)
        self.assertEqual(self.sql('SELECT count(*) FROM files WHERE is_offline=1'), [(0,)])

    def test_keyset_pages_have_finite_frontier_complete_ids_and_no_offset(self):
        paths = self.seed()
        first = self.db.get_inventory_page(str(self.folder))
        self.assertEqual(len(first['rows']), 256)
        self.assertFalse(first['done'])
        seen = list(first['rows'])
        after = first['after_id']
        while True:
            value = self.db.get_inventory_page(str(self.folder), after_id=after, upper_id=first['upper_id'])
            seen.extend(value['rows'])
            if value['done']:
                break
            after = value['after_id']
        self.assertEqual([row[1] for row in seen], paths)
        self.assertEqual(len({row[0] for row in seen}), 530)
        with closing(open_reader(self.db.db_path)) as conn:
            statements = []
            conn.set_trace_callback(statements.append)
            result = page(conn, str(self.folder), after_id=first['after_id'], upper_id=first['upper_id'],
                          folder_like=lambda folder: folder.replace('\\', '/')+'/%')
            self.assertEqual(len(result['rows']), 256)
        selects = [s for s in statements if s.startswith('SELECT')]
        self.assertEqual(len(selects), 1)
        self.assertNotIn('OFFSET', selects[0])

    def test_empty_and_offline_only_scope_do_not_infer_other_folders(self):
        values = self.seed(2)
        self.assertTrue(self.db.remove_files(values))
        empty = self.db.get_inventory_page(str(self.folder))
        self.assertEqual(empty, dict(rows=[], upper_id=None, after_id=None, done=True))
        self.assertEqual(self.db.get_inventory_page(str(self.root/'absent')), empty)

    def test_escaped_folder_scope_preserves_wildcard_names_and_excludes_siblings(self):
        special = self.folder/'100%_ñ'
        special.mkdir()
        expected = self.seed(3, special)
        sibling = self.folder/'100XXñ'
        sibling.mkdir()
        self.seed(4, sibling)
        self.assertEqual([row[1] for row in self.db.get_inventory_page(str(special))['rows']], expected)

    def test_signed_sqlite_ids_include_zero_and_extremes_without_sentinel_loss(self):
        with closing(sqlite3.connect(self.db.db_path)) as conn:
            conn.executemany('INSERT INTO files(id,path) VALUES (?,?)',
                ((value, (self.folder/f'{value}.png').as_posix()) for value in [-2**63, 0, 2**63-1]))
            conn.commit()
        rows = self.db.get_inventory_page(str(self.folder))
        self.assertEqual([row[0] for row in rows['rows']], [-2**63, 0, 2**63-1])
        self.assertTrue(rows['done'])

    def test_later_native_insert_and_offline_rows_are_excluded_by_frontier(self):
        self.seed(4)
        first = self.db.get_inventory_page(str(self.folder), limit=2)
        new = (self.folder/'later.png').as_posix()
        self.assertTrue(self.db.register_files_minimal([dict(path=new, filename='later.png', size=1, created=2)]))
        self.assertTrue(self.db.remove_files([first['rows'][0][1]]))
        resumed = self.db.get_inventory_page(str(self.folder), limit=2,
            after_id=first['after_id'], upper_id=first['upper_id'])
        self.assertTrue(resumed['done'])
        self.assertNotIn(new, [row[1] for row in resumed['rows']])
        self.assertEqual(len(resumed['rows']), 2)

    def test_reader_closes_between_pages_and_writer_can_commit(self):
        self.seed(4)
        readers = []
        def tracked(path):
            conn = open_reader(path)
            readers.append(conn)
            return conn
        with patch('core.library_service.open_reader', tracked):
            first = self.db.get_inventory_page(str(self.folder), limit=2)
            self.assertEqual(self.db.service.metrics_snapshot()['reads_active'], 0)
            self.assertTrue(self.db.update_file_rating(first['rows'][0][1], 5))
            second = self.db.get_inventory_page(str(self.folder), limit=2,
                after_id=first['after_id'], upper_id=first['upper_id'])
        self.assertTrue(second['done'])
        for conn in readers:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')

    def test_scoped_reader_reuses_connection_and_releases_wal_snapshot_per_page(self):
        self.seed()
        readers, statements = [], []
        def tracked(path):
            conn = open_reader(path)
            conn.set_trace_callback(statements.append)
            readers.append(conn)
            return conn
        with patch('core.library_service.open_reader', tracked):
            with self.db.inventory_reader() as reader:
                self.assertFalse(hasattr(reader, 'conn'))
                first = reader.get_inventory_page(str(self.folder))
                self.assertFalse(readers[0].in_transaction)
                self.assertEqual(self.db.service.metrics_snapshot()['reads_active'], 0)
                self.assertTrue(self.db.update_file_rating(first['rows'][0][1], 5))
                self.assertEqual(self.sql('PRAGMA wal_checkpoint(TRUNCATE)')[0][0], 0)
                second = reader.get_inventory_page(str(self.folder), after_id=first['after_id'], upper_id=first['upper_id'])
                self.assertEqual(len(second['rows']), 256)
                self.assertFalse(readers[0].in_transaction)
        self.assertEqual(len(readers), 1)
        self.assertEqual(statements.count('BEGIN'), 2)
        self.assertEqual(statements.count('ROLLBACK'), 2)
        with self.assertRaises(sqlite3.ProgrammingError):
            readers[0].execute('SELECT 1')

    def test_scoped_reader_rejects_other_threads_and_use_after_context(self):
        self.seed(1)
        with self.db.inventory_reader() as reader:
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(reader.get_inventory_page, str(self.folder))
                with self.assertRaises(LibraryError) as caught:
                    result.result(3)
            self.assertEqual(caught.exception.reason, 'inventory_reader_wrong_thread')
            self.assertEqual(len(reader.get_inventory_page(str(self.folder))['rows']), 1)
        with self.assertRaises(LibraryError) as caught:
            reader.get_inventory_page(str(self.folder))
        self.assertEqual(caught.exception.reason, 'inventory_reader_closed')

    def test_scoped_reader_failed_page_rolls_back_and_remains_usable(self):
        paths = self.seed(1)
        oversized = self.folder.as_posix()+'/'+'x'*4096
        readers = []
        def tracked(path):
            conn = open_reader(path)
            readers.append(conn)
            return conn
        with patch('core.library_service.open_reader', tracked), self.db.inventory_reader() as reader:
            self.sql('UPDATE files SET path=? WHERE path=?', (oversized, paths[0]))
            with self.assertRaises(LibraryError) as caught:
                reader.get_inventory_page(str(self.folder))
            self.assertEqual(caught.exception.reason, 'invalid_inventory_path')
            self.assertFalse(readers[0].in_transaction)
            self.sql('UPDATE files SET path=? WHERE path=?', (paths[0], oversized))
            self.assertEqual(reader.get_inventory_page(str(self.folder))['rows'][0][1], paths[0])
            self.assertFalse(readers[0].in_transaction)

    def test_scoped_reader_honors_service_close_and_precancel_before_open(self):
        event = threading.Event()
        event.set()
        with patch('core.library_service.open_reader', side_effect=AssertionError('SQL opened')):
            with self.assertRaises(LibraryError) as caught:
                with self.db.inventory_reader(cancel_event=event):
                    pass
        self.assertEqual(caught.exception.reason, 'cancelled')
        with self.db.inventory_reader() as reader:
            self.db.service.request_close()
            with self.assertRaises(LibraryError) as caught:
                reader.get_inventory_page(str(self.folder))
            self.assertEqual(caught.exception.reason, 'service_closed')

    def test_unguardable_historical_path_reports_failure_not_shortened_inventory(self):
        values = self.seed(2)
        self.sql('UPDATE files SET path=? WHERE path=?',
                 (self.folder.as_posix()+'/'+'x'*4096, values[0]))
        with self.assertRaises(LibraryError) as caught:
            self.db.get_inventory_page(str(self.folder))
        self.assertEqual(caught.exception.reason, 'invalid_inventory_path')
        self.assert_online(2)

    def test_precancelled_inventory_page_opens_no_reader(self):
        event = threading.Event()
        event.set()
        with patch('core.library_service.open_reader', side_effect=AssertionError('SQL opened')):
            with self.assertRaises(LibraryError) as caught:
                self.db.get_inventory_page(str(self.folder), cancel_event=event)
        self.assertEqual(caught.exception.reason, 'cancelled')

    def test_malformed_progress_or_short_done_page_aborts_consumer(self):
        values = self.seed(2)
        for field, invalid in [('after_id', True), ('done', False), ('upper_id', None),
                               ('rows', [(2, values[1]), (1, values[0])])]:
            original = self.db.get_inventory_page(str(self.folder))
            original[field] = invalid
            with self.subTest(field=field), patch.object(self.db, 'get_inventory_page', return_value=original):
                with self.assertRaises(ValueError):
                    list(pages(self.db, str(self.folder), lambda: False, threading.Event()))

    def test_cancelled_page_consumer_makes_no_db_call(self):
        with patch.object(self.db, 'get_inventory_page', side_effect=AssertionError('unexpected read')):
            with self.assertRaises(InventoryCancelled):
                list(pages(self.db, str(self.folder), lambda: True, threading.Event()))

    def test_worker_uses_pages_spool_and_preserves_curated_ids_and_cherry_bytes(self):
        paths = self.seed()
        watched = paths[0]
        before = self.db.get_file_id(watched)
        self.assertTrue(self.db.update_file_rating(watched, 5))
        self.assertTrue(self.db.add_tag_to_file(watched, 'curated'))
        receipt = self.folder/'catalog-receipt.bin'
        receipt.write_bytes(b'Cherry receipts unchanged')
        schema = self.sql('SELECT name,sql FROM sqlite_master ORDER BY name')
        with patch.object(self.db, 'get_known_files_in_folder', side_effect=AssertionError('unbounded inventory')):
            worker = self.run_worker()
        self.assertEqual((worker.outcome, worker.archived_count), (Status.COMPLETE, 530))
        self.assertEqual(self.db.count_files(), 0)
        current = self.db.get_copy_version_state(before)
        self.assertEqual(current['state']['file']['rating'], 5)
        self.assertEqual(current['state']['file']['is_offline'], 1)
        self.assertEqual(self.sql('SELECT t.name FROM tags t JOIN file_tags ft ON t.id=ft.tag_id WHERE ft.file_id=?', (before,)), [('curated',)])
        self.assertEqual(receipt.read_bytes(), b'Cherry receipts unchanged')
        self.assertEqual(self.sql('SELECT name,sql FROM sqlite_master ORDER BY name'), schema)

    def test_later_page_sql_failure_archives_nothing_and_closes_temp(self):
        self.seed()
        read, calls = self.db.get_inventory_page, []
        def fail(folder, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise LibraryError('get_inventory_page', 'fixture read failure')
            return read(folder, **kwargs)
        with patch.object(InventoryReader, 'get_inventory_page', lambda reader, *a, **k: fail(*a, **k)):
            worker = self.run_worker()
        self.assertEqual((worker.outcome, worker.archived_count), (Status.ERROR, 0))
        self.assert_online(530)

    def test_cancel_between_pages_archives_nothing(self):
        self.seed()
        worker, read, calls = self.worker(), self.db.get_inventory_page, []
        def cancel(folder, **kwargs):
            value = read(folder, **kwargs)
            calls.append(value)
            if len(calls) == 2:
                worker.stop()
            return value
        with patch.object(InventoryReader, 'get_inventory_page', lambda reader, *a, **k: cancel(*a, **k)):
            self.run_worker(worker)
        self.assertEqual((worker.outcome, worker.archived_count), (Status.CANCELLED, 0))
        self.assertTrue(worker._inventory_cancel.is_set())
        self.assert_online(530)

    def test_stop_during_actual_sql_interrupts_read_and_archives_nothing(self):
        self.seed()
        worker, readers = self.worker(), []
        def tracked(path):
            conn = open_reader(path)
            readers.append(conn)
            def trace(statement):
                if statement.startswith('SELECT id, path FROM files'):
                    worker.stop()
            conn.set_trace_callback(trace)
            return conn
        with patch('core.library_service.open_reader', tracked):
            self.run_worker(worker)
        self.assertEqual((worker.outcome, worker.archived_count), (Status.CANCELLED, 0))
        self.assert_online(530)
        self.assertEqual(self.db.service.metrics_snapshot()['reads_cancelled'], 1)
        for conn in readers:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')

    def test_changed_frontier_in_later_page_is_rejected_without_archiving(self):
        self.seed()
        read, calls = self.db.get_inventory_page, []
        def changed(folder, **kwargs):
            value = read(folder, **kwargs)
            calls.append(value)
            if len(calls) == 2:
                value['upper_id'] += 1
            return value
        with patch.object(InventoryReader, 'get_inventory_page', lambda reader, *a, **k: changed(*a, **k)):
            worker = self.run_worker()
        self.assertEqual((worker.outcome, worker.archived_count), (Status.ERROR, 0))
        self.assert_online(530)

    def test_late_new_copy_id_at_same_path_is_not_archived_before_review(self):
        paths = self.seed(1)
        before = self.db.get_file_id(paths[0])
        read, replacements = self.db.get_duplicate_snapshot, []
        def replaced(block):
            old = self.db.get_copy_version_state(before)
            self.assertTrue(self.db.make_copy_online(before, expected_fingerprint=old['fingerprint'],
                digest='a'*64, observation=dict(filename='image-0000.png', size=2, created=2)))
            replacements.append(self.db.get_file_id(paths[0]))
            return read(block)
        with patch.object(self.db, 'get_duplicate_snapshot', replaced):
            worker = self.run_worker()
        self.assertEqual((worker.outcome, worker.archived_count), (Status.ERROR, 0))
        self.assertNotEqual(replacements[0], before)
        self.assertEqual(self.db.get_file_id(paths[0]), replacements[0])
        self.assertEqual(self.db.get_copy_version_state(before)['state']['file']['is_offline'], 1)

    def test_new_native_id_after_first_page_remains_online(self):
        self.seed()
        later = (self.folder/'late-cherry.png').as_posix()
        read, calls = self.db.get_inventory_page, []
        def arrived(folder, **kwargs):
            value = read(folder, **kwargs)
            calls.append(value)
            if len(calls) == 1:
                self.assertTrue(self.db.register_files_minimal([
                    dict(path=later, filename='late-cherry.png', size=7, created=2,
                         origin_url='https://example.invalid/receipt', hash_original='b'*64)]))
            return value
        with patch.object(InventoryReader, 'get_inventory_page', lambda reader, *a, **k: arrived(*a, **k)):
            worker = self.run_worker()
        self.assertEqual((worker.outcome, worker.archived_count), (Status.COMPLETE, 530))
        copy_id = self.db.get_file_id(later)
        self.assertIsNotNone(copy_id)
        state = self.db.get_copy_version_state(copy_id)['state']['file']
        self.assertEqual((state['origin_url'], state['hash_original'], state['is_offline']),
                         ('https://example.invalid/receipt', 'b'*64, 0))

    def test_parent_replacement_after_early_page_blocks_all_archival(self):
        child = self.folder/'child'
        child.mkdir()
        self.seed(1, child)
        self.seed()
        read, calls = self.db.get_inventory_page, []
        def changed(folder, **kwargs):
            value = read(folder, **kwargs)
            calls.append(value)
            if len(calls) == 2:
                child.rename(self.root/'old-child')
                child.mkdir()
            return value
        with patch.object(InventoryReader, 'get_inventory_page', lambda reader, *a, **k: changed(*a, **k)):
            worker = self.run_worker()
        self.assertEqual((worker.outcome, worker.archived_count), (Status.UNAVAILABLE, 0))
        self.assert_online(531)

    def test_spool_capacity_failure_and_corruption_do_not_archive_a_prefix(self):
        self.seed()
        with patch('core.inventory_candidates.MAX_CANDIDATE_BYTES', 50):
            worker = self.run_worker()
        self.assertEqual((worker.outcome, worker.archived_count), (Status.ERROR, 0))
        self.assert_online(530)
        original = InventoryCandidates.blocks
        def corrupted(storage):
            storage.file.truncate(storage.bytes-1)
            yield from original(storage)
        with patch.object(InventoryCandidates, 'blocks', corrupted):
            worker = self.run_worker()
        self.assertEqual((worker.outcome, worker.archived_count), (Status.ERROR, 0))
        self.assert_online(530)

    def test_temp_creation_failure_is_reported_without_archiving(self):
        self.seed(1)
        with patch('core.inventory_candidates.tempfile.TemporaryFile', side_effect=OSError('private no space')):
            worker = self.run_worker()
        self.assertEqual((worker.outcome, worker.archived_count), (Status.UNAVAILABLE, 0))
        self.assert_online(1)

    def test_files_present_even_when_omitted_from_discovery_are_never_archived(self):
        values = self.seed(3)
        Path(values[0]).write_bytes(b'physical but uncatalogued')
        worker = self.worker()
        discover = worker._discover_tree
        def omitted(*args):
            value = discover(*args)
            value.files.clear()
            return value
        with patch.object(worker, '_discover_tree', omitted):
            self.run_worker(worker)
        self.assertEqual((worker.outcome, worker.archived_count), (Status.COMPLETE, 2))
        self.assertIsNotNone(self.db.get_file_id(values[0]))
        self.assertEqual(Path(values[0]).read_bytes(), b'physical but uncatalogued')


if __name__ == '__main__':
    unittest.main()
