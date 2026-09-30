"""Inventario por diferencias; sólo SQLite/archivos temporales, nunca biblioteca real."""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from core.library_repository import LibraryRepository
from core.library_service import LibraryError, LibraryService
from core.database_manager import DatabaseManager
from core.inventory import InventoryStatus
from modules.librarian.logic.indexer import IndexerWorker
from PySide6.QtWidgets import QApplication


def observation(path='C:/fixture/a.png', **changes):
    data = dict(path=path, filename=Path(path).name, size=13, created=1.25)
    data.update(changes)
    return data


class InventoryDelta(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.repo = LibraryRepository(self.conn)
        self.repo.create_schema()
        self.repo.run_transformations()
        self.conn.commit()

    def register(self, batch):
        with self.conn:
            self.assertIs(self.repo.register_files_minimal(batch), True)

    def row(self, path='C:/fixture/a.png'):
        return self.conn.execute('SELECT * FROM files WHERE path=?', (path,)).fetchone()

    def test_unchanged_thousand_no_dml_no_sequence_no_fts(self):
        batch = [observation(f'C:/fixture/{i}.png') for i in range(1000)]
        self.register(batch)
        before = self.conn.total_changes
        sequence = self.conn.execute('SELECT * FROM sqlite_sequence').fetchall()
        trace = []
        self.conn.set_trace_callback(trace.append)
        with patch.object(self.repo, '_fts_sync_file', wraps=self.repo._fts_sync_file) as sync:
            self.register(batch)
        self.conn.set_trace_callback(None)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.conn.execute('SELECT * FROM sqlite_sequence').fetchall(), sequence)
        self.assertEqual(sync.call_count, 0)
        selects = [sql for sql in trace if sql.startswith('SELECT path,')]
        self.assertEqual(len(selects), 4)
        self.assertFalse(any(sql.lstrip().startswith(('INSERT', 'UPDATE', 'DELETE')) for sql in trace))

    def test_non_searchable_changes_keep_identity_curation_and_fts(self):
        self.register([observation(hash_original='original', origin_url='old', artist='old artist')])
        self.assertTrue(self.repo.edit_curation('C:/fixture/a.png', add_tags=['keep'], rating=5))
        self.assertTrue(self.repo.update_file_metadata('C:/fixture/a.png',
                                                     dict(positive='kept prompt', model='kept model')))
        self.conn.execute("UPDATE files SET hash_mod='current', phash='perceptual'")
        self.conn.commit()
        before = self.row()
        sequence = self.conn.execute('SELECT * FROM sqlite_sequence').fetchall()
        fts = self.conn.execute('SELECT * FROM files_fts').fetchall()
        links = self.conn.execute('SELECT * FROM file_tags').fetchall()
        with patch.object(self.repo, '_fts_sync_file') as sync:
            self.register([observation(size=99, created=3.5, hash_original='replacement',
                                       origin_url='new', origin_site='patreon', artist='new artist')])
        after = self.row()
        self.assertEqual(after[0], before[0])
        names = [c[1] for c in self.conn.execute('PRAGMA table_info(files)')]
        data = dict(zip(names, after))
        self.assertEqual((data['file_size'], data['created_date']), (99, 3.5))
        self.assertEqual((data['hash_original'], data['hash_mod'], data['phash']),
                         ('original', 'current', 'perceptual'))
        self.assertEqual((data['rating'], data['meta_positive'], data['meta_model']),
                         (5, 'kept prompt', 'kept model'))
        self.assertEqual((data['origin_url'], data['origin_site'], data['artist']),
                         ('new', 'patreon', 'new artist'))
        self.assertEqual(sync.call_count, 0)
        self.assertEqual(self.conn.execute('SELECT * FROM files_fts').fetchall(), fts)
        self.assertEqual(self.conn.execute('SELECT * FROM file_tags').fetchall(), links)
        self.assertEqual(self.conn.execute('SELECT * FROM sqlite_sequence').fetchall(), sequence)

    def test_repeated_existing_path_keeps_first_hash_and_latest_non_null_origin(self):
        self.register([observation()])
        before_id = self.repo.get_file_id('C:/fixture/a.png')
        sequence = self.conn.execute('SELECT * FROM sqlite_sequence').fetchall()
        with patch.object(self.repo, '_fts_sync_file') as sync:
            self.register([observation(hash_original='first', artist='first artist', size=20),
                           observation(hash_original='second', origin_url='url', size=30),
                           observation(hash_original=None, artist='last artist', size=40)])
        self.assertEqual(sync.call_count, 0)
        self.assertEqual(self.conn.execute('SELECT id,hash_original,origin_url,artist,file_size FROM files').fetchone(),
                         (before_id, 'first', 'url', 'last artist', 40))
        self.assertEqual(self.conn.execute('SELECT * FROM sqlite_sequence').fetchall(), sequence)

    def test_new_and_renamed_only_sync_searchable_rows(self):
        batch = [observation(f'C:/fixture/{i}.png') for i in range(3)]
        self.register(batch)
        old_id = self.repo.get_file_id(batch[1]['path'])
        self.assertTrue(self.repo.edit_curation(batch[1]['path'], add_tags=['kepttag'], rating=4))
        self.assertTrue(self.repo.update_file_metadata(batch[1]['path'], {'positive': 'keptprompt'}))
        batch[0]['size'] += 1
        batch[1]['filename'] = 'new searchable name.png'
        batch.append(observation('C:/fixture/new.png'))
        with patch.object(self.repo, '_fts_sync_file', wraps=self.repo._fts_sync_file) as sync:
            self.register(batch)
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(self.repo.get_file_id(batch[1]['path']), old_id)
        fts = self.conn.execute('SELECT filename,meta_positive,tags FROM files_fts WHERE rowid=?',
                                (old_id,)).fetchone()
        self.assertEqual(fts, ('new searchable name.png', 'keptprompt', 'kepttag'))
        self.assertEqual(self.repo.get_file_rating(batch[1]['path']), 4)
        hits = self.conn.execute("SELECT rowid FROM files_fts WHERE files_fts MATCH 'searchable'").fetchall()
        self.assertEqual(hits, [(old_id,)])

    def test_missing_optional_values_do_not_erase_origin(self):
        self.register([observation(hash_original='first', origin_url='url', origin_site='site', artist='artist')])
        before = self.conn.total_changes
        self.register([observation(hash_original='ignored', origin_url=None, origin_site=None, artist=None)])
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.conn.execute('SELECT hash_original,origin_url,origin_site,artist FROM files').fetchone(),
                         ('first', 'url', 'site', 'artist'))

    def test_null_hash_filled_once_without_fts(self):
        self.register([observation()])
        with patch.object(self.repo, '_fts_sync_file') as sync:
            self.register([observation(hash_original='first')])
            before = self.conn.total_changes
            self.register([observation(hash_original='second')])
        self.assertEqual(sync.call_count, 0)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.conn.execute('SELECT hash_original FROM files').fetchone(), ('first',))

    def test_empty_values_remain_values_not_null(self):
        self.register([observation(hash_original='', origin_url='url')])
        self.register([observation(hash_original='replacement', origin_url='')])
        self.assertEqual(self.conn.execute('SELECT hash_original,origin_url FROM files').fetchone(), ('', ''))
        before = self.conn.total_changes
        self.register([observation(hash_original='replacement', origin_url=None)])
        self.assertEqual(self.conn.total_changes, before)

    def test_duplicate_normalized_paths_keep_order_and_sync_once(self):
        a = observation('C:/fixture/child/../a.png', hash_original='first', origin_url='first-url')
        b = observation('C:/fixture/a.png', hash_original='second', origin_url=None, size=25)
        c = observation('C:/fixture/a.png', artist='last artist', origin_url='last-url', size=25)
        with patch.object(self.repo, '_fts_sync_file', wraps=self.repo._fts_sync_file) as sync:
            self.register([a, b, c])
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(self.conn.execute('SELECT path,hash_original,origin_url,artist,file_size FROM files').fetchall(),
                         [('C:/fixture/a.png', 'first', 'last-url', 'last artist', 25)])
        before = self.conn.total_changes
        self.register([c, c])
        self.assertEqual(self.conn.total_changes, before)

    def test_noop_does_not_repair_partial_fts_implicitly(self):
        self.register([observation()])
        self.conn.execute('DELETE FROM files_fts')
        self.conn.commit()
        self.register([observation()])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM files_fts').fetchone(), (0,))
        self.assertTrue(self.repo.rebuild_fts())
        self.assertEqual(self.conn.execute('SELECT count(*) FROM files_fts').fetchone(), (1,))

    def test_empty_batch_no_sql(self):
        trace = []
        self.conn.set_trace_callback(trace.append)
        self.assertTrue(self.repo.register_files_minimal([]))
        self.assertEqual(trace, [])

    def test_chunking_with_low_sqlite_variable_limit(self):
        self.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 256)
        batch = [observation(f'C:/fixture/{i}.png') for i in range(1025)]
        self.register(batch)
        before = self.conn.total_changes
        self.register(batch)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM files_fts').fetchone(), (1025,))


