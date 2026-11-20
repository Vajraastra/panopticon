"""New-project admission creates a root; missing historical roots stay absent."""
import asyncio
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from third_party.cherry_dl import profiles,index,catalog


class ProfileCreation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        work=ROOT/'.work'/'profile-creation-tests';work.mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir=work);self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.database=self.root/'index.db'
        await index.init_index(self.database)

    async def test_official_new_profile_creates_root_and_portable_source_metadata(self):
        key=await profiles.create_profile(self.database,'Artist','https://private.invalid/artist',self.root/'downloads')
        saved=await index.get_profile(self.database,key)
        folder=Path(saved['folder_path'])
        self.assertTrue(folder.is_dir())
        metadata=await catalog.read_profile_meta(folder)
        self.assertEqual(metadata['display_name'],'Artist')
        self.assertEqual(metadata['urls'][0]['url'],'https://private.invalid/artist')

    async def test_failed_duplicate_admission_does_not_recreate_missing_old_root(self):
        key=await profiles.create_profile(self.database,'Artist','https://private.invalid/artist',self.root/'downloads')
        saved=await index.get_profile(self.database,key)
        folder=Path(saved['folder_path'])
        archived=self.root/'curator-retained-folder'
        folder.rename(archived)
        with self.assertRaises(sqlite3.IntegrityError):
            await profiles.create_profile(self.database,'Artist','https://private.invalid/second',self.root/'downloads')
        self.assertFalse(folder.exists())
        self.assertTrue((archived/catalog.CATALOG_NAME).is_file())


if __name__=='__main__':unittest.main()
