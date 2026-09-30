"""Exportación/portabilidad/conflictos en carpetas y DB exclusivamente temporales."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import subprocess
from contextlib import closing
from unittest.mock import patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from core import curation_sidecar as sidecar
from core.curation_export_lock import GUARD, MAGIC
import curation_snapshot_selftest as helpers


class SidecarTests(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.folder = Path(self.temp.name) / 'colección ñ'
        self.folder.mkdir()
        self.target = self.folder / sidecar.NAME

    def register(self, names=('a.png',), digest='a' * 64):
        paths = [(self.folder / name).as_posix() for name in names]
        self.service.submit('register_files_minimal', [dict(path=path, filename=Path(path).name,
            size=1, created=1, hash_original=digest) for path in paths]).result(3)
        return paths

    def export(self, **kwargs):
        return sidecar.export_curation(self.service.db_path, self.folder, **kwargs)

    def fingerprint(self):
        return hashlib.sha256(self.target.read_bytes()).hexdigest()

    def clean(self):
        self.assertEqual([path.name for path in self.folder.iterdir()
                          if path.name.startswith('.' + sidecar.NAME) and path.name != GUARD], [])
        self.assertEqual((self.folder / GUARD).read_bytes(), MAGIC)

    def test_export_owner_base_schema_and_minimum_payload(self):
        path = self.register()[0]
        self.service.submit('edit_curation', path, add_tags=['alpha'], rating=5).result(3)
        result = self.export()
        self.assertEqual(result['copies'], 1)
        with closing(sqlite3.connect(self.target)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='owner'").fetchone(), ('panopticon',))
            self.assertEqual(conn.execute('SELECT hash,filename,url_source FROM files').fetchall(), [('a'*64, 'a.png', None)])
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone(), ('ok',))
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone(), ('delete',))
        copy = sidecar.read_curation_page(self.folder)['copies'][0]
        self.assertEqual(copy['rating'], 5)
        self.assertEqual(copy['tags'], [dict(name='alpha', source='manual', category='manual')])
        self.assertNotIn('meta_positive', copy)
        self.clean()

    def test_same_hash_copies_keep_different_ratings_tags_and_sources(self):
        paths = self.register(('a.png', 'b.png'))
        for path, tag, rating in zip(paths, ('alpha', 'beta'), (1, 5)):
            self.service.submit('edit_curation', path, add_tags=[tag], rating=rating).result(3)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute("UPDATE file_tags SET source='ai' WHERE file_id=(SELECT id FROM files WHERE path=?)", (paths[1],))
            conn.commit()
        self.export()
        rows = sidecar.read_curation_page(self.folder)['copies']
        self.assertEqual([(r['relative_path'], r['rating'], r['tags'][0]['source']) for r in rows],
                         [('a.png', 1, 'manual'), ('b.png', 5, 'ai')])
        self.assertEqual([r['status'] for r in sidecar.compare_curation_page(self.service.db_path, self.folder)['reports']], ['same', 'same'])

    def test_missing_hash_keeps_curation_without_inventing_sha_identity(self):
        path = self.register(digest=None)[0]
        self.service.submit('edit_curation', path, rating=3).result(3)
        result = self.export()
        self.assertEqual(result['missing_hash'], 1)
        copy = sidecar.read_curation_page(self.folder)['copies'][0]
        self.assertIsNone(copy['hash_original'])
        self.assertEqual(copy['rating'], 3)
        self.assertEqual(sidecar.compare_curation_page(self.service.db_path, self.folder)['reports'][0]['status'], 'identity_missing')

    def test_zero_and_empty_are_authoritative_conflict_not_overwritten(self):
        path = self.register()[0]
        self.export()
        self.service.submit('edit_curation', path, add_tags=['new'], rating=5).result(3)
        report = sidecar.compare_curation_page(self.service.db_path, self.folder)['reports'][0]
        self.assertEqual((report['status'], report['sidecar']['rating'], report['sidecar']['tags']), ('conflict', 0, []))
        self.assertEqual((report['central']['rating'], report['central']['tags'][0]['name']), (5, 'new'))
        self.assertEqual(self.service.read('get_file_rating', path), 5)

    def test_folder_move_preserves_relative_paths_and_per_copy_curation(self):
        paths = self.register(('sub/a.png', 'sub/b.png'))
        self.service.submit('edit_curation', paths[0], rating=4).result(3)
        self.export()
        moved = self.folder.parent / 'otra unidad simulada'
        shutil.move(str(self.folder), str(moved))
        copies = sidecar.read_curation_page(moved)['copies']
        self.assertEqual([r['relative_path'] for r in copies], ['sub/a.png', 'sub/b.png'])
        self.assertEqual(copies[0]['rating'], 4)
        # El reporte no adivina/fusiona por hash al encontrar sólo ubicaciones antiguas.
        reports = sidecar.compare_curation_page(self.service.db_path, moved)['reports']
        self.assertEqual([r['status'] for r in reports], ['unregistered', 'unregistered'])
        self.assertEqual(self.service.read('count_files'), 2)

    def test_multiblock_exports_all_copies_beyond_256_with_same_hash(self):
        self.register(tuple(f'{i:04}.png' for i in range(1030)))
        self.assertEqual(self.export()['copies'], 1030)
        after = 0
        token = None
        seen = []
        while True:
            page = sidecar.read_curation_page(self.folder, after=after, export_id=token)
            token, after = page['export_id'], page['after']
            if not page['copies']:
                break
            self.assertLessEqual(len(page['copies']), 256)
            seen.extend(r['relative_path'] for r in page['copies'])
        self.assertEqual(seen, [f'{i:04}.png' for i in range(1030)])

    def test_snapshot_remains_coherent_across_concurrent_curation_commit(self):
        path = self.register()[0]
        real_open = sidecar.open_reader
        changed = []
        def reader(db):
            conn = real_open(db)
            if Path(db) == Path(self.service.db_path):
                def trace(sql):
                    if sql.startswith('SELECT ft.file_id') and not changed:
                        changed.append(True)
                        self.service.submit('edit_curation', path, add_tags=['new'], rating=5).result(3)
                conn.set_trace_callback(trace)
            return conn
        with patch.object(sidecar, 'open_reader', reader):
            self.export()
        copy = sidecar.read_curation_page(self.folder)['copies'][0]
        self.assertEqual((copy['rating'], copy['tags']), (0, []))
        self.assertEqual(self.service.read('get_file_rating', path), 5)

    def test_cancelled_export_preserves_previous_sidecar_and_cleans_owned_temps(self):
        self.register()
        self.export()
        previous = self.fingerprint()
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            self.export(cancelled=lambda: True)
        self.assertEqual(self.fingerprint(), previous)
        self.clean()

    def test_replace_failure_preserves_previous_sidecar(self):
        self.register()
        self.export()
        previous = self.fingerprint()
        with patch.object(sidecar.os, 'replace', side_effect=OSError('fixture_failure')):
            with self.assertRaisesRegex(OSError, 'fixture_failure'):
                self.export()
        self.assertEqual(self.fingerprint(), previous)
        self.clean()

    def test_first_creation_failure_cannot_publish_partial_file(self):
        self.register()
        with patch.object(sidecar, '_publish_new', side_effect=OSError('fixture_failure')):
            with self.assertRaisesRegex(OSError, 'fixture_failure'):
                self.export()
        self.assertFalse(self.target.exists())
        self.clean()

    def test_cherry_catalog_and_image_bytes_stay_unchanged(self):
        self.register()
        cherry = self.folder / 'catalog.db'
        image = self.folder / 'a.png'
        cherry.write_bytes(b'foreign Cherry sentinel')
        image.write_bytes(b'original image sentinel')
        self.export()
        self.assertEqual(cherry.read_bytes(), b'foreign Cherry sentinel')
        self.assertEqual(image.read_bytes(), b'original image sentinel')

    def test_foreign_target_is_never_overwritten(self):
        self.target.write_bytes(b'foreign sentinel')
        with self.assertRaises(sqlite3.DatabaseError):
            self.export()
        self.assertEqual(self.target.read_bytes(), b'foreign sentinel')
        self.clean()

    def test_owned_sidecar_with_wal_marker_is_not_replaced(self):
        self.register()
        self.export()
        previous = self.fingerprint()
        wal = Path(str(self.target) + '-journal')
        wal.write_bytes(b'foreign active journal')
        try:
            with self.assertRaisesRegex(ValueError, 'sidecar_in_use'):
                self.export()
            self.assertEqual(self.fingerprint(), previous)
            self.assertEqual(wal.read_bytes(), b'foreign active journal')
        finally:
            wal.unlink()
        self.clean()

    def test_existing_lock_is_not_removed_or_ignored(self):
        lock = self.folder / ('.' + sidecar.NAME + '.lock')
        lock.write_bytes(b'other exporter')
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(lock.read_bytes(), b'other exporter')

    def test_changed_export_token_rejects_mixed_pages(self):
        self.register()
        self.export()
        first = sidecar.read_curation_page(self.folder)
        self.export()
        with self.assertRaisesRegex(ValueError, 'sidecar_changed'):
            sidecar.read_curation_page(self.folder, export_id=first['export_id'])

    def test_invalid_hash_aborts_without_losing_previous_export(self):
        path = self.register()[0]
        self.export()
        previous = self.fingerprint()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute("UPDATE files SET hash_original='not-a-sha' WHERE path=?", (path,))
            conn.commit()
        with self.assertRaisesRegex(ValueError, 'invalid_catalogued_hash'):
            self.export()
        self.assertEqual(self.fingerprint(), previous)
        self.clean()

    def test_tampered_relative_path_cannot_escape_folder(self):
        self.register()
        self.export()
        with closing(sqlite3.connect(self.target)) as conn:
            conn.execute("UPDATE curation_copies SET relative_path='../outside.png'")
            conn.commit()
        with self.assertRaisesRegex(ValueError, 'invalid_relative_path'):
            sidecar.compare_curation_page(self.service.db_path, self.folder)

    def test_missing_source_db_is_not_created(self):
        absent = self.folder / 'absent.db'
        with self.assertRaises(FileNotFoundError):
            sidecar.export_curation(absent, self.folder)
        self.assertFalse(absent.exists())
        self.assertFalse(self.target.exists())

    def test_empty_folder_has_valid_owned_empty_export(self):
        self.assertEqual(self.export()['copies'], 0)
        self.assertEqual(sidecar.read_curation_page(self.folder)['copies'], [])

    def test_sql_reads_are_per_block_not_per_image(self):
        self.register(tuple(f'{i}.png' for i in range(600)))
        queries = []
        real_open = sidecar.open_reader
        def reader(db):
            conn = real_open(db)
            if Path(db) == Path(self.service.db_path):
                conn.set_trace_callback(queries.append)
            return conn
        with patch.object(sidecar, 'open_reader', reader):
            self.export()
        self.assertEqual(sum(sql.startswith('SELECT') for sql in queries), 4)
        with patch.object(sidecar, 'open_reader', reader):
            queries.clear()
            sidecar.compare_curation_page(self.service.db_path, self.folder)
        self.assertEqual(sum(sql.startswith('SELECT') for sql in queries), 2)

    def test_failure_after_first_block_preserves_previous_complete_snapshot(self):
        self.register(tuple(f'{i}.png' for i in range(600)))
        self.export()
        previous = self.fingerprint()
        real = sidecar._relative
        calls = []
        def failing(path, folder):
            calls.append(path)
            if len(calls) == 300:
                raise ValueError('fixture_failure')
            return real(path, folder)
        with patch.object(sidecar, '_relative', failing):
            with self.assertRaisesRegex(ValueError, 'fixture_failure'):
                self.export()
        self.assertEqual(self.fingerprint(), previous)
        self.clean()

    def test_foreign_target_appearing_at_publication_is_not_overwritten(self):
        self.register()
        real = sidecar._publish_new
        def race(source, target):
            Path(target).write_bytes(b'foreign racing file')
            return real(source, target)
        with patch.object(sidecar, '_publish_new', race):
            with self.assertRaises(FileExistsError):
                self.export()
        self.assertEqual(self.target.read_bytes(), b'foreign racing file')
        self.clean()

    def test_source_hardlink_target_is_rejected_before_any_sidecar_write(self):
        os.link(self.service.db_path, self.target)
        with self.assertRaisesRegex(ValueError, 'source_target_alias'):
            self.export()
        self.assertTrue(os.path.samefile(self.service.db_path, self.target))
        self.assertEqual(self.service.read('count_files'), 0)

    def test_modified_hash_changes_are_reported_without_applying_tags(self):
        path = self.register()[0]
        self.export()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('UPDATE files SET hash_mod=? WHERE path=?', ('b' * 64, path))
            conn.commit()
        report = sidecar.compare_curation_page(self.service.db_path, self.folder)['reports'][0]
        self.assertEqual(report['status'], 'identity_changed')
        self.assertEqual(report['central']['hash_mod'], 'b' * 64)

    def test_read_page_payload_limit_fails_visibly_without_clipping(self):
        self.register()
        self.export()
        with closing(sqlite3.connect(self.target)) as conn:
            conn.executemany('INSERT INTO curation_tags(copy_id,name) VALUES(1,?)',
                             [(f'tag{i}',) for i in range(sidecar.MAX_PAGE_TAGS + 1)])
            conn.commit()
        with self.assertRaisesRegex(ValueError, 'sidecar_payload_too_large'):
            sidecar.read_curation_page(self.folder)

    def test_relative_traversal_in_central_aborts_export(self):
        path = self.register()[0]
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('UPDATE files SET path=? WHERE path=?',
                         (self.folder.as_posix() + '/../outside.png', path))
            conn.commit()
        with self.assertRaisesRegex(ValueError, 'invalid_relative_path'):
            self.export()
        self.assertFalse(self.target.exists())
        self.clean()

    def test_cli_export_and_compare_have_complete_reports_without_central_writes(self):
        self.register(('a.png', 'b.png'))
        script = Path(__file__).with_name('curation_sidecar.py')
        args = ['--db', self.service.db_path, '--folder', str(self.folder)]
        result = subprocess.run([sys.executable, str(script), 'export', *args],
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['copies'], 2)
        result = subprocess.run([sys.executable, str(script), 'compare', *args],
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row['status'] for row in records[:-1]], ['same', 'same'])
        self.assertTrue(records[-1]['report_complete'])
        self.assertEqual(records[-1]['copies'], 2)
        self.assertEqual(self.service.read('count_files'), 2)

    def test_multiple_missing_hash_copies_remain_distinct(self):
        self.register(('a.png', 'b.png', 'c.png'), digest=None)
        result = self.export()
        self.assertEqual((result['copies'], result['missing_hash']), (3, 3))
        self.assertEqual(len(sidecar.read_curation_page(self.folder)['copies']), 3)
        with closing(sqlite3.connect(self.target)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM files WHERE hash IS NULL').fetchone(), (3,))

    def test_cli_detects_missing_sidecar_rows_before_declaring_report_complete(self):
        self.register()
        self.export()
        with closing(sqlite3.connect(self.target)) as conn:
            conn.execute('DELETE FROM curation_copies')
            conn.commit()
        script = Path(__file__).with_name('curation_sidecar.py')
        result = subprocess.run([sys.executable, str(script), 'compare', '--db', self.service.db_path,
                                '--folder', str(self.folder)], capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('sidecar_count_mismatch', result.stderr)
        self.assertNotIn('report_complete', result.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