class InventoryServiceDelta(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = LibraryService(self.root / 'central.db')
        self.addCleanup(self.service.close)
        self.conn = sqlite3.connect(self.service.db_path)
        self.addCleanup(self.conn.close)
        self.db = DatabaseManager(service=self.service)
        self.path = (self.root / 'a.png').as_posix()
        self.seed = observation(self.path)
        self.assertTrue(self.service.submit('register_files_minimal', [self.seed]).result(3))

    def test_noop_no_wal_growth_no_external_revision_change(self):
        version = self.conn.execute('PRAGMA data_version').fetchone()
        wal = Path(self.service.db_path + '-wal')
        before = wal.stat().st_size
        sequence = self.conn.execute('SELECT * FROM sqlite_sequence').fetchall()
        for _ in range(10):
            self.assertTrue(self.service.submit('register_files_minimal', [self.seed]).result(3))
        self.assertEqual(wal.stat().st_size, before)
        self.assertEqual(self.conn.execute('PRAGMA data_version').fetchone(), version)
        self.assertEqual(self.conn.execute('SELECT * FROM sqlite_sequence').fetchall(), sequence)

    def test_sql_failure_rolls_back_whole_batch_with_cause(self):
        self.conn.execute("CREATE TRIGGER reject_file BEFORE INSERT ON files WHEN NEW.filename='bad.png' "
                          "BEGIN SELECT RAISE(ABORT,'fixture rejection'); END")
        self.conn.commit()
        batch = [dict(self.seed, size=99), observation((self.root / 'good.png').as_posix()),
                 observation((self.root / 'bad.png').as_posix())]
        with self.assertRaisesRegex(LibraryError, 'fixture rejection'):
            self.service.submit('register_files_minimal', batch).result(3)
        self.assertEqual(self.conn.execute('SELECT path,file_size FROM files').fetchall(), [(self.path, 13)])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM files_fts').fetchone(), (1,))
        self.assertTrue(self.service.submit('register_files_minimal', [dict(self.seed, size=17)]).result(3))

    def test_fts_failure_rolls_back_files_sequence_and_curation(self):
        self.assertTrue(self.service.submit('edit_curation', self.path, add_tags=['keep'], rating=5).result(3))
        before = self.conn.execute('SELECT * FROM files').fetchall()
        sequence = self.conn.execute('SELECT * FROM sqlite_sequence').fetchall()
        links = self.conn.execute('SELECT * FROM file_tags').fetchall()
        fts = self.conn.execute('SELECT * FROM files_fts').fetchall()
        with patch.object(LibraryRepository, '_fts_sync_file', side_effect=sqlite3.DatabaseError('fixture FTS')):
            with self.assertRaisesRegex(LibraryError, 'fixture FTS'):
                self.service.submit('register_files_minimal', [dict(self.seed, filename='renamed.png'),
                    observation((self.root / 'new.png').as_posix())]).result(3)
        self.assertEqual(self.conn.execute('SELECT * FROM files').fetchall(), before)
        self.assertEqual(self.conn.execute('SELECT * FROM sqlite_sequence').fetchall(), sequence)
        self.assertEqual(self.conn.execute('SELECT * FROM file_tags').fetchall(), links)
        self.assertEqual(self.conn.execute('SELECT * FROM files_fts').fetchall(), fts)

    def test_update_failure_rolls_back_earlier_update_and_new_file(self):
        other = observation((self.root / 'b.png').as_posix())
        self.assertTrue(self.service.submit('register_files_minimal', [other]).result(3))
        before = self.conn.execute('SELECT * FROM files ORDER BY id').fetchall()
        sequence = self.conn.execute('SELECT * FROM sqlite_sequence').fetchall()
        self.conn.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON files WHEN NEW.file_size=999 "
                          "BEGIN SELECT RAISE(ABORT,'fixture update rejection'); END")
        self.conn.commit()
        with self.assertRaisesRegex(LibraryError, 'fixture update rejection'):
            self.service.submit('register_files_minimal', [dict(self.seed, size=50),
                observation((self.root / 'new.png').as_posix()), dict(other, size=999)]).result(3)
        self.assertEqual(self.conn.execute('SELECT * FROM files ORDER BY id').fetchall(), before)
        self.assertEqual(self.conn.execute('SELECT * FROM sqlite_sequence').fetchall(), sequence)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM files_fts').fetchone(), (2,))

    def test_facade_preserves_false_on_failure(self):
        with patch.object(LibraryRepository, '_fts_sync_file', side_effect=sqlite3.DatabaseError('fixture FTS')):
            self.assertFalse(self.db.register_files_minimal([observation((self.root / 'new.png').as_posix())]))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM files').fetchone(), (1,))

    def test_second_indexer_pass_no_revision_change(self):
        self.root.joinpath('a.png').write_bytes(b'image fixture')
        self.assertTrue(self.db.edit_curation(self.path, add_tags=['keep'], rating=4))
        for run in range(2):
            worker = IndexerWorker(self.db, [str(self.root)], deep_clean=True)
            terminal = []
            worker.finished_signal.connect(lambda: terminal.append(True))
            worker.start()
            self.assertTrue(worker.wait(5000))
            self.app.processEvents()
            self.assertEqual(terminal, [True])
            self.assertEqual(worker.outcome, InventoryStatus.COMPLETE)
            if run == 0:
                version = self.conn.execute('PRAGMA data_version').fetchone()
        self.assertEqual(self.conn.execute('PRAGMA data_version').fetchone(), version)
        self.assertEqual(self.db.get_file_rating(self.path), 4)
        self.assertEqual(self.db.get_tags_for_file(self.path), ['keep'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
