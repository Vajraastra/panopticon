"""index.db: WAL y espera de lock de 30 s en todas sus operaciones, no solo en el alta."""

import asyncio
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from third_party.cherry_dl import index


def journal_mode(path: Path) -> str:
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        return con.execute("pragma journal_mode").fetchone()[0]
    finally:
        con.close()


class IndexLocking(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "index.db"

    async def test_init_index_switches_to_wal(self):
        await index.init_index(self.db)
        self.assertEqual(journal_mode(self.db), "wal")

    async def test_init_index_converts_an_existing_rollback_journal_database(self):
        con = sqlite3.connect(self.db)
        con.execute("create table t(a)")
        con.commit()
        con.close()
        self.assertEqual(journal_mode(self.db), "delete")
        await index.init_index(self.db)
        self.assertEqual(journal_mode(self.db), "wal")

    async def test_a_busy_reader_delays_init_index_without_blocking_the_whole_timeout(self):
        await index.init_index(self.db)
        con = sqlite3.connect(self.db)
        con.execute("pragma journal_mode=DELETE")   # vuelve al modo antiguo
        con.close()
        reader = sqlite3.connect(self.db, isolation_level=None, check_same_thread=False)
        reader.execute("BEGIN")
        reader.execute("select count(*) from sqlite_master").fetchall()   # lock compartido

        def release():
            reader.execute("COMMIT")
            reader.close()

        timer = threading.Timer(3.0, release)
        timer.start()
        self.addCleanup(timer.join)
        started = time.monotonic()
        await asyncio.wait_for(index.init_index(self.db), 60)
        elapsed = time.monotonic() - started
        # Esperó a que el lector soltara (sus escrituras necesitan el lock), no los 30 s enteros.
        self.assertGreater(elapsed, 2.0)
        self.assertLess(elapsed, 15)
        # El intento de pasar a WAL usó una espera corta (0,5 s): con el lector activo no se pudo
        # cambiar y lo dejó como estaba, en vez de esperar a que soltara y cambiarlo.
        self.assertEqual(journal_mode(self.db), "delete")
        await index.init_index(self.db)             # sin lector: ahora sí cambia
        self.assertEqual(journal_mode(self.db), "wal")

    async def test_read_only_readers_keep_working_on_a_wal_index(self):
        await index.init_index(self.db)
        await index.create_profile_with_url(
            self.db, "Artista", Path(self.tmp.name) / "Artista", "patreon",
            "https://www.patreon.com/artista", "patreon")
        con = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        try:
            self.assertEqual(con.execute("select count(*) from profiles").fetchone()[0], 1)
        finally:
            con.close()

    async def test_every_operation_waits_past_the_old_five_second_limit(self):
        # Antes solo el alta esperaba 30 s; el resto fallaba a los 5 s con «database is locked».
        await index.init_index(self.db)
        profile_id = await index.create_profile_with_url(
            self.db, "Artista", Path(self.tmp.name) / "Artista", "patreon",
            "https://www.patreon.com/artista", "patreon")
        ready = threading.Event()

        def hold(seconds):
            con = sqlite3.connect(self.db, timeout=60, isolation_level=None)
            con.execute("BEGIN IMMEDIATE")
            ready.set()
            time.sleep(seconds)
            con.execute("COMMIT")
            con.close()

        thread = threading.Thread(target=hold, args=(6.5,))
        thread.start()
        self.addCleanup(thread.join)
        self.assertTrue(ready.wait(5))
        started = time.monotonic()
        url_id = await index.add_profile_url(
            self.db, profile_id, "https://pawchive.pw/patreon/user/1", "pawchive")
        waited = time.monotonic() - started
        self.assertGreater(url_id, 0)
        self.assertGreater(waited, 5.0, "esperó el lock más allá de los 5 s antiguos")


if __name__ == "__main__":
    unittest.main(verbosity=2)
