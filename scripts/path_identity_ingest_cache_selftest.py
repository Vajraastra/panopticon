"""Private coherent coverage transitions after committed observation batches."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.library_connections import open_reader
from core.library_service import LibraryService, LibraryError
from core.library_repository import LibraryRepository
from core.path_identity_registry import status
from core.path_identity_clock import token, CoverageCache
from core.path_identity import observe_path


class Coverage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = LibraryService(self.root/'private.db')
        self.addCleanup(self.service.close)
        self.service.backfill_path_identity()

    def row(self, name):
        path = self.root/name
        path.write_bytes(b'private')
        return dict(path=path.as_posix(), filename=name, size=7, created=1)

    def ingest(self, rows):
        return self.service.register_files_observed(rows, cancel_event=threading.Event())

    def traced(self):
        counts = dict(coverage=0, conflicts=0)
        def reader(path):
            conn = open_reader(path)
            def trace(sql):
                if 'SUM(CASE WHEN r.file_id IS NULL' in sql:
                    counts['coverage'] += 1
                if 'GROUP BY r.canonical HAVING' in sql:
                    counts['conflicts'] += 1
            conn.set_trace_callback(trace)
            return conn
        return counts, patch('core.library_service.open_reader', side_effect=reader)

    def fresh(self):
        with closing(open_reader(self.service.db_path)) as conn:
            conn.execute('BEGIN')
            return status(conn)

    def test_five_new_batches_retain_exact_coverage_without_five_full_scans(self):
        rows = [self.row(f'{i}.png') for i in range(5)]
        counts, traced = self.traced()
        with traced:
            for row in rows:
                self.assertTrue(self.ingest([row]))
            result = self.service.read('get_path_identity_status')
        self.assertEqual(result, self.fresh())
        self.assertEqual(result['total'], 5)
        self.assertEqual(counts, dict(coverage=0, conflicts=0))

    def test_no_op_keeps_clock_and_coverage(self):
        row = self.row('one.png')
        self.ingest([row])
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            before = token(conn)
        counts, traced = self.traced()
        with traced:
            self.ingest([row])
            self.assertEqual(self.service.read('get_path_identity_status')['total'], 1)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(token(conn), before)
        self.assertEqual(counts, dict(coverage=0, conflicts=0))

    def test_changed_physical_evidence_keeps_coverage_without_recount(self):
        row = self.row('one.png')
        self.ingest([row])
        Path(row['path']).write_bytes(b'changed private')
        row['size'] = 15
        counts, traced = self.traced()
        with traced:
            self.ingest([row])
            value = self.service.read('get_path_identity_status')
        self.assertEqual(value, self.fresh())
        self.assertEqual(counts, dict(coverage=0, conflicts=0))

    def test_optional_cache_failure_cannot_report_committed_batch_as_failed(self):
        row = self.row('one.png')
        with patch.object(self.service._path_coverage, 'put', side_effect=MemoryError('private cache fault')):
            self.assertTrue(self.ingest([row]))
        self.assertEqual(self.service.read('get_path_identity_status'), self.fresh())
        self.assertEqual(self.service.read('count_files'), 1)

    def test_late_failure_discards_pending_transition_before_rollback(self):
        row = self.row('one.png')
        original = LibraryRepository.register_files_observed
        seen = []
        def failed(repo, values):
            original(repo, values)
            seen.append(repo._path_ingest_transition)
            raise ValueError('private after candidate fault')
        with patch.object(LibraryRepository, 'register_files_observed', failed):
            with self.assertRaisesRegex(LibraryError, 'after candidate fault'):
                self.ingest([row])
        self.assertIsNotNone(seen[0])
        self.assertEqual(self.service.read('count_files'), 0)
        self.ingest([row])
        self.assertEqual(self.service.read('get_path_identity_status'), self.fresh())

    def test_commit_failure_never_publishes_uncommitted_hint(self):
        self.service.close()
        fail = threading.Event()
        original = sqlite3.connect
        class Connection(sqlite3.Connection):
            def commit(conn):
                if fail.is_set():
                    fail.clear()
                    raise sqlite3.OperationalError('private commit fault')
                return super().commit()
        def connect(*args, **kwargs):
            return original(*args, factory=Connection, **kwargs)
        with patch('core.library_service.sqlite3.connect', side_effect=connect):
            self.service = LibraryService(self.root/'commit-private.db')
        self.addCleanup(self.service.close)
        self.service.backfill_path_identity()
        row = self.row('one.png')
        fail.set()
        with self.assertRaisesRegex(LibraryError, 'commit fault'):
            self.ingest([row])
        self.assertEqual(self.service.read('count_files'), 0)
        self.assertEqual(self.service.read('get_path_identity_status'), self.fresh())
        self.ingest([row])
        self.assertEqual(self.service.read('get_path_identity_status')['total'], 1)

    def test_foreign_postcommit_insert_before_hint_is_not_acknowledged(self):
        row = self.row('one.png')
        original = LibraryRepository.finish_path_ingest
        inserted = threading.Event()
        def finish(repo, committed):
            if committed and repo._path_ingest_transition is not None and not inserted.is_set():
                with closing(sqlite3.connect(self.service.db_path)) as conn:
                    conn.execute("INSERT INTO files(path,filename) VALUES('C:/foreign.png','foreign.png')")
                    conn.commit()
                inserted.set()
            return original(repo, committed)
        with patch.object(LibraryRepository, 'finish_path_ingest', finish):
            self.ingest([row])
        self.assertTrue(inserted.is_set())
        value = self.service.read('get_path_identity_status')
        self.assertEqual((value['total'], value['pending']), (2, 1))
        self.assertEqual(value, self.fresh())

    def test_extra_trigger_forces_recount_even_when_its_effect_is_harmless(self):
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('CREATE TRIGGER foreign_guard AFTER INSERT ON files BEGIN SELECT 1; END')
            conn.commit()
        counts, traced = self.traced()
        with traced:
            self.ingest([self.row('one.png')])
            value = self.service.read('get_path_identity_status')
        self.assertEqual(value, self.fresh())
        self.assertEqual(counts, dict(coverage=1, conflicts=1))

    def test_foreign_path_effect_remains_pending_in_authoritative_recount(self):
        first = self.row('one.png')
        self.ingest([first])
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute("CREATE TRIGGER foreign_rewrite AFTER INSERT ON files BEGIN "
                         "UPDATE files SET path=path||'.moved' WHERE filename='one.png'; END")
            conn.commit()
        self.ingest([self.row('two.png')])
        value = self.service.read('get_path_identity_status')
        self.assertEqual((value['total'], value['pending']), (2, 1))
        self.assertEqual(value, self.fresh())

    def test_temporary_trigger_installed_during_write_also_disables_hint(self):
        original = LibraryRepository.register_files_minimal
        def register(repo, rows):
            repo.conn.execute('CREATE TEMP TRIGGER temporary_guard AFTER INSERT ON files BEGIN SELECT 1; END')
            return original(repo, rows)
        counts, traced = self.traced()
        with traced, patch.object(LibraryRepository, 'register_files_minimal', register):
            self.ingest([self.row('one.png')])
            value = self.service.read('get_path_identity_status')
        self.assertEqual(value, self.fresh())
        self.assertEqual(counts, dict(coverage=1, conflicts=1))

    def test_old_reader_eviction_cannot_poison_new_transaction_hint(self):
        with closing(open_reader(self.service.db_path)) as old:
            old.execute('BEGIN')
            self.assertEqual(status(old)['total'], 0)
            self.ingest([self.row('one.png')])
            self.assertEqual(status(old, self.service._path_coverage)['total'], 0)
        self.assertEqual(self.service.read('get_path_identity_status')['total'], 1)
        self.assertEqual(self.service.read('get_path_identity_status'), self.fresh())

    def test_unknown_canonical_rebinding_of_existing_path_uses_recount(self):
        row = self.row('one.png')
        self.ingest([row])
        def probe(path, **kwargs):
            return replace(observe_path(path, **kwargs), canonical='C:/PrivateNewName/one.png')
        counts, traced = self.traced()
        with traced, patch('core.library_path_ingest.observe_path', side_effect=probe):
            self.ingest([row])
            value = self.service.read('get_path_identity_status')
        self.assertEqual(value, self.fresh())
        self.assertEqual(value['total'], 1)
        self.assertEqual(counts, dict(coverage=1, conflicts=1))

    def test_unrelated_conflicts_keep_their_count_during_new_ingestion(self):
        row = self.row('one.png')
        alias = dict(row, path=str(self.root/'ONE.PNG'))
        self.service.submit('register_files_minimal', [row, alias]).result(3)
        self.service.backfill_path_identity()
        self.assertEqual(self.service.read('get_path_identity_status')['conflict_groups'], 1)
        self.ingest([self.row('two.png')])
        value = self.service.read('get_path_identity_status')
        self.assertEqual((value['total'], value['conflict_groups']), (3, 1))
        self.assertEqual(value, self.fresh())

    def test_repeated_aliases_new_and_existing_and_offline_keep_exact_counts(self):
        first = self.row('one.png')
        self.ingest([first])
        self.service.submit('remove_files', [first['path']]).result(3)
        self.service.backfill_path_identity()
        second = self.row('two.png')
        self.ingest([first, second, dict(second, path=str(self.root/'TWO.PNG'))])
        value = self.service.read('get_path_identity_status')
        self.assertEqual((value['total'], value['pending'], value['unresolved']), (2, 0, 0))
        self.assertEqual(value, self.fresh())

    def test_final_epoch_mismatch_disables_prediction(self):
        original = LibraryRepository.register_files_minimal
        def register(repo, rows):
            result = original(repo, rows)
            repo.conn.execute('UPDATE path_identity_clock SET epoch=epoch+1')
            return result
        counts, traced = self.traced()
        with traced, patch.object(LibraryRepository, 'register_files_minimal', register):
            self.ingest([self.row('one.png')])
            value = self.service.read('get_path_identity_status')
        self.assertEqual(value, self.fresh())
        self.assertEqual(counts, dict(coverage=1, conflicts=1))


if __name__ == '__main__':
    unittest.main()
