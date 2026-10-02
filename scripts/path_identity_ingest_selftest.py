"""Private Windows identity ingestion: atomic aliases, FIFO and retained history."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
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
from core.database_manager import DatabaseManager
from core.inventory import InventoryStatus
from core.library_batch import BatchCancelled
from core.library_repository import LibraryRepository
from core.library_service import LibraryService, LibraryError
from core.path_identity import PathIdentity, observe_path
from modules.librarian.logic.indexer import IndexerWorker


class Ingest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = LibraryService(self.root/'private.db', capacity=1)
        self.addCleanup(self.service.close)
        self.db = DatabaseManager(service=self.service)
        self.event = threading.Event()

    def image(self, name='One.png'):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'private')
        return path

    def row(self, path, **extra):
        return dict(path=str(path), filename=Path(path).name, size=7, created=1, **extra)

    def seed(self, path, **extra):
        self.service.submit('register_files_minimal', [self.row(path, **extra)]).result(3)
        return self.service.read('get_file_id', str(path))

    def ingest(self, rows):
        return self.db.register_files_minimal(rows, cancel_event=self.event)

    def sql(self, query):
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            return conn.execute(query).fetchall()

    def test_deep_indexing_renamed_parent_keeps_curated_copy_online(self):
        path=self.image('Árbol/One.png')
        key=self.seed(path)
        self.service.submit('edit_curation',str(path),rating=5,add_tags=['keep']).result(3)
        self.service.backfill_path_identity()
        folder=self.root/'árbol'
        path.parent.rename(folder)
        worker=IndexerWorker(self.db,[str(folder)],deep_clean=True)
        worker.run()
        self.assertEqual(worker.outcome,InventoryStatus.COMPLETE)
        self.assertEqual(self.sql('SELECT id,rating,is_offline FROM files'),[(key,5,0)])
        self.assertEqual(worker.archived_count,0)
        self.assertEqual(self.service.resolve_path_identity(str(folder/path.name))['file_id'],key)

    def test_owned_old_registry_repairs_names_without_implicit_index_migration(self):
        path=self.image('Árbol/One.png')
        key=self.seed(path)
        self.service.backfill_path_identity()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('DROP INDEX idx_path_identity_physical')
            conn.commit()
        backups=set(self.root.glob('private.db.pre-path-identity-*.bak'))
        path.parent.rename(self.root/'árbol')
        self.ingest([self.row(self.root/'árbol'/path.name)])
        self.assertEqual(self.sql('SELECT id FROM files'),[(key,)])
        self.assertEqual(self.sql("SELECT name FROM sqlite_master WHERE name='idx_path_identity_physical'"),[])
        self.assertEqual(set(self.root.glob('private.db.pre-path-identity-*.bak')),backups)

    def test_case_renamed_parent_preserves_curated_id_and_refreshes_actual_name(self):
        path = self.image('Árbol/One.png')
        key = self.seed(path, hash_original='saved', origin_url='documented', artist='curated')
        self.service.submit('edit_curation', str(path), rating=5, add_tags=['keep']).result(3)
        self.service.submit('update_file_metadata', str(path), dict(model='model', positive='prompt')).result(3)
        self.service.backfill_path_identity()
        current = self.root/'árbol'/path.name
        path.parent.rename(current.parent)
        # Metadata may change between preparation and rename: inode hints do
        # not require a stale saved mtime/size to equal the current file.
        current.write_bytes(b'new current private metadata')
        self.ingest([self.row(current, hash_original='raw', origin_url='raw', artist='raw')])
        self.assertEqual(self.sql('SELECT id,path,rating,hash_original,origin_url,artist,meta_model,meta_positive FROM files'),
            [(key, path.as_posix(), 5, 'saved', 'documented', 'curated', 'model', 'prompt')])
        self.assertEqual(self.service.read('get_tags_for_file', str(path)), ['keep'])
        self.assertEqual(self.service.resolve_path_identity(str(current))['file_id'], key)
        self.assertEqual(self.service.read('check_fts')['stale'], 0)

    def test_existing_hardlink_does_not_donate_curation_to_new_name(self):
        path = self.image()
        key = self.seed(path)
        self.service.submit('edit_curation', str(path), rating=5, add_tags=['keep']).result(3)
        self.service.backfill_path_identity()
        link = self.root/'Other.png'
        os.link(path, link)
        self.ingest([self.row(link)])
        other = self.service.resolve_path_identity(str(link))['file_id']
        self.assertNotEqual(key, other)
        self.assertEqual(self.sql('SELECT id,rating FROM files ORDER BY id'), [(key,5),(other,0)])
        self.assertEqual(self.service.read('get_tags_for_file', str(link)), [])

    def test_external_move_without_resolvable_saved_name_requires_review(self):
        path = self.image()
        key = self.seed(path)
        self.service.submit('edit_curation', str(path), rating=5).result(3)
        self.service.backfill_path_identity()
        renamed = self.root/'Renamed.png'
        path.rename(renamed)
        with self.assertRaisesRegex(ValueError, 'physical_state_changed'):
            self.ingest([self.row(renamed),self.row(self.image('new.png'))])
        self.assertEqual(self.sql('SELECT id,path,rating FROM files'), [(key,path.as_posix(),5)])

    def test_stale_duplicate_aliases_are_reviewed_without_merging_ids(self):
        path = self.image('Árbol/One.png')
        first = self.seed(path)
        second = self.seed(self.root/'ÁRBOL'/'ONE.PNG')
        self.service.backfill_path_identity()
        path.parent.rename(self.root/'árbol')
        with self.assertRaisesRegex(ValueError, 'review_required'):
            self.ingest([self.row(self.root/'árbol'/'One.png')])
        self.assertEqual(self.sql('SELECT id FROM files ORDER BY id'), [(first,),(second,)])

    def test_case_sensitive_same_inode_names_remain_distinct_with_existing_evidence(self):
        a,b=(self.root/'Name.png').as_posix(),(self.root/'name.png').as_posix()
        physical=(1,2,3,0,7,0,0)
        self.seed(a)
        def observation(path, **kwargs):
            return PathIdentity(path=path,status='resolved',canonical=path,kind='file',physical=physical,reason=None)
        with patch('core.library_path_identity.observe_path',side_effect=observation):
            self.service.backfill_path_identity()
        with patch('core.library_path_ingest.observe_path',side_effect=observation):
            self.ingest([self.row(b)])
        self.assertEqual(self.sql('SELECT path FROM files ORDER BY id'),[(a,),(b,)])

    def test_saved_name_change_after_candidate_probe_rejects_whole_batch(self):
        path=self.image('Árbol/One.png')
        self.seed(path)
        self.service.backfill_path_identity()
        path.parent.rename(self.root/'árbol')
        seen=0
        def probe(name,**kwargs):
            nonlocal seen
            result=observe_path(name,**kwargs)
            if name == path.as_posix():
                seen+=1
                if seen==2:
                    path.rename(path.with_name('Changed.png'))
                    return observe_path(name,**kwargs)
            return result
        with patch('core.library_path_ingest.observe_path',side_effect=probe):
            with self.assertRaisesRegex(ValueError,'physical_state_changed'):
                self.ingest([self.row(self.root/'árbol'/'One.png')])
        self.assertEqual(self.service.read('count_files'),1)

    def test_physical_candidate_overflow_does_not_approve_a_prefix(self):
        path=self.image()
        self.seed(path)
        self.service.backfill_path_identity()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            physical=conn.execute('SELECT physical FROM path_identity_records').fetchone()[0]
            for i in range(257):
                name=(self.root/f'alias{i}.png').as_posix()
                key=conn.execute('INSERT INTO files(path,filename) VALUES(?,?)',(name,f'alias{i}.png')).lastrowid
                conn.execute("INSERT INTO path_identity_records VALUES(?,?,0,'resolved',?,?,NULL)",(key,name,name,physical))
            conn.commit()
        with self.assertRaisesRegex(ValueError,'review_required'):
            self.ingest([self.row(path)])
        self.assertEqual(self.service.read('count_files'),258)

    def test_unicode_alias_reuses_curated_id_name_hash_origin_and_generation(self):
        path = self.image('Árbol/One.png')
        file_id = self.seed(path, hash_original='saved', origin_url='documented', artist='curated')
        self.service.submit('edit_curation', str(path), rating=5, add_tags=['keep']).result(3)
        self.service.submit('update_file_metadata', str(path), dict(model='model', positive='prompt')).result(3)
        self.service.backfill_path_identity()
        alias = self.root/'árbol'/'ONE.PNG'
        self.assertTrue(self.ingest([self.row(alias, hash_original='raw', origin_url='raw', artist='raw')]))
        self.assertEqual(self.sql('SELECT id,path,rating,hash_original,origin_url,artist,meta_model,meta_positive FROM files'),
            [(file_id, path.as_posix(), 5, 'saved', 'documented', 'curated', 'model', 'prompt')])
        self.assertEqual(self.service.read('get_tags_for_file', path.as_posix()), ['keep'])
        self.assertEqual(self.service.resolve_path_identity(str(alias))['file_id'], file_id)
        self.assertEqual(self.service.read('check_fts')['stale'], 0)

    def test_new_file_and_its_identity_commit_together_multiple_aliases_one_id(self):
        self.service.backfill_path_identity()
        path = self.image()
        self.ingest([self.row(path, origin_url='first'), self.row(self.root/'ONE.PNG', origin_url='second')])
        self.assertEqual(self.sql('SELECT path,origin_url FROM files'), [(path.as_posix(), 'first')])
        self.assertEqual(self.service.read('get_path_identity_status')['pending'], 0)
        self.assertEqual(self.service.resolve_path_identity(str(path))['status'], 'unique')

    def test_legacy_205_atomic_and_worker_unprepared_stays_legacy(self):
        rows = [self.row(self.root/f'{i}.png') for i in range(205)]
        self.assertTrue(self.db.register_files_minimal(rows))
        with patch('core.library_path_ingest.observe_path', side_effect=AssertionError('implicit migration')):
            self.ingest([self.row(self.root/'alias.png')])
        self.assertEqual(self.service.read('count_files'), 206)
        self.assertFalse(self.service.read('get_path_identity_status')['prepared'])

    def test_incomplete_or_unavailable_global_evidence_blocks_whole_batch(self):
        path = self.image()
        self.seed(path)
        self.service.submit('prepare_path_identity').result(3)
        other = self.image('new.png')
        with self.assertRaisesRegex(ValueError, 'review_required'):
            self.ingest([self.row(other)])
        path.unlink()
        self.service.backfill_path_identity()
        with self.assertRaisesRegex(ValueError, 'review_required'):
            self.ingest([self.row(other)])
        self.assertEqual(self.service.read('count_files'), 1)

    def test_ambiguous_existing_aliases_are_never_selected_or_merged(self):
        path = self.image()
        self.seed(path)
        alias = self.root/'ONE.PNG'
        self.seed(alias)
        self.service.submit('edit_curation', str(path), rating=5).result(3)
        self.service.backfill_path_identity()
        before = self.sql('SELECT id,path,rating FROM files ORDER BY id')
        with self.assertRaisesRegex(ValueError, 'review_required'):
            self.ingest([self.row(alias), self.row(self.image('new.png'))])
        self.assertEqual(self.sql('SELECT id,path,rating FROM files ORDER BY id'), before)

    def test_offline_exact_history_is_retained_and_not_reactivated(self):
        path = self.image()
        file_id = self.seed(path)
        self.service.submit('edit_curation', str(path), rating=4).result(3)
        self.service.submit('remove_files', [str(path)]).result(3)
        self.service.backfill_path_identity()
        self.ingest([self.row(path)])
        self.assertEqual(self.sql('SELECT id,rating,is_offline FROM files'), [(file_id, 4, 1)])
        self.assertEqual(self.service.read('get_path_identity_status')['pending'], 0)

    def test_hardlinks_remain_distinct_named_copies(self):
        path = self.image()
        link = self.root/'Other.png'
        os.link(path, link)
        self.service.backfill_path_identity()
        self.ingest([self.row(path), self.row(link)])
        self.assertEqual(self.service.read('count_files'), 2)
        self.assertNotEqual(self.service.resolve_path_identity(str(path))['file_id'],
                            self.service.resolve_path_identity(str(link))['file_id'])

    def test_sensitive_directory_contract_with_mocked_adapter_keeps_two_names(self):
        # No Windows folder flags are changed by this test.
        self.service.backfill_path_identity()
        a, b = (self.root/'Name.png').as_posix(), (self.root/'name.png').as_posix()
        def observation(path, **kwargs):
            return PathIdentity(path=path, status='resolved', canonical=path, kind='file',
                                physical=(1, 2, 3 if path == a else 4, 0, 7, 0, 0), reason=None)
        with patch('core.library_path_ingest.observe_path', side_effect=observation):
            self.ingest([self.row(a), self.row(b)])
        self.assertEqual(self.service.read('count_files'), 2)
        self.assertEqual(self.service.read('get_path_identity_status')['conflict_groups'], 0)

    def test_no_reader_or_writer_transaction_is_held_during_probes(self):
        path = self.image()
        self.seed(path)
        self.service.backfill_path_identity()
        def probe(path, **kwargs):
            self.assertEqual(self.service.metrics_snapshot()['reads_active'], 0)
            self.assertTrue(self.service.submit('update_file_rating', path, 2).result(3))
            return observe_path(path, **kwargs)
        with patch('core.library_path_ingest.observe_path', side_effect=probe):
            self.ingest([self.row(path)])
        self.assertEqual(self.service.read('get_file_rating', str(path)), 2)

    def test_physical_change_during_preflight_rejects_entire_batch(self):
        self.service.backfill_path_identity()
        path = self.image()
        calls = 0
        def probe(path, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                Path(path).write_bytes(b'changed-private')
            return observe_path(path, **kwargs)
        with patch('core.library_path_ingest.observe_path', side_effect=probe):
            with self.assertRaisesRegex(ValueError, 'physical_state_changed'):
                self.ingest([self.row(path)])
        self.assertEqual(self.service.read('count_files'), 0)

    def test_foreign_sql_change_after_proof_fails_atomically_without_retry(self):
        self.service.backfill_path_identity()
        path = self.image()
        original = self.service.submit
        calls = 0
        def submit(operation, *args, **kwargs):
            nonlocal calls
            if operation == 'register_files_observed':
                calls += 1
                with closing(sqlite3.connect(self.service.db_path)) as conn:
                    conn.execute("INSERT INTO files(path,filename) VALUES('C:/foreign.png','foreign.png')")
                    conn.commit()
            return original(operation, *args, **kwargs)
        with patch.object(self.service, 'submit', side_effect=submit):
            with self.assertRaisesRegex(LibraryError, 'sql_state_changed'):
                self.ingest([self.row(path)])
        self.assertEqual(calls, 1)
        self.assertEqual(self.sql('SELECT path FROM files'), [('C:/foreign.png',)])

    def test_sql_failure_after_insert_rolls_back_files_fts_keys_and_clock(self):
        self.service.backfill_path_identity()
        path = self.image()
        before = self.sql('SELECT epoch FROM path_identity_clock')
        with patch('core.library_path_ingest.store', side_effect=ValueError('injected_identity_failure')):
            with self.assertRaisesRegex(LibraryError, 'injected_identity_failure'):
                self.ingest([self.row(path)])
        self.assertEqual(self.sql('SELECT id FROM files'), [])
        self.assertEqual(self.sql('SELECT rowid FROM files_fts'), [])
        self.assertEqual(self.sql('SELECT file_id FROM path_identity_records'), [])
        self.assertEqual(self.sql('SELECT epoch FROM path_identity_clock'), before)

    def test_indexer_alias_and_new_file_keep_coverage_complete(self):
        folder = self.root/'Images'
        path = self.image('Images/One.png')
        original_name = folder/'ONE.PNG'
        file_id = self.seed(original_name)
        self.service.submit('edit_curation', str(original_name), rating=5).result(3)
        self.service.backfill_path_identity()
        self.image('Images/Two.png')
        worker = IndexerWorker(self.db, [str(folder)], deep_clean=True)
        worker.batch_size = 1
        worker.run()
        self.assertEqual(worker.outcome, InventoryStatus.COMPLETE)
        self.assertEqual(self.service.read('count_files'), 2)
        self.assertEqual(self.sql(f'SELECT path,rating,is_offline FROM files WHERE id={file_id}'),
                         [(original_name.as_posix(), 5, 0)])
        self.assertEqual(self.service.read('get_path_identity_status')['pending'], 0)

    def test_indexer_error_does_not_reconcile_or_archive_partial_inventory(self):
        folder = self.root/'Images'
        path = self.image('Images/One.png')
        self.seed(path)
        self.service.submit('prepare_path_identity').result(3)
        worker = IndexerWorker(self.db, [str(folder)], deep_clean=True)
        with patch.object(worker, '_reconcile_missing', side_effect=AssertionError('archive after failure')):
            worker.run()
        self.assertEqual(worker.outcome, InventoryStatus.ERROR)
        self.assertEqual(self.sql('SELECT is_offline FROM files'), [(0,)])

    def test_pre_cancel_and_cancel_during_probe_do_not_admit_writes(self):
        path = self.image()
        self.service.backfill_path_identity()
        self.event.set()
        with self.assertRaises(BatchCancelled):
            self.ingest([self.row(path)])
        self.event.clear()
        def probe(path, **kwargs):
            self.event.set()
            return observe_path(path, **kwargs)
        with patch('core.library_path_ingest.observe_path', side_effect=probe):
            with self.assertRaises(BatchCancelled):
                self.ingest([self.row(path)])
        self.assertEqual(self.service.read('count_files'), 0)

    def test_zero_id_alias_is_valid_existing_target(self):
        path = self.image()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('INSERT INTO files(id,path,filename,rating) VALUES(0,?,?,5)', (path.as_posix(), path.name))
            conn.commit()
        self.service.backfill_path_identity()
        self.ingest([self.row(self.root/'ONE.PNG')])
        self.assertEqual(self.sql('SELECT id,rating FROM files'), [(0, 5)])

    def test_preparation_racing_legacy_admission_rejects_without_registration(self):
        path = self.image()
        original = self.service.submit
        def submit(operation, *args, **kwargs):
            if operation == 'register_files_observed':
                original('prepare_path_identity').result(3)
            return original(operation, *args, **kwargs)
        with patch.object(self.service, 'submit', side_effect=submit):
            with self.assertRaisesRegex(LibraryError, 'sql_state_changed'):
                self.ingest([self.row(path)])
        self.assertEqual(self.service.read('count_files'), 0)

    def test_oversized_batch_rejected_before_read_or_io(self):
        with patch.object(self.service, 'read', side_effect=AssertionError('oversized read')):
            with self.assertRaisesRegex(ValueError, 'invalid_path_ingest_batch'):
                self.ingest([self.row(self.root/f'{i}.png') for i in range(101)])

    def test_running_identity_batch_settles_after_stop_before_returning_confirmation(self):
        self.service.backfill_path_identity()
        path = self.image()
        entered, release = threading.Event(), threading.Event()
        original = LibraryRepository.register_files_observed
        def held(repo, records):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('private hold timeout')
            return original(repo, records)
        with patch.object(LibraryRepository, 'register_files_observed', held), ThreadPoolExecutor(1) as pool:
            job = pool.submit(self.ingest, [self.row(path)])
            try:
                self.assertTrue(entered.wait(3))
                self.event.set()
                self.assertFalse(job.done())
                release.set()
                self.assertTrue(job.result(3))
            finally:
                release.set()
        self.assertEqual(self.service.read('get_path_identity_status')['pending'], 0)
        self.assertEqual(self.service.read('count_files'), 1)

    def test_full_queue_keeps_fifo_and_latest_curation_without_duplicate_id(self):
        path = self.image()
        file_id = self.seed(path)
        self.service.backfill_path_identity()
        entered, release = threading.Event(), threading.Event()
        original = LibraryRepository.update_file_rating
        order = []
        def held(repo, *args, **kwargs):
            if not entered.is_set():
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('private hold timeout')
            order.append(args[1])
            return original(repo, *args, **kwargs)
        with patch.object(LibraryRepository, 'update_file_rating', held), ThreadPoolExecutor(1) as pool:
            first = self.service.submit('update_file_rating', str(path), 1)
            try:
                self.assertTrue(entered.wait(3))
                second = self.service.submit('update_file_rating', str(path), 5)
                job = pool.submit(self.ingest, [self.row(self.root/'ONE.PNG')])
                deadline = time.monotonic()+3
                while self.service.metrics_snapshot()['rejected_full'] == 0 and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertGreater(self.service.metrics_snapshot()['rejected_full'], 0)
                release.set()
                self.assertTrue(first.result(3))
                self.assertTrue(second.result(3))
                self.assertTrue(job.result(3))
            finally:
                release.set()
        self.assertEqual(order, [1, 5])
        self.assertEqual(self.sql('SELECT id,rating FROM files'), [(file_id, 5)])

    def test_reader_cancellation_maps_to_cancelled_not_index_error(self):
        path = self.image()
        with patch.object(self.service, 'read', side_effect=LibraryError('get_path_identity_ingest_state', 'cancelled')):
            with self.assertRaises(BatchCancelled):
                self.ingest([self.row(path)])


if __name__ == '__main__':
    unittest.main()
