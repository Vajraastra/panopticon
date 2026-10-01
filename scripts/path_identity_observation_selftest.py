"""Private Windows name observations, bounded SQL pages and concurrency."""
from contextlib import closing
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.path_identity import PathIdentity, local_path, observe_path
from core.library_service import LibraryService, LibraryError


class Observations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_ascii_unicode_aliases_use_windows_spelling(self):
        folder = self.root/'Árbol'
        folder.mkdir()
        image = folder/'Mixed.png'
        image.write_bytes(b'private')
        a, b = observe_path(str(image)), observe_path(str(self.root/'árbol'/'mixed.png'))
        self.assertEqual(a.status, 'resolved')
        self.assertEqual(b.status, 'resolved')
        self.assertEqual(a.canonical, b.canonical)
        self.assertTrue(a.canonical.endswith('/Árbol/Mixed.png'))
        self.assertEqual(a.physical, b.physical)

    def test_hardlinks_keep_distinct_copy_names(self):
        a, b = self.root/'First.png', self.root/'Second.png'
        a.write_bytes(b'private')
        os.link(a, b)
        first, second = observe_path(str(a)), observe_path(str(b))
        self.assertEqual(first.status, second.status, 'resolved')
        self.assertEqual(first.physical[:3], second.physical[:3])
        self.assertNotEqual(first.canonical, second.canonical)
        self.assertTrue(second.canonical.endswith('/Second.png'))

    def test_directory_observation_and_handle_release(self):
        folder = self.root/'Directory'
        folder.mkdir()
        result = observe_path(str(folder))
        self.assertEqual((result.status, result.kind), ('resolved', 'directory'))
        image = folder/'image.png'
        image.write_bytes(b'private')
        observe_path(str(image))
        image.rename(folder/'moved.png')
        (folder/'moved.png').unlink()
        folder.rmdir()

    def test_unknown_does_not_guess_key_or_cache_a_previous_observation(self):
        image = self.root/'image.png'
        image.write_bytes(b'private')
        self.assertEqual(observe_path(str(image)).status, 'resolved')
        image.unlink()
        missing = observe_path(str(image))
        self.assertEqual(missing.status, 'unavailable')
        self.assertIsNone(missing.canonical)
        self.assertIsNone(missing.physical)

    def test_invalid_spelling_rejected_before_disk(self):
        with patch('core.path_identity._windows_observe', side_effect=AssertionError('disk')):
            for value in ('', 'relative.png', 'C:image.png', 'C:/a/../b',
                          'C:/a./b', 'C:/a /b', 'C:/a/b:stream', 'C:/a/?.png',
                          '\\\\server\\share\\file', '\\\\?\\C:\\a', 'C:/a\0b', 'C:/'+'a'*4096):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    observe_path(value)
        self.assertEqual(local_path('c:\\Árbol\\file.png'), 'c:/Árbol/file.png')

    def test_cooperative_cancel_before_and_after_io(self):
        event = threading.Event()
        event.set()
        with patch('core.path_identity._windows_observe', side_effect=AssertionError('disk')):
            with self.assertRaisesRegex(ValueError, 'identity_cancelled'):
                observe_path('C:/private.png', cancel_event=event)
        event.clear()
        def io(value):
            event.set()
            return value, 'file', (1, 2, 3)
        with patch('core.path_identity._windows_observe', side_effect=io):
            with self.assertRaisesRegex(ValueError, 'identity_cancelled'):
                observe_path('C:/private.png', cancel_event=event)

    def test_sensitive_spelling_is_not_folded_in_observed_results(self):
        # Adapter contract, not a claim that the private directory has its flag set.
        with patch('core.path_identity._windows_observe', side_effect=lambda p: (p, 'file', (1, 2, 3))):
            a, b = observe_path('C:/Sensitive/A.png'), observe_path('C:/Sensitive/a.png')
        self.assertNotEqual(a.canonical, b.canonical)
        self.assertEqual(a.physical, b.physical)
        with self.assertRaises(FrozenInstanceError):
            a.canonical = b.canonical

    def test_changed_binding_and_reparse_remain_unresolved(self):
        for reason, status in [('identity_path_changed', 'changed'), ('identity_reparse_path', 'unsupported')]:
            with patch('core.path_identity._windows_observe', side_effect=ValueError(reason)):
                result = observe_path('C:/private.png')
            self.assertEqual(result.status, status)
            self.assertIsNone(result.canonical)

    def test_handle_closed_on_name_failure(self):
        image = self.root/'image.png'
        image.write_bytes(b'private')
        # First chain is real; fail after Windows opens its shared handle.
        import core.path_identity as identity
        original = identity._chain
        count = 0
        def chain(value):
            nonlocal count
            count += 1
            if count > 1:
                raise OSError('injected failure after opening')
            return original(value)
        with patch('core.path_identity._chain', side_effect=chain):
            self.assertEqual(observe_path(str(image)).status, 'unavailable')
        image.rename(self.root/'moved.png')


