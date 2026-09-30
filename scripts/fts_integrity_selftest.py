"""Integridad FTS en bases temporales; no abre la biblioteca del usuario."""
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import library_fts
from core.database_manager import DatabaseManager
from core.library_connections import open_reader
from core.library_repository import LibraryRepository
from core.library_service import LibraryError, LibraryService


class FTSIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'fixture.db'
        self.service = LibraryService(self.path)
        self.addCleanup(self.service.close)
        self.db = DatabaseManager(service=self.service)
        self.add(3)

    def sql(self, sql, args=()):
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute(sql, args).fetchall()
            conn.commit()
            return rows

    def add(self, count):
        self.service.submit('register_files_minimal', [
            dict(path=f'/fixture/{i}.png', filename=f'picture_{i}.png', size=i,
                 created=1, hash_original=f'hash-{i}') for i in range(count)
        ]).result(timeout=10)

    def check(self, missing=0, stale=0, orphaned=0):
        self.assertEqual(self.service.read('check_fts'),
                         dict(missing=missing, stale=stale, orphaned=orphaned,
                              consistent=not (missing or stale or orphaned)))

    def snapshot(self):
        return {table: self.sql(f'SELECT {"rowid,*" if table == "files_fts" else "*"} '
                               f'FROM {table} ORDER BY 1') for table in
                ('files', 'tags', 'file_tags', 'files_fts', 'sqlite_sequence')}

    def repair(self):
        self.assertIs(self.service.submit('repair_fts').result(timeout=10), True)

    def test_healthy_check_and_repair_no_writes_or_revision(self):
        with closing(sqlite3.connect(self.path)) as reader:
            version = reader.execute('PRAGMA data_version').fetchone()
            before = self.snapshot()
            self.check()
            self.repair()
            self.assertEqual(reader.execute('PRAGMA data_version').fetchone(), version)
            self.assertEqual(self.snapshot(), before)

    def test_missing_row_restores_search(self):
        self.sql('DELETE FROM files_fts WHERE rowid=2')
        self.check(missing=1)
        self.assertEqual(self.sql("SELECT rowid FROM files_fts WHERE files_fts MATCH 'picture_1'"), [])
        self.repair()
        self.check()
        self.assertEqual(self.sql("SELECT rowid FROM files_fts WHERE files_fts MATCH 'picture_1'"), [(2,)])

    def test_equal_counts_wrong_identity_not_considered_healthy(self):
        self.sql('UPDATE files_fts SET rowid=999 WHERE rowid=2')
        self.assertEqual(self.sql('SELECT count(*) FROM files_fts'), [(3,)])
        self.check(missing=1, orphaned=1)
        self.repair()
        self.check()
        self.assertEqual(self.sql('SELECT rowid FROM files_fts ORDER BY rowid'), [(1,), (2,), (3,)])

    def test_each_searchable_field_is_checked(self):
        for field in (*library_fts.FIELDS, 'tags'):
            with self.subTest(field=field):
                self.sql(f'UPDATE files_fts SET {field}=? WHERE rowid=2', ('outdated',))
                self.check(stale=1)
                self.repair()
                self.check()

    def test_null_fts_values_are_stale_empty_source_values_are_canonical(self):
        self.sql('UPDATE files SET meta_positive=NULL,meta_negative=? WHERE id=2', ('',))
        self.check()
        self.sql('UPDATE files_fts SET meta_positive=NULL WHERE rowid=2')
        self.check(stale=1)
        self.repair()
        self.assertEqual(self.sql('SELECT meta_positive,meta_negative FROM files_fts WHERE rowid=2'), [('', '')])

    def test_tags_order_unicode_category_and_source_preserved(self):
        for tag in ('zebra', 'árbol', 'alpha'):
            self.assertTrue(self.db.edit_curation('/fixture/1.png', add_tags=[tag]))
        expected = self.sql('SELECT tags FROM files_fts WHERE rowid=2')
        self.sql("UPDATE files_fts SET tags='stale' WHERE rowid=2")
        before = self.snapshot()
        self.check(stale=1)
        self.repair()
        after = self.snapshot()
        self.assertEqual(self.sql('SELECT tags FROM files_fts WHERE rowid=2'), expected)
        for table in before:
            if table != 'files_fts':
                self.assertEqual(before[table], after[table])

    def test_only_changed_row_is_written(self):
        self.sql("UPDATE files_fts SET filename='stale' WHERE rowid=2")
        with closing(sqlite3.connect(self.path, isolation_level=None)) as conn:
            seen = []
            conn.set_trace_callback(seen.append)
            conn.execute('BEGIN IMMEDIATE')
            LibraryRepository(conn).repair_fts()
            conn.commit()
        dml = [s for s in seen if s.startswith(('DELETE FROM files_fts', 'INSERT INTO files_fts'))]
        self.assertEqual(len(dml), 2)
        self.assertIn('rowid=2', dml[0])
        self.check()

    def test_large_missing_and_orphan_batches_including_negative_ids(self):
        self.add(1030)
        self.sql('DELETE FROM files_fts')
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executemany('INSERT INTO files_fts(rowid,filename) VALUES (?,?)',
                             ((-i, 'orphan') for i in range(600)))
            conn.commit()
        self.check(missing=1030, orphaned=600)
        before = self.snapshot()
        self.repair()
        self.check()
        after = self.snapshot()
        for table in ('files', 'tags', 'file_tags', 'sqlite_sequence'):
            self.assertEqual(before[table], after[table])
        self.assertEqual(len(after['files_fts']), 1030)

    def test_empty_library_removes_orphans(self):
        self.sql('DELETE FROM files')
        self.check(orphaned=3)
        self.repair()
        self.check()
        self.assertEqual(self.sql('SELECT count(*) FROM files_fts'), [(0,)])

    def test_signed_legacy_file_ids_are_not_skipped(self):
        self.sql("INSERT INTO files(id,path,filename) VALUES (-5,'/negative','negative.png')")
        self.sql("INSERT INTO files(id,path,filename) VALUES (0,'/zero','zero.png')")
        self.check(missing=2)
        self.repair()
        self.check()
        self.assertEqual(self.sql('SELECT rowid FROM files_fts WHERE rowid<=0 ORDER BY rowid'), [(-5,), (0,)])

    def test_orphan_deletion_failure_rolls_back_prior_repairs(self):
        self.sql("UPDATE files_fts SET filename='stale' WHERE rowid=2")
        self.sql("INSERT INTO files_fts(rowid,filename) VALUES (999,'orphan')")
        before = self.snapshot()

        class FailOrphan:
            def __init__(proxy, conn):
                proxy.conn = conn
                proxy.deletes = 0

            def execute(proxy, *args):
                return proxy.conn.execute(*args)

            def executemany(proxy, sql, rows):
                if sql.startswith('DELETE'):
                    proxy.deletes += 1
                    if proxy.deletes == 2:
                        raise sqlite3.OperationalError('fixture_orphan_delete')
                return proxy.conn.executemany(sql, rows)

        with patch.object(LibraryRepository, 'repair_fts',
                          lambda repo: library_fts.repair_fts(FailOrphan(repo.conn))):
            with self.assertRaisesRegex(LibraryError, 'fixture_orphan_delete'):
                self.service.submit('repair_fts').result(timeout=5)
        self.assertEqual(self.snapshot(), before)
        self.repair()
        self.check()

    def test_sql_error_is_visible_and_owner_remains_usable(self):
        self.sql('DROP TABLE files_fts')
        with self.assertRaisesRegex(LibraryError, 'files_fts'):
            self.service.read('check_fts')
        with self.assertRaisesRegex(LibraryError, 'files_fts'):
            self.service.submit('repair_fts').result(timeout=5)
        self.assertFalse(self.db.repair_fts())
        self.assertTrue(self.db.update_file_rating('/fixture/1.png', 4))

    def test_failure_in_second_block_rolls_back_all_fts_and_sequence(self):
        self.add(600)
        self.sql("UPDATE files_fts SET filename='stale'")
        before = self.snapshot()

        class FailSecond:
            def __init__(proxy, conn):
                proxy.conn = conn
                proxy.inserts = 0

            def execute(proxy, *args):
                return proxy.conn.execute(*args)

            def executemany(proxy, sql, rows):
                if sql.startswith('INSERT'):
                    proxy.inserts += 1
                    if proxy.inserts == 2:
                        raise sqlite3.OperationalError('fixture_second_block')
                return proxy.conn.executemany(sql, rows)

        with patch.object(LibraryRepository, 'repair_fts',
                          lambda repo: library_fts.repair_fts(FailSecond(repo.conn))):
            with self.assertRaisesRegex(LibraryError, 'fixture_second_block'):
                self.service.submit('repair_fts').result(timeout=5)
        self.assertEqual(self.snapshot(), before)
        self.repair()
        self.check()

    def test_readers_see_old_snapshot_until_commit_and_next_write_syncs(self):
        self.sql("UPDATE files_fts SET filename='old' WHERE rowid=2")
        entered, release = threading.Event(), threading.Event()

        class Pause:
            def __init__(proxy, conn):
                proxy.conn = conn

            def execute(proxy, *args):
                return proxy.conn.execute(*args)

            def executemany(proxy, sql, rows):
                value = proxy.conn.executemany(sql, rows)
                if sql.startswith('INSERT'):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('fixture_timeout')
                return value

        with patch.object(LibraryRepository, 'repair_fts',
                          lambda repo: library_fts.repair_fts(Pause(repo.conn))):
            future = self.service.submit('repair_fts')
            try:
                self.assertTrue(entered.wait(5))
                self.check(stale=1)
                edit = self.service.submit('edit_curation', '/fixture/1.png', add_tags=['after'])
                self.assertFalse(future.done())
            finally:
                release.set()
            self.assertTrue(future.result(timeout=5))
            self.assertTrue(edit.result(timeout=5))
        self.check()
        self.assertEqual(self.sql('SELECT tags FROM files_fts WHERE rowid=2'), [('after',)])

    def test_read_snapshot_coherent_across_external_commit(self):
        with closing(open_reader(str(self.path))) as conn:
            conn.execute('BEGIN')
            self.assertTrue(LibraryRepository(conn).check_fts()['consistent'])
            self.sql('DELETE FROM files_fts WHERE rowid=2')
            self.assertTrue(LibraryRepository(conn).check_fts()['consistent'])
        self.check(missing=1)

    def test_cancelled_read_does_not_write(self):
        event = threading.Event()
        event.set()
        before = self.snapshot()
        with self.assertRaisesRegex(LibraryError, 'cancelled'):
            self.service.read('check_fts', cancel_event=event)
        self.assertEqual(self.snapshot(), before)

    def test_migration_repairs_equal_counts_once_preserving_backup(self):
        self.service.close()
        self.sql('DELETE FROM panopticon_schema')
        self.sql('UPDATE files_fts SET rowid=999 WHERE rowid=2')
        before = self.snapshot()
        service = LibraryService(self.path)
        self.addCleanup(service.close)
        self.assertTrue(service.read('check_fts')['consistent'])
        backups = list(Path(self.tmp.name).glob('*.bak'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as conn:
            self.assertEqual(conn.execute('SELECT rowid,* FROM files_fts ORDER BY rowid').fetchall(), before['files_fts'])

    def test_existing_marker_does_not_run_hidden_audit_or_repair(self):
        self.service.close()
        self.sql('DELETE FROM files_fts WHERE rowid=2')
        with patch.object(LibraryRepository, 'repair_fts', side_effect=AssertionError('hidden_scan')):
            service = LibraryService(self.path)
        self.addCleanup(service.close)
        self.assertEqual(service.read('check_fts')['missing'], 1)

    def test_migration_failure_rolls_back_and_preserves_original(self):
        self.service.close()
        self.sql('DELETE FROM panopticon_schema')
        self.sql("UPDATE files_fts SET filename='stale' WHERE rowid=2")
        before = self.snapshot()
        real = LibraryRepository.repair_fts

        def fail(repo):
            real(repo)
            raise sqlite3.OperationalError('fixture_migration_fts')

        with patch.object(LibraryRepository, 'repair_fts', fail):
            with self.assertRaisesRegex(LibraryError, 'fixture_migration_fts'):
                LibraryService(self.path)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.sql('SELECT * FROM panopticon_schema'), [])

    def test_explicit_rebuild_still_regenerates_every_row_and_propagates_cause(self):
        before = self.snapshot()
        with patch.object(LibraryRepository, '_fts_sync_file', side_effect=sqlite3.OperationalError('fixture_rebuild')):
            with self.assertRaisesRegex(LibraryError, 'fixture_rebuild'):
                self.service.submit('rebuild_fts').result(timeout=5)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.db.rebuild_fts())
        self.check()


if __name__ == '__main__':
    unittest.main(verbosity=2)
