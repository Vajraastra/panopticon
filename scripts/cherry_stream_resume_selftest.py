"""Unknown HTTP lengths and quota-paused Range resumes; real local TCP, private state."""

import asyncio
from contextlib import ExitStack
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from third_party.cherry_dl.config import NetworkConfig, UserConfig
from third_party.cherry_dl.engine import DownloadEngine, ErrorKind
from third_party.cherry_dl.raw_read_guard import BoundedStream, raw_capacity
from third_party.cherry_dl.raw_traffic import RawQuotaExceeded
from third_party.cherry_dl.site_throttle import SiteThrottle


class StreamResume(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 1000.25
        self.requests = []
        self.pauses = []
        self.received = []
        self.samples = []
        self.payload = bytes(range(256)) * 384
        self.style = 'chunked'
        self.ignore_range = False
        self.wrong_range = False
        self.changed = False
        self.cancel_pause = False
        self.missing = False
        self.original_payload = b'published original' * 800
        self.post_checks = 0
        self.cancelled = asyncio.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args):
                pass
            def do_GET(self):
                if '/post/' in self.path:
                    owner.post_checks += 1
                    page = b"<html>We haven't archived this post yet. Previews only.</html>"
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(page)))
                    self.end_headers()
                    self.wfile.write(page)
                    return
                if self.path == '/missing' and owner.missing:
                    self.send_response(404)
                    self.send_header('Transfer-Encoding', 'chunked')
                    self.end_headers()
                    self.wfile.write(b'0\r\n\r\n')
                    return
                offset = int(self.headers.get('Range', 'bytes=0-').split('=')[1].split('-')[0])
                validator = '"changed"' if owner.changed else '"stable"'
                if owner.ignore_range or (self.headers.get('If-Range') and self.headers['If-Range'] != validator):
                    offset = 0
                owner.requests.append((offset, self.headers.get('If-Range'), self.headers.get('Accept-Encoding')))
                content = owner.original_payload if self.path == '/missing' else owner.payload
                payload = content[offset:]
                if offset >= len(content):
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{len(content)}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                self.send_response(206 if offset else 200)
                self.send_header('ETag', validator)
                if offset:
                    start = offset + 1 if owner.wrong_range else offset
                    self.send_header('Content-Range', f'bytes {start}-{len(content)-1}/{len(content)}')
                if owner.style == 'chunked':
                    self.send_header('Transfer-Encoding', 'chunked')
                elif owner.style == 'known':
                    self.send_header('Content-Length', str(len(payload)))
                else:
                    self.send_header('Connection', 'close')
                    self.close_connection = True
                self.end_headers()
                try:
                    for start in range(0, len(payload), 2048):
                        chunk = payload[start:start+2048]
                        if owner.style == 'chunked':
                            self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
                        else:
                            self.wfile.write(chunk)
                    if owner.style == 'chunked':
                        self.wfile.write(b'0\r\n\r\n')
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.temp.cleanup()

    def gate(self, budget):
        async def sleep(seconds):
            self.now += seconds + .01
            await asyncio.sleep(0)
        return SiteThrottle('pawchive', limit=500, byte_limit=budget, raw_traffic=True,
            adaptive=True, window_seconds=100, failure_threshold=1, failure_window_seconds=0,
            cooldowns=(0,), path=self.root / 'quota.json', clock=lambda: self.now, sleeper=sleep)

    async def download(self, *, budget=65536, expected=True, timeout=None, hook=None):
        self.quota = self.gate(budget)
        self.permit = await self.quota.acquire(expected_bytes=None)
        cfg = UserConfig(network=NetworkConfig(delay_min=0, delay_max=0, retries_file=2))
        async def pause():
            path = self.root / 'out.bin.tmp'
            self.assertTrue(path.exists())
            partial = path.read_bytes()
            self.pauses.append(len(partial))
            self.assertEqual(partial, self.payload[:len(partial)])
            self.assertEqual(self.quota.snapshot().used_files, 0)
            if hook:
                hook()
            if self.cancel_pause:
                self.cancelled.set()
                await asyncio.Event().wait()
            await self.quota.refill(self.permit)
        def received(amount):
            self.quota.record_raw_bytes(self.permit, amount)
            self.received.append(amount)
            snapshot = self.quota.snapshot()
            self.samples.append(snapshot.used_bytes)
            self.assertLessEqual(snapshot.used_bytes, budget)
        with patch('third_party.cherry_dl.engine.load_session', return_value={}):
            async with DownloadEngine(cfg) as engine:
                result = await engine.download(f'http://127.0.0.1:{self.server.server_port}/file',
                    self.root, 'out.bin', on_response_size=lambda n: self.quota.resize(self.permit, n),
                    on_raw_bytes=received, on_raw_capacity=lambda: self.permit.reserved_bytes,
                    on_quota_pause=pause, total_timeout=timeout,
                    expected_hash=hashlib.sha256(self.payload).hexdigest() if expected else None)
        await self.quota.finish(self.permit, success=result.ok, transferred_bytes=result.file_size)
        return result

    async def test_unknown_chunked_file_completes_without_false_deferral(self):
        self.payload = self.payload[:20000]
        result = await self.download()
        self.assertTrue(result.ok, result.error)
        self.assertEqual((self.root / 'out.bin').read_bytes(), self.payload)
        self.assertEqual(self.pauses, [])
        self.assertEqual(sum(self.received), len(self.payload))
        self.assertEqual(self.quota.snapshot().used_files, 1)

    async def test_close_delimited_file_completes_without_content_length(self):
        self.style = 'close'
        self.payload = self.payload[:20000]
        result = await self.download()
        self.assertTrue(result.ok, result.error)
        self.assertEqual((self.root / 'out.bin').read_bytes(), self.payload)

    async def test_unknown_file_pauses_at_quota_and_resumes_exact_range(self):
        result = await self.download()
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.pauses, [65536])
        self.assertEqual([n for n, _, _ in self.requests], [0, 65536])
        self.assertEqual(self.requests[1][1], '"stable"')
        self.assertTrue(all(encoding == 'identity' for _, _, encoding in self.requests))
        self.assertEqual(sum(self.received), len(self.payload))
        self.assertEqual((self.root / 'out.bin').read_bytes(), self.payload)
        self.assertEqual(self.quota.snapshot().used_files, 1)
        self.assertFalse((self.root / 'out.bin.tmp').exists())

    async def test_multiple_quota_pauses_keep_one_file_and_all_received_bytes(self):
        result = await self.download(budget=32768)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.pauses, [32768, 65536])
        self.assertEqual(sum(self.received), len(self.payload))
        self.assertEqual((self.root / 'out.bin').read_bytes(), self.payload)
        self.assertEqual(self.quota.snapshot().used_files, 1)

    async def test_quota_wait_is_excluded_from_engine_total_timeout(self):
        import httpx
        self.quota = self.gate(100)
        permit = await self.quota.acquire(expected_bytes=100)
        calls = 0
        async def size(amount):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RawQuotaExceeded('fixture pause before first body')
            return await self.quota.resize(permit, amount)
        async def pause():
            await asyncio.sleep(.15)
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'x' * 16
        cfg = UserConfig(network=NetworkConfig(delay_min=0, delay_max=0))
        with patch('third_party.cherry_dl.engine.load_session', return_value={}):
            engine = DownloadEngine(cfg)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
                httpx.Response(200, headers={'Content-Length':'16'}, stream=Body()))) as client:
            engine._client = client
            result = await engine.download('https://fixture.test/file', self.root, 'timeout.bin',
                total_timeout=.1, on_response_size=size,
                on_raw_bytes=lambda n:self.quota.record_raw_bytes(permit,n),
                on_raw_capacity=lambda:permit.reserved_bytes, on_quota_pause=pause)
        await self.quota.finish(permit, success=result.ok)
        self.assertTrue(result.ok, result.error)
        self.assertEqual((self.root / 'timeout.bin').read_bytes(), b'x' * 16)
        self.assertGreater(calls, 1)

    async def test_known_header_larger_than_current_budget_can_pause_and_continue(self):
        self.style = 'known'
        result = await self.download()
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.pauses, [65536])
        self.assertEqual((self.root / 'out.bin').read_bytes(), self.payload)

    async def test_complete_sha_at_quota_does_not_request_an_invalid_suffix(self):
        self.payload = self.payload[:65536]
        result = await self.download()
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.pauses, [])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.quota.snapshot().used_files, 1)

    async def test_complete_unknown_body_without_sha_uses_verified_overlap(self):
        self.payload = self.payload[:65536]
        result = await self.download(expected=False)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.pauses, [65536])
        self.assertEqual([n for n, _, _ in self.requests], [0, 65535])
        self.assertEqual(sum(self.received), len(self.payload) + 1)
        self.assertEqual((self.root / 'out.bin').read_bytes(), self.payload)
        self.assertEqual(self.quota.snapshot().used_files, 1)

    async def test_wrong_range_is_rejected_without_publishing_a_mixed_file(self):
        self.wrong_range = True
        result = await self.download()
        self.assertEqual(result.error_kind, ErrorKind.NETWORK)
        self.assertFalse((self.root / 'out.bin').exists())
        self.assertEqual(self.quota.snapshot().used_files, 0)

    async def test_changed_resource_returns_200_and_restarts_instead_of_stitching(self):
        def change():
            self.changed = True
            self.payload = b'changed' * 2000
        result = await self.download(expected=False, hook=change)
        self.assertTrue(result.ok, result.error)
        self.assertEqual((self.root / 'out.bin').read_bytes(), self.payload)
        self.assertEqual(self.quota.snapshot().used_files, 1)

    async def test_cancel_while_paused_keeps_received_traffic_and_no_success(self):
        self.cancel_pause = True
        task = asyncio.create_task(self.download())
        await asyncio.wait_for(self.cancelled.wait(), 3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self.quota.abandon(self.permit)
        self.assertEqual(self.quota.snapshot().used_bytes, 65536)
        self.assertEqual(self.quota.snapshot().used_files, 0)
        self.assertEqual(self.quota.snapshot().reserved_bytes, 0)
        self.assertFalse((self.root / 'out.bin').exists())
        self.assertFalse((self.root / 'out.bin.tmp').exists())

    async def test_unpublished_original_keeps_archive_state_until_explicit_recheck(self):
        from third_party.cherry_dl import config, download_service, live_metrics, site_budget, site_throttle
        from third_party.cherry_dl.catalog import awaiting_archive_count, reactivate_awaiting_archive
        from third_party.cherry_dl.templates import _registry, pawchive
        from third_party.cherry_dl.templates.base import ArtistInfo, FileInfo, SiteTemplate
        from third_party.cherry_dl.site_policy import SourceCategory, ScanPolicy, TransferPolicy

        self.payload = self.payload[:20000]
        self.missing = True
        base = f'http://127.0.0.1:{self.server.server_port}'
        class FixtureTemplate(SiteTemplate):
            name = 'pawchive'
            category = SourceCategory.REPOSITORY
            scan_policy = ScanPolicy(cooldown_threshold=200)
            transfer_policy = TransferPolicy(workers=2, delay_min=0, delay_max=0)
            @classmethod
            def can_handle(cls, url):
                return url.startswith('fixture://')
            async def get_artist_info(self, url):
                return ArtistInfo('7', 'Artist', 'patreon', 'pawchive', url)
            async def iter_files(self, artist, since=None):
                for name in ('missing', 'available'):
                    yield FileInfo(url=f'{base}/{name}', filename=f'{name}.bin',
                        artist_id='7', artist_name='Artist', post_id=name,
                        url_source=f'fixture://{name}')
            async def classify_not_found(self, artist, info):
                return await pawchive.PawchiveTemplate(self.engine).classify_not_found(artist, info)

        folder = self.root / 'Artist'
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
                (pawchive, '_BASE', base),
            ):
                stack.enter_context(patch.object(obj, key, value))
            cfg = UserConfig(download_dir=str(self.root), workers=2,
                network=NetworkConfig(delay_min=0, delay_max=0, retries_file=1))
            profile = {'id': -1, 'folder_path': str(folder), 'urls': [
                {'id': -1, 'url': 'fixture://artist', 'site': 'pawchive', 'enabled': True,
                 'last_synced': None, 'file_count': 0}]}
            async def run():
                events = []
                result = await asyncio.wait_for(download_service.run_profile_download(profile,
                    workers=2, ext_filter=set(), emit=events.append, config=cfg,
                    force_full=True, finalize_profile=False), 5)
                states = [e.snapshot for e in events if isinstance(e, download_service.QuotaStatus)]
                return result, states[-1]
            first, quota = await run()
            self.assertEqual(first.awaiting_archive, 1)
            self.assertEqual(first.downloaded, 1)
            self.assertEqual(quota.used_files, 1)
            self.assertEqual(await awaiting_archive_count(folder), 1)
            self.assertEqual(self.post_checks, 1)
            second, quota = await run()
            self.assertEqual(second.downloaded, 0)
            self.assertEqual(quota.used_files, 1)
            self.assertEqual(await awaiting_archive_count(folder), 1)
            self.assertEqual(self.post_checks, 1)
            self.missing = False
            self.assertEqual(await reactivate_awaiting_archive(folder, -1, due_only=False), 1)
            third, quota = await run()
            self.assertEqual(third.downloaded, 1)
            self.assertEqual(quota.used_files, 2)
            self.assertEqual(await awaiting_archive_count(folder), 0)


