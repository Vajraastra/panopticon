"""scripts/crash_report.py: localiza el último fallo y simboliza módulo+desplazamiento."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import crash_report


class CrashReport(unittest.TestCase):
    def test_last_crash_block_skips_clean_sessions_and_finds_the_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "crash.log"
            log.write_text(
                "\n=== 2026-01-01 10:00:00 arranque (pid 1) ===\nQt WARNING: algo\n"
                "\n=== 2026-01-01 11:00:00 arranque (pid 2) ===\nWindows fatal exception: access violation\n"
                "Current thread 0x1 (most recent call first):\n"
                "\n=== 2026-01-01 12:00:00 arranque (pid 3) ===\nQt WARNING: sesión limpia posterior\n",
                encoding="utf-8")
            block = crash_report.last_crash_block(log)
        self.assertIn("pid 2", block)
        self.assertIn("access violation", block)
        self.assertNotIn("pid 3", block)

    def test_no_crash_means_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(crash_report.last_crash_block(Path(tmp) / "no-existe.log"))
            log = Path(tmp) / "crash.log"
            log.write_text("\n=== arranque ===\nQt WARNING: ruido\n", encoding="utf-8")
            self.assertIsNone(crash_report.last_crash_block(log))

    @unittest.skipUnless(sys.platform == "win32", "las DLL son de Windows")
    def test_symbolize_names_the_closest_export_below_the_offset(self):
        dll = crash_report.find_dll("python3" + str(sys.version_info.minor) + ".dll")
        self.assertIsNotNone(dll, "no se encontró la DLL de Python")
        table = crash_report.export_table(dll)
        self.assertGreater(len(table), 500)
        # Una exportación cuya siguiente esté lejos: rva+3 debe caer dentro de ella.
        for (rva, name), (next_rva, _) in zip(table, table[1:]):
            if next_rva - rva > 16:
                self.assertEqual(crash_report.symbolize(dll.name, rva + 3), f"{name}+0x3")
                break
        else:
            self.fail("no se halló una exportación con hueco suficiente")

    def test_annotate_only_touches_frame_lines(self):
        text = "pila nativa:\n  ntdll.dll+0x10\n  esto no es un marco\n"
        out = crash_report.annotate(text)
        self.assertIn("esto no es un marco", out)
        self.assertEqual(out.count("≈"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
