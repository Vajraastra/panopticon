"""Adaptive quota, raw retries/cancellation and independent UI timers; private data."""

import asyncio
from contextlib import ExitStack
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import httpx
from PySide6.QtWidgets import QApplication
from third_party.cherry_dl import config
from third_party.cherry_dl.engine import DownloadEngine, ErrorKind
from third_party.cherry_dl.site_throttle import SiteThrottle, ThrottleDeferred
from modules.cherry.ui.download_panel import DownloadPanel


class QuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 1000.25

    async def asyncTearDown(self):
        self.temp.cleanup()

    def gate(self, *, limit=5, byte_limit=100, adaptive=True, path=None):
        return SiteThrottle('pawchive', limit=limit, byte_limit=byte_limit,
            raw_traffic=True, adaptive=adaptive, window_seconds=100,
            resume_headroom=0, failure_threshold=1, failure_window_seconds=0,
            cooldowns=(0,), path=path or self.root / 'quota.json', clock=lambda: self.now)

    async def completed(self, gate, size):
        permit = await gate.acquire(expected_bytes=size, wait=False)
        gate.record_raw_bytes(permit, size)
        await gate.finish(permit, success=True, transferred_bytes=size)

    async def test_500_small_images_enter_economic_and_all_still_count(self):
        gate = self.gate(limit=500, byte_limit=25000)
        for n in range(500):
            await self.completed(gate, 10)
            self.now += .01
        self.assertEqual(gate.snapshot().mode, 'economic')
        self.assertEqual(gate.snapshot().used_bytes, 5000)
        self.assertEqual(gate.snapshot().resume_at_files, 400)
        await self.completed(gate, 10)
        self.assertEqual(gate.snapshot().used_files, 501)
        reloaded = self.gate(limit=500, byte_limit=25000)
        self.assertEqual(reloaded.snapshot().mode, 'economic')
        self.now = 1101.27  # first 102 successes have expired, not all traffic.
        self.assertEqual(reloaded.snapshot().mode, 'normal')
        self.assertLessEqual(reloaded.snapshot().used_files, 400)

    async def test_half_volume_is_strict_entry_threshold(self):
        gate = self.gate()
        for _ in range(5):
            await self.completed(gate, 10)
        self.assertEqual(gate.snapshot().used_bytes, 50)
        with self.assertRaises(ThrottleDeferred) as error:
            await gate.acquire(expected_bytes=1, wait=False)
        self.assertEqual(error.exception.kind, 'quota_count_recovery')
        self.assertEqual(gate.snapshot().mode, 'normal')

    async def test_wait_can_resume_economic_before_file_count_recovers(self):
        (self.root / 'quota.json').write_text(json.dumps({'pawchive': {
            'successes': [1050.] * 5,
            'transfers': [{'at': 1000., 'bytes': 70}]
        }}))
        self.now = 1070.
        gate = self.gate()
        with self.assertRaises(ThrottleDeferred) as error:
            await gate.acquire(expected_bytes=1, wait=False)
        self.assertEqual(error.exception.kind, 'quota_economic')
        self.assertEqual(error.exception.seconds, 30.)
        self.now = 1100.1
        permit = await gate.acquire(expected_bytes=1, wait=False)
        self.assertEqual(gate.snapshot().mode, 'economic')
        self.assertEqual(gate.snapshot().used_files, 5)
        await gate.abandon(permit)

    async def test_volume_standby_does_not_wait_for_count_recovery_too(self):
        (self.root / 'quota.json').write_text(json.dumps({'pawchive': {
            'successes': [1050.] * 5,
            'transfers': [{'at': 1000., 'bytes': 70}]
        }}))
        self.now = 1070.
        gate = self.gate()
        with self.assertRaises(ThrottleDeferred) as error:
            await gate.acquire(expected_bytes=40, wait=False)
        self.assertEqual(error.exception.kind, 'quota_bytes_recovery')
        self.assertEqual(error.exception.seconds, 30.)

    async def test_reservations_alone_do_not_trigger_economic_mode(self):
        gate = self.gate(limit=5)
        for _ in range(4):
            await self.completed(gate, 1)
        permit = await gate.acquire(expected_bytes=1, wait=False)
        with self.assertRaises(ThrottleDeferred):
            await gate.acquire(expected_bytes=1, wait=False)
        self.assertEqual(gate.snapshot().mode, 'normal')
        gate.record_raw_bytes(permit, 1)
        await gate.finish(permit, success=True)
        self.assertEqual(gate.snapshot().mode, 'economic')

    async def test_full_volume_recovers_half_then_normal(self):
        gate = self.gate()
        for _ in range(5):
            await self.completed(gate, 5)
        self.now += 1
        await self.completed(gate, 75)
        self.assertEqual(gate.snapshot().mode, 'standby')
        with self.assertRaises(ThrottleDeferred) as error:
            await gate.acquire(expected_bytes=1, wait=False)
        self.assertEqual(error.exception.kind, 'quota_bytes_recovery')
        self.now = 1101.5  # 25 bytes released; still 75 > half, remain asleep.
        with self.assertRaises(ThrottleDeferred):
            await gate.acquire(expected_bytes=1, wait=False)
        self.now = 1102.1
        permit = await gate.acquire(expected_bytes=1, wait=False)
        self.assertEqual(gate.snapshot().mode, 'normal')
        await gate.abandon(permit)

    async def test_recovered_volume_can_resume_economic_with_fresh_successes(self):
        gate = self.gate()
        permit = await gate.acquire(expected_bytes=100)
        gate.record_raw_bytes(permit, 100)
        await gate.finish(permit, success=False)
        self.now = 1099.
        data = json.loads((self.root / 'quota.json').read_text())
        data['pawchive']['successes'] = [1099.] * 6
        (self.root / 'quota.json').write_text(json.dumps(data))
        self.now = 1101.1
        resumed = await gate.acquire(expected_bytes=1, wait=False)
        self.assertEqual(gate.snapshot().mode, 'economic')
        self.assertEqual(gate.snapshot().used_files, 6)
        await gate.abandon(resumed)

    async def test_failed_and_cancelled_bytes_survive_restart_without_success_count(self):
        gate = self.gate()
        failed = await gate.acquire(expected_bytes=40)
        gate.record_raw_bytes(failed, 25)
        await gate.finish(failed, success=False)
        cancelled = await gate.acquire(expected_bytes=40)
        gate.record_raw_bytes(cancelled, 20)
        await gate.abandon(cancelled)
        self.assertEqual(gate.snapshot().used_bytes, 45)
        self.assertEqual(gate.snapshot().used_files, 0)
        self.assertEqual(gate.snapshot().reserved_bytes, 0)
        reloaded = self.gate()
        self.assertEqual(reloaded.snapshot().used_bytes, 45)
        self.assertGreater(reloaded.snapshot().next_release_seconds, 0)
        self.now = 1101.01
        self.assertEqual(reloaded.snapshot().used_bytes, 0)

    async def test_unclean_restart_keeps_unacknowledged_reservation_conservatively(self):
        gate = self.gate()
        permit = await gate.acquire(expected_bytes=60)
        gate.record_raw_bytes(permit, 20)
        self.now += 10
        reloaded = self.gate()
        next_permit = await reloaded.acquire(expected_bytes=1)
        self.assertEqual(reloaded.snapshot().used_bytes, 60)
        self.assertEqual(reloaded.snapshot().used_files, 0)
        await reloaded.abandon(next_permit)

    async def test_concurrent_reservations_and_resize_do_not_double_count_raw(self):
        gate = self.gate()
        a = await gate.acquire(expected_bytes=60)
        b = await gate.acquire(expected_bytes=40)
        gate.record_raw_bytes(a, 25)
        self.assertEqual(gate.snapshot().used_bytes + gate.snapshot().reserved_bytes, 100)
        self.assertEqual(await gate.resize(a, 36), 0)
        self.assertEqual(await gate.resize(a, 30), 30)
        gate.record_raw_bytes(a, 30)
        await gate.finish(a, success=True, transferred_bytes=55)
        self.assertEqual(gate.snapshot().used_bytes, 55)
        self.assertEqual(gate.snapshot().used_files, 1)
        await gate.abandon(b)

    async def test_legacy_completed_volume_imported_once(self):
        (self.root / 'quota.json').write_text(json.dumps({'pawchive': {
            'successes': [1000.], 'transfers': [{'at': 1000., 'bytes': 25}]
        }}))
        gate = self.gate()
        await self.completed(gate, 10)
        reloaded = self.gate()
        await self.completed(reloaded, 10)
        self.assertEqual(reloaded.snapshot().used_bytes, 45)
        self.assertEqual(reloaded.snapshot().used_files, 3)

    async def test_receipt_failure_keeps_reservation_and_blocks_new_downloads(self):
        import sqlite3
        gate = self.gate()
        permit = await gate.acquire(expected_bytes=60)
        with patch.object(gate.traffic, '_add', side_effect=sqlite3.OperationalError('fixture write failure')):
            with self.assertRaises(sqlite3.OperationalError):
                gate.record_raw_bytes(permit, 20)
        await gate.abandon(permit)
        self.assertEqual(gate.snapshot().reserved_bytes, 60)
        with self.assertRaises(OSError):
            await gate.acquire(expected_bytes=1, wait=False)
        reloaded = self.gate()
        resumed = await reloaded.acquire(expected_bytes=1, wait=False)
        self.assertEqual(reloaded.snapshot().used_bytes, 60)
        await reloaded.abandon(resumed)

    async def download(self, handler, *, budget=100, size=100, payments=None):
        gate = self.gate(byte_limit=budget)
        permit = await gate.acquire(expected_bytes=size)
        cfg = config.UserConfig(network=config.NetworkConfig(delay_min=0, delay_max=0, retries_file=2))
        with patch('third_party.cherry_dl.engine.load_session', return_value={}):
            engine = DownloadEngine(cfg)
        if payments is not None:
            class Limiter:
                async def take(self, amount):
                    payments.append(amount)
            engine._bucket = Limiter()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            engine._client = client
            result = await engine.download('https://fixture.test/file', self.root, 'out.bin',
                on_response_size=lambda n: gate.resize(permit, n),
                on_raw_bytes=lambda n: gate.record_raw_bytes(permit, n))
        await gate.finish(permit, success=result.ok, transferred_bytes=result.file_size)
        return result, gate

    async def test_redirect_body_also_counts_against_raw_volume(self):
        class Body(httpx.AsyncByteStream):
            def __init__(self, content):
                self.content = content
            async def __aiter__(self):
                yield self.content
        def handler(req):
            if req.url.path == '/file':
                return httpx.Response(302, headers={'Content-Length': '4', 'Location': '/final'},
                                      stream=Body(b'move'))
            return httpx.Response(200, headers={'Content-Length': '20'}, stream=Body(b'a' * 20))
        result, gate = await self.download(handler)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.file_size, 20)
        self.assertEqual(gate.snapshot().used_bytes, 24)
        self.assertEqual(gate.snapshot().used_files, 1)

    async def test_chunked_response_cannot_bypass_content_length_reservation(self):
        read = []
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                read.append(True)
                yield b'a' * 1000
        result, gate = await self.download(lambda req: httpx.Response(200,
            headers={'Content-Length': '10', 'Transfer-Encoding': 'chunked'}, stream=Body()))
        self.assertEqual(result.error_kind, ErrorKind.QUOTA)
        self.assertEqual(read, [])
        self.assertEqual(gate.snapshot().used_bytes, 0)

    async def test_error_body_counts_raw_bytes_without_completing_file(self):
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'access denied'
        result, gate = await self.download(lambda req: httpx.Response(403,
            headers={'Content-Length': '13', 'Content-Type': 'text/plain'}, stream=Body()))
        self.assertEqual(result.error_kind, ErrorKind.AUTH)
        self.assertEqual(gate.snapshot().used_bytes, 13)
        self.assertEqual(gate.snapshot().used_files, 0)

    async def test_unknown_response_size_is_deferred_without_reading_body(self):
        read = []
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                read.append(True)
                yield b'a' * 1000
        result, gate = await self.download(lambda req: httpx.Response(200, stream=Body()))
        self.assertEqual(result.error_kind, ErrorKind.QUOTA)
        self.assertEqual(read, [])
        self.assertEqual(gate.snapshot().used_bytes, 0)

    async def test_profile_download_stays_economic_and_keeps_both_histories(self):
        import time
        from third_party.cherry_dl import download_service, live_metrics, site_budget, site_throttle
        from third_party.cherry_dl.templates import _registry
        from third_party.cherry_dl.templates.base import ArtistInfo, FileInfo, SiteTemplate
        from third_party.cherry_dl.site_policy import SourceCategory, ScanPolicy, TransferPolicy

        class Body(httpx.AsyncByteStream):
            def __init__(self, payload):
                self.payload = payload
            async def __aiter__(self):
                yield self.payload

        rates = []
        class FixtureEngine(DownloadEngine):
            async def __aenter__(self):
                self._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
                    httpx.Response(200, headers={'Content-Length': '20'},
                                   stream=Body(req.url.path.encode().ljust(20, b'x')))))
                return self

            def set_rate(self, rate):
                rates.append(rate)
                return super().set_rate(rate)

        class FixtureTemplate(SiteTemplate):
            name = 'pawchive'
            category = SourceCategory.REPOSITORY
            scan_policy = ScanPolicy(cooldown_threshold=200)
            transfer_policy = TransferPolicy(workers=2, delay_min=0, delay_max=0,
                                            max_bytes_per_sec=4 * 1024**2)
            @classmethod
            def can_handle(cls, url):
                return url.startswith('fixture://')
            async def get_artist_info(self, url):
                return ArtistInfo('artist', 'Artist', 'fixture', 'pawchive', url)
            async def iter_files(self, artist, since=None):
                for n in range(3):
                    yield FileInfo(url=f'https://fixture.test/{n}', filename=f'file{n}.bin',
                        artist_id='artist', artist_name='Artist', post_id=str(n),
                        url_source=f'fixture://{n}', remote_size=20)

        (self.root / 'quota.json').write_text(json.dumps({'pawchive': {
            'successes': [time.time()] * 500,
            'transfers': [{'at': time.time(), 'bytes': 5 * 2**30}]
        }}))
        with ExitStack() as stack:
            for obj, key, value in (
                (download_service, 'INDEX_DB', self.root / 'index.db'),
                (config, 'INDEX_DB', self.root / 'index.db'),
                (config, 'CONFIG_FILE', self.root / 'config.toml'),
                (config, 'SESSION_FILE', self.root / 'session.json'),
                (site_throttle, 'THROTTLE_FILE', self.root / 'quota.json'),
                (site_budget, 'BUDGET_FILE', self.root / 'budget.json'),
                (live_metrics, 'METRICS_FILE', self.root / 'metrics.jsonl'),
                (_registry, '_TEMPLATES', [FixtureTemplate]),
                (download_service, 'DownloadEngine', FixtureEngine),
            ):
                stack.enter_context(patch.object(obj, key, value))
            cfg = config.UserConfig(download_dir=str(self.root), workers=2,
                network=config.NetworkConfig(delay_min=0, delay_max=0, retries_file=1))
            events = []
            result = await asyncio.wait_for(download_service.run_profile_download(
                {'id': -1, 'folder_path': str(self.root / 'Artist'), 'urls': [
                    {'id': -1, 'url': 'fixture://artist', 'site': 'pawchive', 'enabled': True,
                     'last_synced': None, 'file_count': 0}]},
                workers=2, ext_filter=set(), emit=events.append, config=cfg,
                force_full=True, finalize_profile=False), 5)
        self.assertEqual(result.downloaded, 3)
        self.assertEqual(result.errors, 0)
        snapshots = [e.snapshot for e in events if isinstance(e, download_service.QuotaStatus)]
        self.assertEqual(snapshots[-1].used_files, 503)
        self.assertEqual(snapshots[-1].used_bytes, 5 * 2**30 + 60)
        self.assertEqual(snapshots[-1].mode, 'economic')
        self.assertIn(2 * 1024**2, rates)
        self.assertEqual(rates[-1], 2 * 1024**2)

    async def test_encoded_response_counts_raw_body_not_decoded_file_size(self):
        content = b'a' * 10000
        compressed = gzip.compress(content)
        payments = []
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield compressed
        result, gate = await self.download(lambda request: httpx.Response(200,
            headers={'Content-Length': str(len(compressed)), 'Content-Encoding': 'gzip'}, stream=Body()),
            payments=payments)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.file_size, len(content))
        self.assertEqual(gate.snapshot().used_bytes, len(compressed))
        self.assertEqual(sum(payments), len(compressed))

    async def test_partial_retry_counts_both_prefixes_when_range_ignored(self):
        calls = []
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'a' * 20
                if len(calls) == 1:
                    raise httpx.ReadError('fixture cutoff')
                yield b'b' * 20
        def handler(request):
            calls.append(request)
            return httpx.Response(200, headers={'Content-Length': '40'}, stream=Body())
        result, gate = await self.download(handler)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(gate.snapshot().used_bytes, 60)
        self.assertEqual(gate.snapshot().used_files, 1)
        self.assertEqual(result.file_size, 40)

    async def test_retry_that_no_longer_fits_is_deferred_before_body(self):
        calls = []
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'a' * 25
                raise httpx.ReadError('fixture cutoff')
        def handler(request):
            calls.append(request)
            return httpx.Response(200, headers={'Content-Length': '40'}, stream=Body())
        result, gate = await self.download(handler, budget=50, size=40)
        self.assertEqual(result.error_kind, ErrorKind.QUOTA)
        self.assertEqual(gate.snapshot().used_bytes, 25)
        self.assertEqual(gate.snapshot().used_files, 0)
        self.assertEqual(len(calls), 2)
        self.assertFalse((self.root / 'out.bin').exists())

    async def test_cancel_during_body_retains_raw_prefix(self):
        gate = self.gate()
        permit = await gate.acquire(expected_bytes=60)
        received = asyncio.Event()
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'a' * 20
                received.set()
                await asyncio.Event().wait()
        cfg = config.UserConfig(network=config.NetworkConfig(delay_min=0, delay_max=0))
        with patch('third_party.cherry_dl.engine.load_session', return_value={}):
            engine = DownloadEngine(cfg)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
                httpx.Response(200, headers={'Content-Length': '60'}, stream=Body()))) as client:
            engine._client = client
            task = asyncio.create_task(engine.download('https://fixture.test', self.root, 'cancel.bin',
                on_response_size=lambda n: gate.resize(permit, n),
                on_raw_bytes=lambda n: gate.record_raw_bytes(permit, n)))
            await asyncio.wait_for(received.wait(), 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await gate.abandon(permit)
        self.assertEqual(gate.snapshot().used_bytes, 20)
        self.assertEqual(gate.snapshot().used_files, 0)
        self.assertFalse((self.root / 'cancel.bin.tmp').exists())

    async def test_rate_change_preserves_bucket_debt(self):
        with patch('third_party.cherry_dl.engine.load_session', return_value={}):
            engine = DownloadEngine(config.UserConfig())
        engine.set_rate(4 * 1024**2)
        bucket = engine._bucket
        bucket._tokens = -1000
        engine.set_rate(2 * 1024**2)
        self.assertIs(engine._bucket, bucket)
        self.assertEqual(bucket._tokens, -1000)


class QuotaUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_preserve_diagnostic_policy_and_show_effective_margin(self):
        from modules.cherry.logic import settings
        from modules.cherry.ui.settings_dialog import SettingsDialog
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, 'CONFIG_FILE', Path(tmp) / 'config.toml'):
            cfg = config.UserConfig(pawchive=config.PawchiveConfig(adaptive_quota=False, resume_headroom=50))
            config.save_config(cfg)
            values = settings.load()
            settings.save(**values)
            self.assertFalse(config.load_config().pawchive.adaptive_quota)
            dialog = SettingsDialog()
            self.assertEqual(dialog._pawchive_headroom.minimum(), 0)
            self.assertEqual(dialog._pawchive_headroom.value(), 50)
            dialog.close()
            cfg.pawchive.adaptive_quota = True
            config.save_config(cfg)
            dialog = SettingsDialog()
            self.assertEqual(dialog._pawchive_headroom.minimum(), 100)
            self.assertEqual(dialog._pawchive_headroom.value(), 100)
            dialog.close()

    def test_raw_ui_does_not_count_in_flight_bytes_twice_and_has_two_timers(self):
        from third_party.cherry_dl.site_throttle import QuotaSnapshot
        with patch.object(DownloadPanel, 'refresh_pawchive_quota'):
            panel = DownloadPanel()
        try:
            state = QuotaSnapshot('pawchive', 501, 500, 5 * 2**30, 25 * 2**30, 2, 7200, 60,
                resume_at_files=400, raw_bytes=True, mode='economic', adaptive=True,
                file_release_seconds=60, byte_release_seconds=120,
                file_recovery_seconds=300)
            panel._set_quota(state, 2**30)
            text = panel._quota.text()
            self.assertIn('5.000/25 GiB', text)
            self.assertIn('20.000', text)
            self.assertIn('501/500', text)
            self.assertIn('1 min', text)
            self.assertIn('2 min', text)
            self.assertIn('5 min', text)
        finally:
            for timer in (panel._quota_timer, panel._status_timer, panel._cooldown_timer):
                timer.stop()
            panel.close()

    def test_module_load_view_unload_and_tooltips(self):
        from modules.cherry.module import CherryModule
        from PySide6.QtWidgets import QPushButton, QToolButton, QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QSlider
        with patch.object(DownloadPanel, 'refresh_pawchive_quota'):
            module = CherryModule()
            module.on_load({})
            view = module.get_view()
            try:
                self.assertIs(module.get_view(), view)
                controls = (QPushButton, QToolButton, QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QSlider)
                widgets = [w for kind in controls for w in view.findChildren(kind)]
                outer = [w for w in widgets if not isinstance(w.parent(), (QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit))]
                self.assertTrue(outer)
                self.assertTrue(all(w.toolTip().strip() for w in outer))
            finally:
                module.on_unload()
                view.close()
                self.assertTrue(module.shutdown_ready())


if __name__ == '__main__':
    unittest.main()
