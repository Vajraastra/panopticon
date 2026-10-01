"""Private coverage caching with transactional changes from independent clients."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.library_service import LibraryService, LibraryError
from core.path_identity_clock import token, SQL, CoverageCache
from core.path_identity_registry import status, resolution


class Clock(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.service=LibraryService(self.root/'private.db')
        self.addCleanup(self.service.close)
        self.image=self.root/'One.png'
        self.image.write_bytes(b'private')
        self.service.submit('register_files_minimal',[dict(path=str(self.image),filename=self.image.name,size=7,created=1)]).result(3)
        self.service.backfill_path_identity()
        self.canonical=self.service.resolve_path_identity(str(self.image))['canonical']

    def foreign(self):
        return sqlite3.connect(self.service.db_path)

    def clock(self):
        with closing(self.foreign()) as conn:
            return token(conn)

    def test_100_warm_lookups_do_not_rescan_coverage_and_ratings_remain_live(self):
        # Persistent test connection makes trace statements inspectable; same
        # cache contract is used by service readers, which are short-lived.
        with closing(self.foreign()) as conn:
            trace=[]
            conn.set_trace_callback(trace.append)
            conn.execute('PRAGMA query_only=ON')
            cache=CoverageCache()
            for _ in range(100):
                conn.execute('BEGIN')
                result=resolution(conn,self.canonical,cache)
                self.assertEqual(result['status'],'unique')
                conn.rollback()
            self.assertEqual(sum('SUM(CASE WHEN r.file_id IS NULL' in q for q in trace),1)
            self.assertEqual(sum('GROUP BY r.canonical HAVING' in q for q in trace),1)
            before=token(conn)
            with closing(self.foreign()) as writer:
                writer.execute('UPDATE files SET rating=5')
                writer.commit()
            self.assertEqual(token(conn),before)
            conn.execute('BEGIN')
            current=resolution(conn,self.canonical,cache)
            conn.rollback()
            self.assertEqual(current['records'][0][2],5)
            self.assertEqual(sum('SUM(CASE WHEN r.file_id IS NULL' in q for q in trace),1)

    def test_foreign_insert_invalidates_coverage_immediately_without_owner_poll(self):
        self.assertEqual(self.service.read('get_path_identity_resolution',self.canonical)['status'],'unique')
        with closing(self.foreign()) as conn:
            conn.execute("INSERT INTO files(path,filename) VALUES('C:/private/new.png','new.png')")
            conn.commit()
        # No delay waiting for Native revision poll: trigger belongs to the commit.
        result=self.service.read('get_path_identity_resolution',self.canonical)
        self.assertEqual(result['status'],'incomplete')
        self.assertIsNone(result['file_id'])
        self.assertEqual(result['coverage']['pending'],1)

    def test_foreign_path_and_offline_changes_remove_old_matches(self):
        for expression in ("path=path||'.new'", 'is_offline=1'):
            with self.subTest(expression=expression), closing(self.foreign()) as conn:
                before=token(conn)
                conn.execute('BEGIN')
                conn.execute('UPDATE files SET '+expression)
                self.assertNotEqual(token(conn),before)
                self.assertEqual(resolution(conn,self.canonical,self.service._path_coverage)['records'],[])
                conn.rollback()
                self.assertEqual(token(conn),before)
        self.assertEqual(self.service.read('get_path_identity_resolution',self.canonical)['status'],'unique')

    def test_foreign_delete_does_not_reuse_cached_total(self):
        before=self.clock()
        with closing(self.foreign()) as conn:
            conn.execute('DELETE FROM files')
            conn.commit()
        self.assertNotEqual(self.clock(),before)
        self.assertEqual(self.service.read('get_path_identity_status')['total'],0)

    def test_registry_updates_and_deletes_invalidate_cache(self):
        before=self.clock()
        with closing(self.foreign()) as conn:
            conn.execute("UPDATE path_identity_records SET canonical='C:/private/different.png'")
            conn.commit()
        self.assertNotEqual(self.clock(),before)
        self.assertEqual(self.service.read('get_path_identity_resolution',self.canonical)['status'],'missing')
        with closing(self.foreign()) as conn:
            conn.execute('DELETE FROM path_identity_records')
            conn.commit()
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],1)

    def test_rolled_back_registry_and_file_changes_do_not_advance_token(self):
        before=self.clock()
        with closing(self.foreign()) as conn:
            conn.execute('BEGIN')
            conn.execute("UPDATE files SET path=path||'.private'")
            conn.execute('DELETE FROM path_identity_records')
            self.assertNotEqual(token(conn),before)
            conn.rollback()
        self.assertEqual(self.clock(),before)
        self.assertEqual(self.service.read('get_path_identity_resolution',self.canonical)['status'],'unique')

    def test_unchanged_file_binding_and_local_curation_retain_token(self):
        before=self.clock()
        with closing(self.foreign()) as conn:
            conn.execute('UPDATE files SET path=path,is_offline=is_offline')
            conn.commit()
        self.service.submit('edit_curation',str(self.image),rating=5,add_tags=['keep']).result(3)
        self.assertEqual(self.clock(),before)
        self.assertEqual(self.service.read('get_path_identity_resolution',self.canonical)['records'][0][2],5)

    def test_old_snapshot_and_new_reader_each_use_their_transaction_token(self):
        with closing(self.foreign()) as old:
            old.execute('PRAGMA query_only=ON')
            old.execute('BEGIN')
            initial=resolution(old,self.canonical,self.service._path_coverage)
            with closing(self.foreign()) as writer:
                writer.execute("INSERT INTO files(path,filename) VALUES('C:/private/new.png','new.png')")
                writer.commit()
            fresh=self.service.read('get_path_identity_resolution',self.canonical)
            self.assertEqual(fresh['status'],'incomplete')
            self.assertEqual(resolution(old,self.canonical,self.service._path_coverage),initial)
            old.rollback()
        self.assertEqual(self.service.read('get_path_identity_resolution',self.canonical)['status'],'incomplete')

    def test_cache_returns_copies_and_holds_only_one_key(self):
        cache=CoverageCache()
        for i in range(1000):
            cache.put(('private',i),dict(pending=i))
        value=cache.get(('private',999))
        value['pending']=0
        self.assertEqual(cache.get(('private',999)),dict(pending=999))
        self.assertIsNone(cache.get(('private',998)))
        self.assertIsNone(cache.get(None))
        self.assertEqual(set(vars(cache)),{'_lock','_key','_value'})

    def test_missing_or_changed_owned_trigger_rejects_warm_cache(self):
        self.service.read('get_path_identity_resolution',self.canonical)
        with closing(self.foreign()) as conn:
            conn.execute('DROP TRIGGER pi_files_insert')
            conn.commit()
        with self.assertRaisesRegex(ValueError,'path_identity_unknown_clock'):
            self.service.read('get_path_identity_resolution',self.canonical)
        with self.assertRaisesRegex(LibraryError,'path_identity_unknown_clock'):
            self.service.submit('prepare_path_identity').result(3)

    def test_old_registry_without_clock_works_uncached_then_prepare_backs_it_up(self):
        with closing(self.foreign()) as conn:
            for name in SQL:
                conn.execute('DROP TRIGGER '+name)
            conn.execute('DROP TABLE path_identity_clock')
            conn.execute("DELETE FROM panopticon_schema WHERE name='path_identity_clock_v1'")
            conn.commit()
        before=list(self.root.glob('*.pre-path-identity-*.bak'))
        result=self.service.read('get_path_identity_resolution',self.canonical)
        self.assertEqual(result['status'],'unique')
        self.assertTrue(self.service.submit('prepare_path_identity').result(3))
        after=list(self.root.glob('*.pre-path-identity-*.bak'))
        self.assertEqual(len(after),len(before)+1)
        added=next(p for p in after if p not in before)
        with closing(sqlite3.connect(added)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='path_identity_clock'").fetchone())
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM path_identity_records').fetchone(),(1,))
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone(),('ok',))

    def test_replaced_nonce_invalidates_cache_and_invalid_clock_rejects(self):
        from uuid import uuid4
        before=self.clock()
        with closing(self.foreign()) as conn:
            conn.execute('UPDATE path_identity_clock SET nonce=?',(str(uuid4()),))
            conn.commit()
        self.assertNotEqual(self.clock(),before)
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)
        with closing(self.foreign()) as conn:
            conn.execute("UPDATE path_identity_clock SET nonce='invalid'")
            conn.commit()
        with self.assertRaisesRegex(ValueError,'path_identity_invalid_clock'):
            self.service.read('get_path_identity_status')

    def test_rollback_reused_epoch_cannot_poison_shared_cache(self):
        cache=self.service._path_coverage
        initial=cache._key
        with closing(self.foreign()) as writer:
            writer.execute('BEGIN')
            writer.execute("INSERT INTO files(path,filename) VALUES('C:/private/rollback.png','rollback.png')")
            future=token(writer)
            self.assertEqual(status(writer,cache)['total'],2)
            self.assertEqual(cache._key,initial)
            writer.rollback()
            writer.execute('DELETE FROM path_identity_records')
            writer.commit()
            self.assertEqual(token(writer),future)
        result=self.service.read('get_path_identity_status')
        self.assertEqual(result['total'],1)
        self.assertEqual(result['pending'],1)

    def test_autocommit_or_dirty_reader_does_not_populate_shared_cache(self):
        with closing(self.foreign()) as reader:
            cache=CoverageCache()
            reader.execute('PRAGMA query_only=ON')
            status(reader,cache)
            self.assertIsNone(cache._key)
            reader.execute('PRAGMA read_uncommitted=ON')
            reader.execute('BEGIN')
            status(reader,cache)
            self.assertIsNone(cache._key)
            reader.rollback()


if __name__=='__main__':
    unittest.main(verbosity=2)
