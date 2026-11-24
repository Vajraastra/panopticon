"""Alta de proyecto: perfil, primera fuente y carpeta se crean juntos o no se crean."""

import asyncio
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from third_party.cherry_dl import index, profiles


class ProfileCreationIsAtomic(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / "library"
        self.base.mkdir()
        self.db = Path(self.tmp.name) / "index.db"
        await index.init_index(self.db)

    def counts(self):
        con = sqlite3.connect(self.db)
        try:
            profiles_n = con.execute("select count(*) from profiles").fetchone()[0]
            urls_n = con.execute("select count(*) from profile_urls").fetchone()[0]
            orphans = con.execute(
                "select count(*) from profiles p where not exists "
                "(select 1 from profile_urls u where u.profile_id = p.id)").fetchone()[0]
        finally:
            con.close()
        return profiles_n, urls_n, orphans

    async def create(self, name="Artista", url="https://www.patreon.com/artista"):
        return await profiles.create_profile(self.db, name, url, self.base)

    async def test_creates_profile_source_and_folder_together(self):
        profile_id = await self.create()
        self.assertEqual(self.counts(), (1, 1, 0))
        self.assertTrue((self.base / "Artista").is_dir())
        self.assertGreater(profile_id, 0)

    async def test_folder_failure_rolls_everything_back(self):
        real_mkdir = Path.mkdir

        def failing(path, *args, **kwargs):
            if path.name == "Artista":
                raise PermissionError("carpeta bloqueada")
            return real_mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", failing):
            with self.assertRaises(PermissionError):
                await self.create()
        # Antes quedaba un perfil sin fuentes (el insert del perfil ya estaba confirmado).
        self.assertEqual(self.counts(), (0, 0, 0))
        # Y el nombre queda libre para reintentar.
        await self.create()
        self.assertEqual(self.counts(), (1, 1, 0))

    def hold_write_lock(self, seconds):
        ready = threading.Event()

        def run():
            con = sqlite3.connect(self.db, timeout=30, isolation_level=None)
            con.execute("BEGIN IMMEDIATE")
            ready.set()
            time.sleep(seconds)
            con.execute("COMMIT")
            con.close()

        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join)
        self.assertTrue(ready.wait(5))

    async def test_waits_for_a_busy_database_instead_of_failing(self):
        self.hold_write_lock(0.8)
        with patch.object(index, "CREATE_LOCK_TIMEOUT", 10.0):
            await self.create()
        self.assertEqual(self.counts(), (1, 1, 0))

    async def test_lock_timeout_leaves_nothing_behind(self):
        self.hold_write_lock(1.5)
        with patch.object(index, "CREATE_LOCK_TIMEOUT", 0.3):
            with self.assertRaises(sqlite3.OperationalError):
                await self.create()
        await asyncio.sleep(1.5)   # el otro escritor termina
        self.assertEqual(self.counts(), (0, 0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
