"""Quota notices, live transfer states and wall-clock timers; private fixtures."""

import asyncio
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from modules.cherry.ui.download_panel import DownloadPanel
from third_party.cherry_dl import config
from third_party.cherry_dl.site_throttle import SiteThrottle, ThrottleDeferred, QuotaSnapshot
from third_party.cherry_dl.transfer_status import TransferStatus, WorkerState
from third_party.cherry_dl import download_service, live_metrics, site_budget, site_throttle
from third_party.cherry_dl.templates import _registry
from third_party.cherry_dl.templates.base import ArtistInfo, FileInfo, SiteTemplate
from third_party.cherry_dl.site_policy import SourceCategory, ScanPolicy, TransferPolicy


class RealDownloadWait(unittest.IsolatedAsyncioTestCase):
    async def test_http_transfers_pause_once_and_resume_with_private_quota(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                time.sleep(.02)
                payload = (self.path * 10).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        class FixtureTemplate(SiteTemplate):
            name = "pawchive"
            category = SourceCategory.REPOSITORY
            scan_policy = ScanPolicy(cooldown_threshold=200)
            transfer_policy = TransferPolicy(workers=2, delay_min=0, delay_max=0)

            @classmethod
            def can_handle(cls, url):
                return url.startswith("fixture://")

            async def get_artist_info(self, url):
                return ArtistInfo("artist", "Artist", "fixture", "pawchive", url)

            async def iter_files(self, artist, since=None):
                for n in range(3):
                    yield FileInfo(url=f"{base}/{n}", filename=f"file{n}.bin",
                                   artist_id="artist", artist_name="Artist", post_id=str(n),
                                   url_source=f"fixture://{n}")

        try:
            with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                for obj, key, value in (
                    (download_service, "INDEX_DB", root / "index.db"),
                    (config, "INDEX_DB", root / "index.db"),
                    (config, "CONFIG_FILE", root / "config.toml"),
                    (config, "SESSION_FILE", root / "session.json"),
                    (site_throttle, "THROTTLE_FILE", root / "quota.json"),
                    (site_budget, "BUDGET_FILE", root / "budget.json"),
                    (live_metrics, "METRICS_FILE", root / "metrics.jsonl"),
                    (_registry, "_TEMPLATES", [FixtureTemplate]),
                ):
                    stack.enter_context(patch.object(obj, key, value))
                cfg = config.UserConfig(download_dir=tmp, workers=2,
                    network=config.NetworkConfig(delay_min=0, delay_max=0, retries_api=1, retries_file=1),
                    pawchive=config.PawchiveConfig(downloads_per_window=2, resume_headroom=1,
                                                   gigabytes_per_window=1, window_hours=1 / 3600))
                events = []
                summary = await asyncio.wait_for(download_service.run_profile_download(
                    {"id": -1, "folder_path": str(root / "Artist"), "urls": [
                        {"id": -1, "url": "fixture://artist", "site": "pawchive", "enabled": True,
                         "last_synced": None, "file_count": 0}]},
                    workers=2, ext_filter=set(), emit=events.append, config=cfg,
                    force_full=True, finalize_profile=False,
                ), 10)
                self.assertEqual(summary.downloaded, 3)
                self.assertEqual(summary.errors, 0)
                waits = [e for e in events if isinstance(e, WorkerState) and e.phase == "waiting"]
                self.assertTrue(any(e.reason == "quota_bytes" and e.until is None for e in waits))
                self.assertTrue(any(e.reason == "quota_count_recovery" for e in waits))
                logs = [e.msg for e in events if isinstance(e, download_service.Log)]
                pauses = [msg for msg in logs if "transferencias en espera" in msg]
                resumes = [msg for msg in logs if "transferencias reanudadas" in msg]
                self.assertEqual(len(pauses), 1, pauses)
                self.assertEqual(len(resumes), 1, resumes)
                self.assertFalse(any("por volumen" in msg for msg in pauses), pauses)
                self.assertFalse(any(isinstance(e, download_service.Cooldown) for e in events))

                # A restarted, quota-full run must cancel without announcing a
                # fictitious resume or losing its pending files/history.
                now = time.time()
                (root / "quota.json").write_text(json.dumps({"pawchive": {
                    "successes": [now, now], "transfers": [{"at": now, "bytes": 10}],
                    "count_recovery": True,
                }}), encoding="utf-8")
                cfg.pawchive.window_hours = 2
                events.clear()
                task = asyncio.create_task(download_service.run_profile_download(
                    {"id": -1, "folder_path": str(root / "Restart"), "urls": [
                        {"id": -1, "url": "fixture://artist", "site": "pawchive", "enabled": True,
                         "last_synced": None, "file_count": 0}]},
                    workers=2, ext_filter=set(), emit=events.append, config=cfg,
                    force_full=True, finalize_profile=False,
                ))
                try:
                    async def paused():
                        while not any(isinstance(e, download_service.Log) and "transferencias en espera" in e.msg for e in events):
                            await asyncio.sleep(.01)
                    await asyncio.wait_for(paused(), 3)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(task.cancelled())
                self.assertFalse(any(isinstance(e, download_service.Log) and "transferencias reanudadas" in e.msg for e in events))
                from third_party.cherry_dl.catalog import get_pending_files
                self.assertEqual(len(await get_pending_files(root / "Restart")), 3)
                self.assertEqual(len(json.loads((root / "quota.json").read_text())["pawchive"]["successes"]), 2)
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())


