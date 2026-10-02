"""Private additive migration, collision review, CAS and physical rejection."""
from contextlib import closing
from copy import deepcopy
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.library_service import LibraryService, LibraryError


class Registry(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = LibraryService(self.root/'private.db')
        self.addCleanup(self.service.close)

    def add(self, name):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'private')
        self.register([path])
        return path

    def register(self, paths):
        self.service.submit('register_files_minimal', [dict(path=str(p), filename=Path(p).name,
            size=7, created=1, hash_original='a'*64) for p in paths]).result(3)

    def prepare(self):
        self.assertTrue(self.service.submit('prepare_path_identity').result(3))

    def raw(self, page):
        keys = ('file_id','stored_path','status','canonical','physical','reason')
        return [dict(**{k:r[k] for k in keys}, is_offline=int(r['status']=='offline')) for r in page['records']]

    def test_prior_owned_registry_adds_physical_index_only_with_backup(self):
        image=self.add('one.png')
        self.service.backfill_path_identity()
        before=self.service.read('get_path_identity_status')
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('DROP INDEX idx_path_identity_physical')
            conn.commit()
        previous=set(self.root.glob('private.db.pre-path-identity-*.bak'))
        self.assertEqual(self.service.read('get_path_identity_status'),before)
        self.prepare()
        new=set(self.root.glob('private.db.pre-path-identity-*.bak'))-previous
        self.assertEqual(len(new),1)
        with closing(sqlite3.connect(next(iter(new)))) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_path_identity_physical'").fetchone())
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone(),('ok',))
        self.assertEqual(self.service.read('get_path_identity_status'),before)
        self.prepare()
        self.assertEqual(set(self.root.glob('private.db.pre-path-identity-*.bak')),previous|new)

    def test_foreign_physical_index_is_rejected_without_overwriting(self):
        self.add('one.png')
        self.prepare()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('DROP INDEX idx_path_identity_physical')
            conn.execute('CREATE INDEX idx_path_identity_physical ON path_identity_records(stored_path)')
            conn.commit()
        with self.assertRaisesRegex(ValueError,'unknown_schema'):
            self.service.read('get_path_identity_status')
        with self.assertRaisesRegex(LibraryError,'unknown_schema'):
            self.prepare()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT sql FROM sqlite_master WHERE name='idx_path_identity_physical'").fetchone(),
                ('CREATE INDEX idx_path_identity_physical ON path_identity_records(stored_path)',))

    def test_physical_search_uses_index_for_bounded_batch(self):
        self.add('one.png')
        self.service.backfill_path_identity()
        from core.path_identity_registry import PHYSICAL_COLUMNS
        columns=[part.replace('physical','r.physical') for part in PHYSICAL_COLUMNS]
        equal=' AND '.join(column+'=w.column'+str(i+1) for i,column in enumerate(columns))
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            query='SELECT r.file_id FROM (VALUES '+','.join('(?,?,?)' for _ in range(100))+') w CROSS JOIN path_identity_records r WHERE '+equal
            plan=conn.execute('EXPLAIN QUERY PLAN '+query,[0]*300).fetchall()
            self.assertTrue(any('SEARCH r USING ' in row[3] and 'INDEX idx_path_identity_physical' in row[3] for row in plan),plan)
            self.assertFalse(any('SCAN r' in row[3] for row in plan),plan)

    def test_backup_consistent_before_additive_schema_and_idempotent(self):
        image = self.add('one.png')
        file_id = self.service.read('get_file_id', str(image))
        self.service.submit('edit_curation',str(image),rating=5,add_tags=['keep']).result(3)
        self.assertFalse(self.service.read('get_path_identity_status')['prepared'])
        self.prepare()
        backups = list(self.root.glob('private.db.pre-path-identity-*.bak'))
        self.assertEqual(len(backups),1)
        with closing(sqlite3.connect(backups[0])) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='path_identity_records'").fetchone())
            self.assertEqual(conn.execute('SELECT id,path,rating FROM files').fetchone(), (file_id,str(image).replace('\\','/'),5))
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone(),('ok',))
        token = self.service.revision_snapshot()
        self.prepare()
        self.assertEqual(list(self.root.glob('private.db.pre-path-identity-*.bak')),backups)
        self.assertEqual(self.service.revision_snapshot(),token)

    def test_unicode_legacy_collision_preserves_every_id_and_curation(self):
        image = self.add('Árbol/Mixed.png')
        alias = self.root/'árbol'/'mixed.png'
        self.register([alias])
        ids = [self.service.read('get_file_id',str(p)) for p in (image,alias)]
        self.service.submit('edit_curation',str(image),rating=5,add_tags=['keep']).result(3)
        self.prepare()
        page = self.service.inspect_path_identity_page()
        self.assertTrue(self.service.publish_path_identity_page(page))
        result = self.service.read('get_path_identity_matches',page['records'][0]['canonical'])
        self.assertTrue(result['ambiguous'])
        self.assertEqual([r[0] for r in result['records']],ids)
        self.assertEqual([r[2] for r in result['records']],[5,0])
        self.assertEqual(self.service.read('get_tags_for_file',str(image)),['keep'])
        status = self.service.read('get_path_identity_status')
        self.assertEqual(status,dict(prepared=True,total=2,pending=0,unresolved=0,conflict_groups=1))
        self.assertEqual(self.service.read('check_fts')['stale'],0)

    def test_hardlinks_have_distinct_keys(self):
        first = self.add('First.png')
        second = self.root/'Second.png'
        os.link(first,second)
        self.register([second])
        self.prepare()
        page = self.service.inspect_path_identity_page()
        self.service.publish_path_identity_page(page)
        self.assertNotEqual(*[r['canonical'] for r in page['records']])
        for row in page['records']:
            result = self.service.read('get_path_identity_matches',row['canonical'])
            self.assertEqual([r[0] for r in result['records']],[row['file_id']])

    def test_repeat_publication_does_not_advance_revision(self):
        self.add('one.png')
        self.prepare()
        self.service.publish_path_identity_page(self.service.inspect_path_identity_page())
        token = self.service.revision_snapshot()
        self.service.publish_path_identity_page(self.service.inspect_path_identity_page())
        self.assertEqual(self.service.revision_snapshot(),token)

    def test_physical_change_rejects_entire_page_without_sql(self):
        self.add('one.png')
        second = self.add('two.png')
        self.prepare()
        page = self.service.inspect_path_identity_page()
        second.write_bytes(b'changed private bytes')
        with self.assertRaisesRegex(ValueError,'path_identity_physical_state_changed'):
            self.service.publish_path_identity_page(page)
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],2)

    def test_sql_path_change_rejects_entire_batch(self):
        self.add('one.png')
        second = self.add('two.png')
        self.prepare()
        page = self.service.inspect_path_identity_page()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute("UPDATE files SET path=path||'.changed' WHERE id=?",(page['records'][1]['file_id'],))
            conn.commit()
        with self.assertRaisesRegex(LibraryError,'path_identity_sql_state_changed'):
            self.service.submit('store_path_identity_batch',self.raw(page)).result(3)
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],2)

    def test_sql_curation_change_is_preserved_by_key_only_publication(self):
        image = self.add('one.png')
        self.prepare()
        page = self.service.inspect_path_identity_page()
        self.service.submit('edit_curation',str(image),rating=5,add_tags=['keep']).result(3)
        # Runtime publisher rejects stale Native revision; SQL command's CAS
        # preserves independent curation even if that change occurs after admission.
        with self.assertRaisesRegex(ValueError,'path_identity_preview_stale'):
            self.service.publish_path_identity_page(page)
        self.service.submit('store_path_identity_batch',self.raw(page)).result(3)
        self.assertEqual(self.service.read('get_file_rating',str(image)),5)
        self.assertEqual(self.service.read('get_tags_for_file',str(image)),['keep'])

    def test_offline_remains_unresolved_even_with_an_existing_file(self):
        image = self.add('one.png')
        file_id = self.service.read('get_file_id',str(image))
        self.service.submit('remove_files',[str(image)]).result(3)
        self.prepare()
        page = self.service.inspect_path_identity_page()
        self.service.publish_path_identity_page(page)
        self.assertTrue(image.exists())
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT file_id,status,canonical FROM path_identity_records').fetchone(),(file_id,'offline',None))
            self.assertEqual(conn.execute('SELECT is_offline FROM files').fetchone(),(1,))

    def test_missing_file_has_no_guessed_key(self):
        image = self.add('one.png')
        image.unlink()
        self.prepare()
        self.service.publish_path_identity_page(self.service.inspect_path_identity_page())
        self.assertEqual(self.service.read('get_path_identity_status')['unresolved'],1)

    def test_binding_change_invalidates_old_match_without_merging(self):
        image = self.add('one.png')
        self.prepare()
        page = self.service.inspect_path_identity_page()
        self.service.publish_path_identity_page(page)
        self.service.submit('remove_files',[str(image)]).result(3)
        self.assertEqual(self.service.read('get_path_identity_matches',page['records'][0]['canonical'])['records'],[])
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)
        self.assertEqual(self.service.read('get_path_identity_records')['records'][0][2],1)

    def test_caps_duplicate_and_incomplete_record_reject_atomically(self):
        self.add('one.png')
        self.prepare()
        rows = self.raw(self.service.inspect_path_identity_page())
        for bad in ([*rows,*rows],rows*101,[dict(rows[0],physical=(1,2))],[dict(rows[0],canonical=None)]):
            with self.assertRaises(LibraryError):
                self.service.submit('store_path_identity_batch',bad).result(3)
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],1)

    def test_match_cap_returns_error_instead_of_approved_prefix(self):
        self.add('one.png')
        self.prepare()
        row = self.raw(self.service.inspect_path_identity_page())[0]
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            for i in range(3):
                cursor=conn.execute('INSERT INTO files(path,filename) VALUES(?,?)',(f'C:/private/{i}.png',f'{i}.png'))
                conn.execute('INSERT INTO path_identity_records VALUES(?,?,?,?,?,?,?)',(
                    cursor.lastrowid,f'C:/private/{i}.png',0,'resolved',row['canonical'],'[1,2,3,4,5,6,7]',None))
            conn.commit()
        with self.assertRaisesRegex(LibraryError,'path_identity_matches_too_large'):
            self.service.read('get_path_identity_matches',row['canonical'],limit=2)

    def test_cancel_before_admission_keeps_pending_and_releases_handles(self):
        image = self.add('one.png')
        self.prepare()
        page = self.service.inspect_path_identity_page()
        event = threading.Event()
        event.set()
        with self.assertRaisesRegex(ValueError,'identity_cancelled'):
            self.service.publish_path_identity_page(page,cancel_event=event)
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],1)
        image.rename(self.root/'renamed.png')

    def test_backup_failure_does_not_create_schema_or_stop_owner(self):
        self.add('one.png')
        with patch('core.library_service.backup_database',side_effect=OSError('private backup failed')):
            with self.assertRaisesRegex(LibraryError,'private backup failed'):
                self.prepare()
        self.assertFalse(self.service.read('get_path_identity_status')['prepared'])
        self.prepare()

    def test_unknown_marker_not_overwritten(self):
        self.prepare()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute("UPDATE panopticon_schema SET value='99' WHERE name='path_identity_v1'")
            conn.commit()
        with self.assertRaisesRegex(LibraryError,'path_identity_unknown_schema'):
            self.prepare()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM panopticon_schema WHERE name='path_identity_v1'").fetchone(),('99',))

    def test_changed_index_collation_requires_review(self):
        self.prepare()
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('DROP INDEX idx_path_identity_canonical')
            conn.execute('CREATE INDEX idx_path_identity_canonical ON path_identity_records(canonical COLLATE NOCASE,file_id)')
            conn.commit()
        with self.assertRaisesRegex(LibraryError,'path_identity_unknown_schema'):
            self.prepare()


if __name__ == '__main__':
    unittest.main(verbosity=2)
