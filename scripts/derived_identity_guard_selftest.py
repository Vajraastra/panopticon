"""Owned identity triggers retain warm producer guards; foreigners stay unsafe."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from derived_paths_selftest import Paths
from core.derived_paths import DerivedPaths
from core.library_service import LibraryError


class OwnedGuards(unittest.TestCase):
    setUp=Paths.setUp
    sql=Paths.sql
    state=Paths.state
    request=Paths.request
    prepare=Paths.prepare
    apply=Paths.apply
    receipt=Paths.receipt
    approve=Paths.approve
    trace=Paths.trace
    scans=Paths.scans
    named=Paths.named

    def identity(self):
        self.service.submit('prepare_path_identity').result(3)

    def test_owned_clock_keeps_one_scan_for_five_outputs_and_rejects_collision(self):
        self.prepare()
        self.identity()
        trace=self.trace()
        for i in range(5):
            request=self.named(f'{i}.webp')
            self.assertTrue(self.approve(request))
            self.assertTrue(self.apply(request))
        self.assertEqual(self.scans(trace),1)
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.approve(self.named('0.webp'))
        self.assertEqual(self.scans(trace),1)
        self.assertTrue(self.db.check_fts()['consistent'])

    def test_foreign_trigger_with_owned_clock_still_refreshes_all_paths(self):
        self.prepare()
        self.identity()
        victim=str(self.root/'victim.webp').replace('\\','/')
        occupied=str(self.root/'occupied.webp').replace('\\','/')
        self.sql('INSERT INTO files(path,filename) VALUES(?,?)',(victim,'victim.webp'))
        self.sql("CREATE TRIGGER private_effect AFTER INSERT ON files BEGIN "
                 f"UPDATE files SET path='{occupied}' WHERE path='{victim}'; END")
        trace=self.trace()
        self.apply(self.named('first.webp'))
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.approve(self.named('occupied.webp'))
        self.assertEqual(self.scans(trace),2)

    def test_altered_owned_name_cannot_hide_path_side_effects(self):
        self.prepare()
        self.identity()
        victim=str(self.root/'victim.webp').replace('\\','/')
        occupied=str(self.root/'occupied.webp').replace('\\','/')
        self.sql('INSERT INTO files(path,filename) VALUES(?,?)',(victim,'victim.webp'))
        self.sql('DROP TRIGGER pi_files_insert')
        self.sql("CREATE TRIGGER pi_files_insert AFTER INSERT ON files BEGIN "
                 f"UPDATE files SET path='{occupied}' WHERE path='{victim}'; END")
        trace=self.trace()
        self.apply(self.named('first.webp'))
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.approve(self.named('occupied.webp'))
        self.assertEqual(self.scans(trace),2)

    def test_foreign_postcommit_does_not_get_acknowledged_by_owned_clock(self):
        self.prepare()
        self.identity()
        first,second=self.named('first.webp'),self.named('second.webp')
        trace=self.trace()
        original=DerivedPaths.finish
        def finish(cache,committed,operation):
            if committed and operation=='register_derived_copy':
                self.sql('INSERT INTO files(path,filename) VALUES(?,?)',
                         (second['path'].replace('\\','/'),'second.webp'))
            original(cache,committed,operation)
        with patch.object(DerivedPaths,'finish',finish):
            self.approve(first)
            self.apply(first)
        with self.assertRaisesRegex(LibraryError,'destination_occupied'):
            self.approve(second)
        self.assertEqual(self.scans(trace),2)

    def test_failed_registration_discards_incremental_cache_with_clock(self):
        self.prepare()
        self.identity()
        request=self.request()
        trace=self.trace()
        self.approve(request)
        original=DerivedPaths.inserted
        def inserted(cache,path):
            original(cache,path)
            raise ValueError('private_after_guard')
        with patch.object(DerivedPaths,'inserted',inserted):
            with self.assertRaisesRegex(LibraryError,'private_after_guard'):
                self.apply(request)
        self.assertIsNone(self.receipt(request)['receipt'])
        self.apply(request)
        self.assertEqual(self.scans(trace),2)

    def test_temporary_trigger_forces_clear_even_with_only_native_persistent_guards(self):
        self.prepare()
        self.identity()
        trace=self.trace()
        original=DerivedPaths.inserted
        def inserted(cache,path):
            cache.conn.execute('CREATE TEMP TRIGGER private_effect AFTER INSERT ON files BEGIN SELECT 1; END')
            try:
                original(cache,path)
            finally:
                cache.conn.execute('DROP TRIGGER private_effect')
        with patch.object(DerivedPaths,'inserted',inserted):
            self.apply(self.named('first.webp'))
        self.approve(self.named('second.webp'))
        self.assertEqual(self.scans(trace),2)


if __name__=='__main__':
    unittest.main(verbosity=2)
