"""session.json / config.toml: escritura atómica y carga tolerante a corrupción."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from third_party.cherry_dl import config
from third_party.cherry_dl.auth import patreon


class SessionAtomic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.session = root / "session.json"
        self.config = root / "config.toml"
        for key, value in (("CHERRY_DIR", root), ("SESSION_FILE", self.session),
                           ("CONFIG_FILE", self.config)):
            patcher = patch.object(config, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_roundtrip_and_merge_keep_other_sites(self):
        patreon.save_patreon_cookies({"session_id": "abc"})
        config.save_session({**config.load_session(), "pixiv": {"refresh": "x"}})
        data = config.load_session()
        self.assertEqual(data["patreon"]["session_id"], "abc")
        self.assertEqual(data["pixiv"], {"refresh": "x"})
        self.assertFalse(list(Path(self.tmp.name).glob("*.tmp")))

    def test_truncated_session_is_set_aside_not_fatal(self):
        self.session.write_text('{"patreon": {"session_id": "abc", "_saved_at": 17', encoding="utf-8")
        self.assertEqual(config.load_session(), {})
        self.assertFalse(self.session.exists())
        self.assertIn('"session_id"', self.session.with_name("session.json.corrupt").read_text(encoding="utf-8"))
        # Los consumidores arrancan sin sesión en vez de lanzar JSONDecodeError.
        self.assertFalse(patreon.load_patreon_cookies())
        # Y una nueva sesión se guarda con normalidad.
        patreon.save_patreon_cookies({"session_id": "nuevo"})
        self.assertEqual(config.load_session()["patreon"]["session_id"], "nuevo")

    def test_non_object_json_is_treated_as_corrupt(self):
        self.session.write_text("[]", encoding="utf-8")
        self.assertEqual(config.load_session(), {})
        self.assertTrue(self.session.with_name("session.json.corrupt").exists())

    def test_failed_write_keeps_previous_session_intact(self):
        config.save_session({"patreon": {"session_id": "viejo"}})
        before = self.session.read_bytes()
        with patch("third_party.cherry_dl.config.os.replace", side_effect=OSError("disco lleno")):
            with self.assertRaises(OSError):
                config.save_session({"patreon": {"session_id": "nuevo"}})
        self.assertEqual(self.session.read_bytes(), before)
        self.assertEqual(json.loads(before)["patreon"]["session_id"], "viejo")
        self.assertFalse(list(Path(self.tmp.name).glob("*.tmp")))

    def test_failed_config_write_keeps_previous_config_intact(self):
        config.save_config(config.UserConfig(workers=3))
        before = self.config.read_bytes()
        with patch("third_party.cherry_dl.config.os.replace", side_effect=OSError("disco lleno")):
            with self.assertRaises(OSError):
                config.save_config(config.UserConfig(workers=5))
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(config.load_config().workers, 3)
        config.save_config(config.UserConfig(workers=5))
        self.assertEqual(config.load_config().workers, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
