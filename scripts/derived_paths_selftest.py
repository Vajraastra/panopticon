"""Caché del dueño: commits propios/externos, rollback, límites y presupuesto."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from derived_copies_selftest import Derived
from core.derived_paths import DerivedPaths
from core import metadata_destinations as limits
from core.library_service import LibraryError


class Paths(unittest.TestCase):
    setUp = Derived.setUp
    sql = Derived.sql
    state = Derived.state
    request = Derived.request
    prepare = Derived.prepare
    apply = Derived.apply
    receipt = Derived.receipt

    def approve(self, request):
        return self.service.submit('approve_derived_copy', self.key, **request).result(timeout=15)

    def trace(self):
        statements = []
        original = DerivedPaths.occupied
        def occupied(cache, aliases, check):
            cache.conn.set_trace_callback(statements.append)
            return original(cache, aliases, check)
        self.enterContext(patch.object(DerivedPaths, 'occupied', occupied))
        return statements

    def scans(self, statements):
        return sum(query == 'SELECT substr(path,1,4097) FROM files' for query in statements)

    def named(self, name):
        path = str(self.root/name)
        return self.request(path=path, destination_paths=[path],
                            observation=dict(filename=name,size=20,created=2))

    def test_reuses_one_scan_across_independent_commits_and_rejects_own_collision(self):
        self.prepare()
        trace = self.trace()
        for i in range(5):
            request = self.named(f'{i}.webp')
            self.assertTrue(self.approve(request))
            self.assertTrue(self.apply(request))
            self.assertTrue(self.apply(request))  # recibo histórico no altera caché.
        self.assertEqual(self.scans(trace),1)
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.approve(self.named('0.webp'))
        self.assertEqual(self.scans(trace),1)
        self.assertTrue(self.db.check_fts()['consistent'])

    def test_external_offline_unicode_alias_change_after_approval_is_seen(self):
        self.prepare()
        path = str(self.root/'Straße.webp').replace('\\','/')
        self.sql('INSERT INTO files(path,filename,is_offline) VALUES(?,?,1)',(path,'Straße.webp'))
        request = self.request(destination_paths=[self.output,str(self.root/'STRASSE.webp')])
        trace = self.trace()
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.approve(request)
        self.sql('DELETE FROM files WHERE path=?',(path,))
        self.approve(request)
        self.sql('INSERT INTO files(path,filename,is_offline) VALUES(?,?,1)',(path,'Straße.webp'))
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.apply(request)
        self.assertEqual(self.scans(trace),3)
        self.assertIsNone(self.receipt(request)['receipt'])

    def test_external_commit_during_postcommit_hook_cannot_be_acknowledged_as_cached(self):
        self.prepare()
        request = self.named('first.webp')
        second = self.named('second.webp')
        trace = self.trace()
        original = DerivedPaths.finish
        def finish(cache, committed, operation):
            if committed and operation == 'register_derived_copy':
                self.sql('INSERT INTO files(path,filename) VALUES(?,?)',
                         (second['path'].replace('\\','/'),'second.webp'))
            original(cache,committed,operation)
        with patch.object(DerivedPaths,'finish',finish):
            self.approve(request)
            self.apply(request)
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.approve(second)
        self.assertEqual(self.scans(trace),2)

    def test_other_owner_command_invalidates_without_losing_offline_path(self):
        self.prepare()
        trace = self.trace()
        request = self.request()
        self.approve(request)
        self.db.register_files_minimal([dict(path=self.output,filename='other',size=1,created=1)])
        self.sql('UPDATE files SET is_offline=1 WHERE id<>?',(self.key,))
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.apply(request)
        self.assertEqual(self.scans(trace),2)

    def test_failure_after_incremental_update_discards_cache_and_rolls_back(self):
        self.prepare()
        request = self.request()
        trace = self.trace()
        self.approve(request)
        original = DerivedPaths.inserted
        def inserted(cache,path):
            original(cache,path)
            raise ValueError('private_failure_after_update')
        with patch.object(DerivedPaths,'inserted',inserted):
            with self.assertRaisesRegex(LibraryError,'private_failure'):
                self.apply(request)
        self.assertIsNone(self.receipt(request)['receipt'])
        self.assertTrue(self.apply(request))
        self.assertEqual(self.scans(trace),2)
        self.assertTrue(self.db.check_fts()['consistent'])

    def test_arbitrary_trigger_effects_force_full_refresh(self):
        self.prepare()
        victim = str(self.root/'victim.webp').replace('\\','/')
        destination = str(self.root/'occupied.webp').replace('\\','/')
        self.sql('INSERT INTO files(path,filename) VALUES(?,?)',(victim,'victim.webp'))
        self.sql("CREATE TRIGGER private_effect AFTER INSERT ON files BEGIN "
                 f"UPDATE files SET path='{destination}' WHERE path='{victim}'; END")
        trace = self.trace()
        self.apply(self.named('first.webp'))
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.approve(self.named('occupied.webp'))
        self.assertEqual(self.scans(trace),2)

    def test_external_nonpath_commit_still_forces_refresh(self):
        self.prepare()
        trace = self.trace()
        request = self.request()
        self.approve(request)
        self.sql('INSERT INTO watched_folders(path) VALUES(?)',(str(self.root/'watch'),))
        self.approve(request)
        self.assertEqual(self.scans(trace),2)

    def test_caps_use_complete_historical_fallback_and_do_not_rebuild_prefix(self):
        self.prepare()
        occupied = str(self.root/'last.webp').replace('\\','/')
        self.sql('INSERT INTO files(path,filename,is_offline) VALUES(?,?,1)',(occupied,'last.webp'))
        trace = self.trace()
        for limit in ('MAX_ROWS','MAX_BYTES','MAX_KEY_MEMORY'):
            with self.subTest(limit=limit), patch.object(limits,limit,0):
                before = self.scans(trace)
                self.approve(self.named('absent.webp'))
                self.approve(self.named('absent.webp'))
                self.assertEqual(self.scans(trace)-before,1)
                with self.assertRaisesRegex(LibraryError,'destination_occupied'):
                    self.approve(self.named('last.webp'))
                # rollback al rechazar despeja la caché antes del subtest siguiente.

    def test_increment_crossing_row_cap_switches_to_full_fallback(self):
        self.prepare()
        with patch.object(limits,'MAX_ROWS',1):
            trace = self.trace()
            request = self.request()
            self.approve(request)
            self.apply(request)
            with self.assertRaisesRegex(LibraryError,'destination_occupied'):
                self.approve(self.request())
            self.assertEqual(self.scans(trace),1)

    def test_corrupt_path_cannot_be_hidden_in_prefix(self):
        self.prepare()
        self.sql('INSERT INTO files(path,filename) VALUES(?,?)',('x'*4097,'bad'))
        with self.assertRaisesRegex(LibraryError,'invalid_stored_metadata_path'):
            self.approve(self.request())
        self.assertIsNone(self.receipt(self.request())['receipt'])

    def test_python_scan_deadline_discards_partial_keys_and_owner_remains_usable(self):
        self.prepare()
        request = self.request()
        original = DerivedPaths.occupied
        def occupied(cache,aliases,check):
            calls = 0
            def budget():
                nonlocal calls
                calls += 1
                check()
                if calls == 2:
                    raise ValueError('derived_write_budget')
            return original(cache,aliases,budget)
        with patch.object(DerivedPaths,'occupied',occupied):
            with self.assertRaisesRegex(LibraryError,'derived_write_budget'):
                self.approve(request)
        self.assertTrue(self.approve(request))
        self.assertTrue(self.apply(request))

    def test_plain_repository_does_not_cache_and_cache_requires_transaction(self):
        from core.library_repository import LibraryRepository
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertIsNone(LibraryRepository(conn)._derived_paths)
            with self.assertRaisesRegex(RuntimeError,'requires_transaction'):
                DerivedPaths(conn).occupied(('absent',),lambda:None)

    def test_service_close_releases_keys_even_if_cache_is_still_referenced(self):
        self.prepare()
        captured = []
        original = DerivedPaths.occupied
        def occupied(cache,aliases,check):
            captured.append(cache)
            return original(cache,aliases,check)
        with patch.object(DerivedPaths,'occupied',occupied):
            self.approve(self.request())
        self.assertIsNotNone(captured[0].keys)
        self.service.close()
        self.assertIsNone(captured[0].keys)
        self.assertIsNone(captured[0].version)


if __name__ == '__main__':
    # No descubrir nuevamente los 30 casos importados del contrato.
    unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Paths)).wasSuccessful() or sys.exit(1)
