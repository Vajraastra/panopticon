"""SQL-only Tagger copy contract; private DB, no real files/catalogs/models."""
from pathlib import Path
from contextlib import closing
import sys
import sqlite3
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import derived_copies_selftest as fixture
from core.library_service import LibraryError


class TaggerNative(unittest.TestCase):
    setUp = fixture.Derived.setUp
    sql = fixture.Derived.sql
    state = fixture.Derived.state
    request = fixture.Derived.request
    prepare = fixture.Derived.prepare
    apply = fixture.Derived.apply
    receipt = fixture.Derived.receipt

    def exact_copy(self):
        self.prepare()
        value = self.request(producer='dataset_tagger', result_digest='a'*64)
        self.assertTrue(self.apply(value))
        return value, self.receipt(value)['receipt']['copy_id']

    def test_identical_copy_keeps_initial_curation_and_provenance(self):
        before = self.state()
        _, key = self.exact_copy()
        new = self.state(key)['state']
        old = before['state']
        self.assertNotEqual(key, self.key)
        for name in ('rating', 'origin_url', 'artist', 'hash_original', 'hash_mod'):
            self.assertEqual(new['file'][name], old['file'][name], name)
        self.assertEqual(new['tags'], old['tags'])
        self.assertEqual(self.state(), before)
        self.assertTrue(self.db.check_fts()['consistent'])

    def test_curation_is_independent_and_acknowledgement_does_not_reapply(self):
        value, key = self.exact_copy()
        self.db.edit_curation(self.output, add_tags=['only copy'], rating=2)
        before = self.state(key)
        self.assertTrue(self.apply(value))
        self.assertEqual(self.state(key), before)
        self.assertEqual(self.state()['state']['file']['rating'], 5)
        self.assertNotEqual(self.state()['state']['tags'], self.state(key)['state']['tags'])

    def test_modified_bytes_cannot_claim_exact_copy_curation(self):
        self.prepare()
        before = self.sql('SELECT * FROM files ORDER BY id')
        with self.assertRaisesRegex(LibraryError, 'identical_bytes'):
            self.apply(self.request(producer='dataset_tagger'))
        self.assertEqual(self.sql('SELECT * FROM files ORDER BY id'), before)

    def test_composed_derivative_starts_with_independent_empty_curation(self):
        self.prepare()
        value = self.request(producer='ideogram4')
        self.assertTrue(self.apply(value))
        key = self.receipt(value)['receipt']['copy_id']
        self.assertEqual(self.state(key)['state']['tags'], [])
        self.assertEqual(self.state(key)['state']['file']['rating'], 0)
        self.assertEqual(self.state()['state']['file']['rating'], 5)

    def test_changed_source_curation_rejects_stale_copy(self):
        self.prepare()
        value = self.request(producer='dataset_tagger', result_digest='a'*64)
        self.db.edit_curation(self.source, rating=1)
        before = self.sql('SELECT * FROM files ORDER BY id')
        with self.assertRaisesRegex(LibraryError, 'changed_since_review'):
            self.apply(value)
        self.assertEqual(self.sql('SELECT * FROM files ORDER BY id'), before)

    def test_identical_copy_preserves_download_identity_after_stamping(self):
        # Private fixture: simulate the independently tested Metadata Hub update.
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('UPDATE files SET hash_mod=? WHERE id=?', ('b'*64, self.key))
            conn.commit()
        self.prepare()
        value = self.request(producer='dataset_tagger', source_digest='b'*64, result_digest='b'*64)
        self.assertTrue(self.apply(value))
        key = self.receipt(value)['receipt']['copy_id']
        copy = self.state(key)['state']['file']
        self.assertEqual(copy['hash_original'], 'a'*64)
        self.assertEqual(copy['hash_mod'], 'b'*64)


if __name__ == '__main__':
    unittest.main()