class ReadGuard(unittest.IsolatedAsyncioTestCase):
    async def test_read_is_bounded_and_tls_keeps_the_same_guard(self):
        class Stream:
            def __init__(self):
                self.reads = []
            async def read(self, maximum, timeout=None):
                self.reads.append(maximum)
                return b'x' * maximum
            async def start_tls(self, context, hostname=None, timeout=None):
                return self
        source = Stream()
        remaining = 5
        token = raw_capacity.set(lambda: remaining)
        try:
            stream = BoundedStream(source)
            self.assertEqual(len(await stream.read(100)), 5)
            remaining = 0
            with self.assertRaises(RawQuotaExceeded):
                await stream.read(100)
            self.assertEqual(source.reads, [5])
            remaining = 3
            secure = await stream.start_tls(object(), 'fixture')
            self.assertEqual(len(await secure.read(100)), 3)
        finally:
            raw_capacity.reset(token)

    async def test_concurrent_request_contexts_have_independent_credits(self):
        class Stream:
            async def read(self, maximum, timeout=None):
                await asyncio.sleep(0)
                return b'x' * maximum
        async def read(credit):
            token = raw_capacity.set(lambda: credit)
            try:
                return len(await BoundedStream(Stream()).read(100))
            finally:
                raw_capacity.reset(token)
        self.assertEqual(await asyncio.gather(read(3), read(7)), [3, 7])
        self.assertIsNone(raw_capacity.get())


if __name__ == '__main__':
    unittest.main()