class WorkerWaitCallbacks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gate = SiteThrottle(
            "pawchive", limit=500, byte_limit=100, window_seconds=7200,
            failure_threshold=5, failure_window_seconds=120, cooldowns=(900,),
            path=Path(self.temp.name) / "quota.json", clock=lambda: 1_000_000.,
        )

    async def test_indefinite_reservation_wait_is_visible_and_cancelable(self):
        permit = await self.gate.acquire(expected_bytes=100)
        states = []
        waiter = asyncio.create_task(self.gate.acquire(expected_bytes=1, on_state=lambda *s: states.append(s)))
        try:
            await asyncio.sleep(.02)
            self.assertEqual(states, [("quota_bytes", None)])
            self.assertFalse(waiter.done())
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            await self.gate.abandon(permit)
        self.assertEqual(self.gate.snapshot().active_files, 0)

    async def test_batch_does_not_sleep_for_an_indefinite_reservation(self):
        permit = await self.gate.acquire(expected_bytes=100)
        try:
            with self.assertRaises(ThrottleDeferred) as caught:
                await asyncio.wait_for(self.gate.acquire(expected_bytes=1, wait=False), .2)
            self.assertEqual(caught.exception.kind, "quota_bytes")
            self.assertEqual(caught.exception.seconds, 0)
        finally:
            await self.gate.abandon(permit)

    async def test_both_workers_receive_the_same_persisted_wait_deadline(self):
        self.gate.limit = 1
        permit = await self.gate.acquire(expected_bytes=10)
        await self.gate.finish(permit, success=True, transferred_bytes=10)
        states = [[], []]
        waiters = [asyncio.create_task(self.gate.acquire(
            expected_bytes=1, on_state=lambda *s, slot=slot: states[slot].append(s),
        )) for slot in range(2)]
        try:
            await asyncio.sleep(.02)
            self.assertEqual(states, [[("quota_count", 1_007_200.)]] * 2)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)


class PauseTransitions(unittest.TestCase):
    def test_reservations_during_transfers_never_announce_a_global_pause(self):
        status = TransferStatus(2)
        self.assertIsNone(status.update(WorkerState(0, "downloading")))
        for _ in range(50):
            self.assertIsNone(status.update(WorkerState(1, "waiting", "quota_bytes", 7200)))
            self.assertIsNone(status.update(WorkerState(1, "downloading")))
            self.assertIsNone(status.update(WorkerState(1, "preparing")))
        self.assertFalse(status.paused_reason)

    def test_real_pause_announces_once_and_resumes_only_on_admission(self):
        status = TransferStatus(2)
        self.assertIsNone(status.update(WorkerState(0, "waiting", "quota_count_recovery", 7200)))
        self.assertEqual(status.update(WorkerState(1, "waiting", "quota_count_recovery", 7200))[0], "paused")
        for slot in range(2):
            self.assertIsNone(status.update(WorkerState(slot, "waiting", "quota_count_recovery", 7210)))
        self.assertIsNone(status.update(WorkerState(0, "preparing")))
        self.assertEqual(status.update(WorkerState(0, "downloading"))[0], "resumed")
        self.assertIsNone(status.update(WorkerState(1, "downloading")))

    def test_final_worker_can_pause_after_the_other_finishes(self):
        status = TransferStatus(2)
        status.update(WorkerState(0, "waiting", "quota_bytes", None))
        action, state = status.update(WorkerState(1, "idle"))
        self.assertEqual(action, "paused")
        self.assertIsNone(state.until)


class Locale:
    def __init__(self, language):
        self.values = json.loads((ROOT / "locales" / f"{language}.json").read_text(encoding="utf-8"))

    def tr(self, key, default):
        return self.values.get(key, default)