class NativePages(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = LibraryService(self.root/'private.db')
        self.addCleanup(self.service.close)

    def register(self, paths):
        self.service.submit('register_files_minimal', [dict(path=path, filename=Path(path).name,
                            size=1, created=1) for path in paths]).result(3)

    def test_keyset_pages_fixed_upper_bound_with_signed_ids(self):
        self.register([f'C:/private/{i}.png' for i in range(205)])
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute("INSERT INTO files(id,path,filename) VALUES(-2,'C:/private/negative.png','negative.png')")
            conn.execute("INSERT INTO files(id,path,filename) VALUES(0,'C:/private/zero.png','zero.png')")
            conn.commit()
        pages = []
        page = self.service.read('get_path_identity_records')
        upper = page['upper_id']
        self.register(['C:/private/new.png'])
        while True:
            pages.append(page)
            if not page['more']:
                break
            page = self.service.read('get_path_identity_records', after_id=page['after_id'], upper_id=upper)
        self.assertEqual([len(p['records']) for p in pages], [100, 100, 7])
        ids = [row[0] for p in pages for row in p['records']]
        self.assertEqual(ids[:2], [-2, 0])
        self.assertEqual(len(set(ids)), 207)
        self.assertEqual(ids[-1], upper)

    def test_probe_has_no_open_sql_read_and_writer_progresses(self):
        self.register(['C:/private/one.png'])
        def probe(path, **kwargs):
            self.assertEqual(self.service.metrics_snapshot()['reads_active'], 0)
            self.service.submit('edit_curation', path, rating=5, add_tags=['keep']).result(2)
            return PathIdentity(path, 'resolved', path, 'file', (1, 2, 3))
        with patch('core.library_path_identity.observe_path', side_effect=probe):
            page = self.service.inspect_path_identity_page()
        self.assertTrue(page['stale'])
        self.assertEqual(len(page['records']), 1)
        self.assertEqual(self.service.read('get_file_rating', 'C:/private/one.png'), 5)

    def test_offline_rows_keep_id_history_without_disk_probe(self):
        path = 'C:/private/offline.png'
        self.register([path])
        saved_id = self.service.read('get_file_id', path)
        self.service.submit('edit_curation', path, rating=5, add_tags=['keep']).result(3)
        self.service.submit('remove_files', [path]).result(3)
        with patch('core.library_path_identity.observe_path', side_effect=AssertionError('offline disk')):
            page = self.service.inspect_path_identity_page()
        self.assertEqual(page['records'][0]['file_id'], saved_id)
        self.assertEqual(page['records'][0]['status'], 'offline')
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT rating,is_offline FROM files WHERE id=?',(saved_id,)).fetchone(), (5, 1))

    def test_invalid_page_cancel_and_close_reject_without_probe(self):
        self.register(['C:/private/one.png'])
        with patch('core.library_path_identity.observe_path', side_effect=AssertionError('disk')):
            for kwargs in ({'limit': 101}, {'limit': True}, {'after_id': True}, {'upper_id': 2**63}):
                with self.assertRaises(LibraryError):
                    self.service.inspect_path_identity_page(**kwargs)
            event = threading.Event()
            event.set()
            with self.assertRaises(LibraryError):
                self.service.inspect_path_identity_page(cancel_event=event)
            self.service.close()
            with self.assertRaises(LibraryError):
                self.service.inspect_path_identity_page()

    def test_empty_page_and_invalid_legacy_record_visible(self):
        self.assertEqual(self.service.inspect_path_identity_page()['records'], [])
        self.register(['relative.png'])
        page = self.service.inspect_path_identity_page()
        self.assertEqual(page['records'][0]['status'], 'unsupported')
        self.assertIsNone(page['records'][0]['canonical'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
