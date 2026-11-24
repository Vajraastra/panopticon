"""Panel de descarga: una fila en curso no se borra por eventos de fase de otra fuente."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from modules.cherry.ui.download_panel import DownloadPanel
from third_party.cherry_dl import config
from third_party.cherry_dl.transfer_status import WorkerState


class Locale:
    def __init__(self, language):
        self.values = json.loads((ROOT / "locales" / f"{language}.json").read_text(encoding="utf-8"))

    def tr(self, key, default):
        return self.values.get(key, default)


class PanelRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.object(config, "CHERRY_DIR", Path(self.temp.name)), \
                patch.object(config, "load_config", return_value=config.UserConfig()):
            self.panel = DownloadPanel(locale=Locale("es"))
        self.addCleanup(self.close_panel)
        self.panel._on_workers(2)

    def close_panel(self):
        for timer in (self.panel._cooldown_timer, self.panel._quota_timer, self.panel._status_timer):
            timer.stop()
        self.panel.deleteLater()

    def row(self, slot):
        row = self.panel._rows[slot]
        # El nombre lleva un sufijo de estado («· Transferencia activa»); se compara el archivo.
        return row._name.text().split(" · ")[0], row._bar.value(), row._bar.minimum(), row._bar.maximum()

    def start_both(self):
        for slot in range(2):
            self.panel._on_worker_start(slot, f"file{slot}.png")
            self.panel._on_worker_state(WorkerState(slot, "downloading"))
        self.panel._on_worker_progress(1, 40, 100)

    def test_phase_events_do_not_blank_a_transfer_in_progress(self):
        self.start_both()
        # La superior termina; el motor pasa a otra fuente mientras la inferior sigue.
        self.panel._on_worker_done(0, "file0.png", "✓")
        before = self.row(1)
        self.assertEqual(before[:2], ("file1.png", 40))
        for event in (lambda: self.panel._on_source_started("fixture://b", "B", "pawchive"),
                      lambda: self.panel._on_scan_progress(5, 3),
                      lambda: self.panel._on_scan_complete(3),
                      lambda: self.panel._on_batch_info(10, 0)):
            event()
            self.assertEqual(self.row(1), before, "la fila en curso no debe vaciarse")
        # El progreso posterior de esa misma transferencia sigue actualizando su barra.
        self.panel._on_worker_progress(1, 80, 100)
        self.assertEqual(self.row(1)[:2], ("file1.png", 80))

    def test_finished_row_keeps_its_last_file_until_it_starts_another(self):
        self.start_both()
        self.panel._on_worker_done(0, "file0.png", "✓")
        self.panel._on_scan_progress(1, 1)
        self.assertEqual(self.row(0)[:2], ("✓ file0.png", 100))
        self.panel._on_worker_start(0, "file2.png")
        self.assertEqual(self.row(0)[:2], ("file2.png", 0))

    def test_a_new_profile_or_run_does_reset_every_row(self):
        self.start_both()
        self.panel._on_profile_started(2, 3, "Otro")
        self.assertEqual([self.row(s)[:2] for s in range(2)], [("—", 0), ("—", 0)])
        self.start_both()
        self.panel.reset("Nueva corrida")
        self.assertEqual([self.row(s)[:2] for s in range(2)], [("—", 0), ("—", 0)])

    def test_idle_after_done_clears_only_that_row(self):
        self.start_both()
        self.panel._on_worker_idle(0)
        self.assertEqual(self.row(0)[:2], ("—", 0))
        self.assertEqual(self.row(1)[:2], ("file1.png", 40))


if __name__ == "__main__":
    unittest.main(verbosity=2)