class PanelTimers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.object(config, "CHERRY_DIR", Path(self.temp.name)), patch.object(config, "load_config", return_value=config.UserConfig()):
            self.panel = DownloadPanel(locale=Locale("es"))
        self.now = 1_000_000.
        self.clock = patch("modules.cherry.ui.download_panel.time.time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.close_panel)
        self.panel._on_workers(2)
        for slot in range(2):
            self.panel._on_worker_start(slot, f"file{slot}.png")
            self.panel._on_worker_state(WorkerState(slot, "preparing"))

    def close_panel(self):
        for timer in (self.panel._cooldown_timer, self.panel._quota_timer, self.panel._status_timer):
            timer.stop()
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_reservation_wait_keeps_transfer_visible_without_long_timer(self):
        self.panel._on_worker_state(WorkerState(0, "downloading"))
        self.panel._on_worker_state(WorkerState(1, "waiting", "quota_bytes", self.now + 3900))
        self.assertIn("1 transferencias activas", self.panel._phase.text())
        self.assertIn("1 esperando cupo", self.panel._phase.text())
        self.assertIn("al terminar otra transferencia", self.panel._rows[1]._name.text())
        self.assertNotIn("65 min", self.panel._rows[1]._name.text())
        self.assertFalse(self.panel._cooldown_timer.isActive())

    def test_quota_pause_counts_real_elapsed_time_and_resumes_before_bytes(self):
        for slot in range(2):
            self.panel._on_worker_state(WorkerState(slot, "waiting", "quota_count_recovery", self.now + 100))
        self.assertIn("1 min 40 s", self.panel._phase.text())
        self.assertIn("a las", self.panel._phase.text())
        self.now += 75
        self.panel._render_worker_states()
        self.assertIn("25 s", self.panel._phase.text())
        self.panel._on_worker_state(WorkerState(0, "downloading"))
        self.assertIn("Descargando", self.panel._phase.text())
        self.assertNotIn("25 s", self.panel._phase.text())
        self.now += 12
        self.panel._render_worker_states()
        self.assertIn("sin nuevos bytes desde hace 12 s", self.panel._rows[0]._name.text())
        self.assertIn("sin nuevos bytes desde hace 12 s", self.panel._phase.text())
        self.panel._on_worker_progress(0, 10, 100)
        self.assertNotIn("sin nuevos bytes", self.panel._rows[0]._name.text())
        self.assertNotIn("sin nuevos bytes", self.panel._phase.text())

    def test_expired_wait_says_rechecking_without_promising_a_download(self):
        self.panel._on_worker_state(WorkerState(0, "waiting", "quota_bytes", self.now - 1))
        self.panel._on_worker_state(WorkerState(1, "idle"))
        self.assertIn("revisando cupo", self.panel._phase.text())

    def test_scan_cooldown_expires_after_clock_jump(self):
        self.panel.set_phase("Escaneando")
        self.panel._on_cooldown(30)
        self.now += 120
        self.panel._tick_cooldown()
        self.assertFalse(self.panel._cooldown_timer.isActive())
        self.assertNotIn("Enfriando", self.panel._phase.text())
        self.assertNotIn("Pausa tras el escaneo", self.panel._phase.text())
        self.panel._on_cooldown(30)
        self.panel._on_cooldown(0)
        self.assertFalse(self.panel._cooldown_timer.isActive())

    def test_quota_release_uses_deadline_instead_of_number_of_ticks(self):
        self.panel._set_quota(QuotaSnapshot("pawchive", 10, 500, 100, 25 * 2**30, 0, 7200, 100), 0)
        self.now += 75
        self.panel._tick_quota()
        self.assertEqual(self.panel._quota_next_release, 25)
        self.now += 200
        with patch.object(self.panel, "refresh_pawchive_quota") as refresh:
            self.panel._tick_quota()
        refresh.assert_called_once()

    def test_cancellation_and_source_change_cannot_be_overwritten_by_old_timer(self):
        for slot in range(2):
            self.panel._on_worker_state(WorkerState(slot, "waiting", "quota_count", self.now + 100))
        self.panel.set_phase("Cancelado")
        self.now += 20
        self.panel._render_worker_states()
        self.assertEqual(self.panel._phase.text(), "Cancelado")
        self.assertEqual(self.panel._rows[0]._name.text(), "—")
        self.panel._on_source_started("test://source", "Artista", "patreon")
        self.panel._render_worker_states()
        self.assertEqual(self.panel._phase.text(), "Fuente: Artista (patreon)")

    def test_source_done_removes_all_waiting_status(self):
        self.panel._on_worker_state(WorkerState(0, "waiting", "quota_count", self.now + 100))
        self.panel._on_source_done(1, 20)
        self.now += 20
        self.panel._render_worker_states()
        self.assertIn("Fuente terminada", self.panel._phase.text())
        self.assertFalse(self.panel._worker_states)


if __name__ == "__main__":
    unittest.main()
