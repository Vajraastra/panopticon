"""Evidence for path policy on private Windows files and temporary Native DBs."""
from contextlib import closing
from pathlib import Path
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.library_service import LibraryService


class PathPolicy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = LibraryService(self.root/'private.db')
        self.addCleanup(self.service.close)

    def register(self,*paths):
        return self.service.submit('register_files_minimal',[
            dict(path=str(path),filename=Path(path).name,size=1,created=1,hash_original='a'*64)
            for path in paths]).result(3)

    def test_private_windows_ascii_alias_is_one_file_but_two_native_ids(self):
        folder = self.root/'MiColeccion'
        folder.mkdir()
        original = folder/'Mixed.png'
        original.write_bytes(b'private image fixture')
        alias = self.root/'micoleccion'/'mixed.png'
        if not alias.exists():
            self.skipTest('Private parent is case-sensitive; alias equality not applicable')
        self.assertTrue(os.path.samefile(original,alias))
        before = hashlib.sha256(original.read_bytes()).digest()
        self.assertTrue(self.register(original,alias))
        ids = [self.service.read('get_file_id',str(path)) for path in (original,alias)]
        self.assertNotEqual(*ids)
        self.service.submit('edit_curation',str(original),rating=5,add_tags=['curated']).result(3)
        records = self.service.read('get_curation_snapshot',[str(original),str(alias)])
        self.assertEqual([record['rating'] for record in records],[5,0])
        self.assertEqual([record['tags'] for record in records],[('curated',),()])
        self.assertEqual(hashlib.sha256(original.read_bytes()).digest(),before)

    def test_unicode_windows_alias_does_not_match_sqlite_legacy_like(self):
        original_folder = self.root/'Árbol'
        original_folder.mkdir()
        original = original_folder/'one.png'
        original.write_bytes(b'private image fixture')
        alias_folder = self.root/'árbol'
        if not alias_folder.exists():
            self.skipTest('Private parent distinguishes Unicode spelling; alias equality not applicable')
        self.assertTrue(os.path.samefile(original_folder,alias_folder))
        self.assertTrue(self.register(original))
        self.assertEqual(self.service.read('get_folder_count',str(original_folder)),1)
        self.assertEqual(self.service.read('get_folder_count',str(alias_folder)),0)

    def test_legacy_ascii_like_combines_case_distinct_paths_in_db(self):
        # Synthetic roots model potentially case-sensitive folders. No OS flag
        # is changed and no assumption is made that these are one physical copy.
        upper, lower = 'C:/private/Root/one.png','C:/private/root/two.png'
        self.assertTrue(self.register(upper,lower))
        self.assertNotEqual(self.service.read('get_file_id',upper),self.service.read('get_file_id',lower))
        self.assertEqual(self.service.read('get_folder_count','C:/private/Root'),2)
        self.assertEqual(self.service.read('get_folder_count','C:/private/root'),2)

    def test_archive_one_case_spelling_preserves_both_ids_and_curated_history(self):
        upper, lower = 'C:/private/Root/Mixed.png','C:/private/root/mixed.png'
        self.assertTrue(self.register(upper,lower))
        ids = [self.service.read('get_file_id',path) for path in (upper,lower)]
        self.service.submit('edit_curation',upper,rating=5,add_tags=['curated']).result(3)
        self.assertTrue(self.service.submit('remove_files',[upper]).result(3))
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            rows = conn.execute('SELECT id,rating,is_offline FROM files ORDER BY id').fetchall()
            tags = conn.execute('SELECT t.name FROM file_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.file_id=?',(ids[0],)).fetchall()
        self.assertEqual(rows,[(ids[0],5,1),(ids[1],0,0)])
        self.assertEqual(tags,[('curated',)])
        self.assertEqual(self.service.read('get_file_id',lower),ids[1])
        self.assertIsNone(self.service.read('get_file_id',upper))


if __name__ == '__main__':
    unittest.main(verbosity=2)
