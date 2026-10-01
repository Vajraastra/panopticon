"""Frontera de descubrimiento real, con DBs privadas y transporte sin red."""
import asyncio
from contextlib import closing, ExitStack
from datetime import datetime
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'third_party'))
from cherry_dl import catalog, download_service as ds, index
from cherry_dl.config import UserConfig, NetworkConfig
from cherry_dl.engine import DownloadResult
from cherry_dl.templates import _registry
from cherry_dl.templates.base import ArtistInfo, FileInfo, SiteTemplate

OLD = '2026-09-25 12:00:00'
START = '2026-10-01 12:00:00'


class FrontierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cherry-frontier-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = self.root / 'portátil ñ'
        self.db = self.root / 'index.db'
        await catalog.init_catalog(self.folder)
        await index.init_index(self.db)
        self.pid = await index.create_profile(self.db, 'Artist', self.folder, 'frontierfixture')
        self.uid = await index.add_profile_url(self.db, self.pid, 'frontierfixture://artist', 'frontierfixture')
        self.items = ['one', 'two']
        self.since = []
        self.calls = 0
        self.downloads = []
        self.events = []
        self.fail = None
        self.fail_by_url = {}
        self.artist_fail = False
        self.pause = None
        self.entered = asyncio.Event()
        self.download_pause = None
        self.download_entered = asyncio.Event()
        self.download_fail = False
        owner = self

        class Template(SiteTemplate):
            name = 'frontierfixture'
            workers = 1
            @classmethod
            def can_handle(cls, url): return url.startswith('frontierfixture://')
            async def get_artist_info(self, url):
                if owner.artist_fail:
                    raise RuntimeError('artist failed')
                return ArtistInfo('fixture', 'Artist', 'fixture', self.name, url)
            async def iter_files(self, artist, since=None):
                owner.since.append(since)
                owner.calls += 1
                for position, item in enumerate(owner.items):
                    published = datetime(2026, 10, 1, 12, 0, 0)
                    if since and published < since:
                        continue
                    yield FileInfo(url='https://fixture.invalid/'+item, filename=item+'.png',
                                   artist_id='fixture', artist_name='Artist', post_id=item,
                                   date_published=published.isoformat()+'Z')
                    if position == 0:
                        owner.entered.set()
                        if owner.pause:
                            await owner.pause.wait()
                        error = owner.fail(owner.calls) if callable(owner.fail) else owner.fail
                        error = owner.fail_by_url.get(artist.url, error)
                        if error:
                            raise error

        class Engine:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def set_workers(self, *args): pass
            def set_scan_policy(self, *args): pass
            def set_transfer_policy(self, *args): pass
            def set_pace(self, *args): pass
            def set_rate(self, *args): pass
            def set_completion_gap(self, *args): pass
            async def download(self, *, url, dest_dir, filename, **kwargs):
                owner.downloads.append(url)
                owner.download_entered.set()
                if owner.download_pause:
                    await owner.download_pause.wait()
                if owner.download_fail:
                    return DownloadResult(url, filename, error='private transfer failure', error_kind='unknown')
                payload = url.encode()
                path = Path(dest_dir) / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                return DownloadResult(url, filename, path, hashlib.sha256(payload).hexdigest(), len(payload))

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(_registry, '_TEMPLATES', [Template]))
        stack.enter_context(patch.object(ds, 'INDEX_DB', self.db))
        stack.enter_context(patch.object(ds, 'DownloadEngine', Engine))
        stack.enter_context(patch.object(ds, '_scan_watermark', return_value=START))
        self.config = UserConfig(download_dir=str(self.root), workers=1,
            network=NetworkConfig(delay_min=0, delay_max=0, retries_api=1, retries_file=1))

    def sql(self, statement, args=(), *, catalog_db=False):
        with closing(sqlite3.connect(self.folder/'catalog.db' if catalog_db else self.db)) as conn:
            rows = conn.execute(statement, args).fetchall()
            conn.commit()
            return rows

    async def seed(self, *, full=True):
        await index.update_profile_url_sync(self.db, self.uid, last_synced=OLD)
        if full:
            await catalog.set_meta_int(self.folder, 'full_scan_done', 1)

    async def frontier(self):
        return (await index.get_profile(self.db, self.pid))['urls'][0]['last_synced']

    async def pending(self):
        return await catalog.get_pending_files(self.folder, self.uid)

    async def run_service(self, **kwargs):
        profile = await index.get_profile(self.db, self.pid)
        options = dict(workers=1, ext_filter=set(), scan_only=True,
                       config=self.config, emit=self.events.append)
        options.update(kwargs)
        return await ds.run_profile_download(profile, **options)

    def task(self, **kwargs):
        task = asyncio.create_task(self.run_service(**kwargs))
        async def close_task():
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.addAsyncCleanup(close_task)
        return task

    async def test_artist_and_count_do_not_create_or_advance_frontier(self):
        await index.update_profile_url_sync(self.db, self.uid, artist_id='known', file_count=7)
        self.assertIsNone(await self.frontier())
        await self.seed()
        await index.update_profile_url_sync(self.db, self.uid, artist_id='updated', file_count=8)
        self.assertEqual(await self.frontier(), OLD)
        row = (await index.get_profile(self.db, self.pid))['urls'][0]
        self.assertEqual((row['artist_id'], row['file_count']), ('updated', 8))
        portable = await catalog.read_profile_meta(self.folder)
        self.assertEqual(portable['urls'][0]['last_synced'], OLD)

    async def test_start_watermark_only_after_pending_is_durable(self):
        await self.seed()
        update = index.update_profile_url_sync
        evidence = []
        async def guarded(*args, **kwargs):
            if kwargs.get('last_synced'):
                evidence.append(len(await self.pending()))
                self.assertEqual(await self.frontier(), OLD)
            return await update(*args, **kwargs)
        with patch.object(ds, 'update_profile_url_sync', guarded):
            await self.run_service()
        self.assertEqual(evidence, [2])
        self.assertEqual(await self.frontier(), START)
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.since, [datetime.fromisoformat(OLD)])

    async def test_empty_complete_history_is_a_success(self):
        self.items = []
        await self.run_service()
        self.assertEqual(await self.frontier(), START)
        self.assertEqual(await catalog.get_meta_int(self.folder, 'full_scan_done'), 1)

    async def test_partial_error_preserves_frontier_and_resume_keeps_both_pages(self):
        await self.seed()
        self.fail = RuntimeError('second page failed')
        await self.run_service()
        self.assertEqual(await self.frontier(), OLD)
        self.assertEqual(len(await self.pending()), 1)
        self.fail = None
        await self.run_service()
        self.assertEqual(await self.frontier(), START)
        self.assertEqual(len(await self.pending()), 2)
        self.assertEqual(self.since, [datetime.fromisoformat(OLD)]*2)

    async def test_cancel_during_pagination_flushes_partial_without_advancing(self):
        await self.seed()
        self.pause = asyncio.Event()
        task = self.task()
        await asyncio.wait_for(self.entered.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(await self.frontier(), OLD)
        self.assertEqual(len(await self.pending()), 1)
        self.assertFalse(any(t.get_name() == 'cherry-scan-catalog-close' for t in asyncio.all_tasks()))

    async def test_cancel_during_close_is_drained_even_when_repeated(self):
        await self.seed()
        entered, release = asyncio.Event(), asyncio.Event()
        original = catalog.ScanCatalogBatch.close
        async def close(batch):
            entered.set()
            await release.wait()
            await original(batch)
        with patch.object(catalog.ScanCatalogBatch, 'close', close):
            task = self.task()
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(await self.frontier(), OLD)
            release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(len(await self.pending()), 2)
        self.assertFalse(any(t.get_name() == 'cherry-scan-catalog-close' for t in asyncio.all_tasks()))

    async def test_flush_failure_cannot_commit_coverage_or_frontier(self):
        await self.seed(full=False)
        self.sql("CREATE TRIGGER reject_pending BEFORE INSERT ON pending_queue BEGIN SELECT RAISE(ABORT,'flush failed'); END", catalog_db=True)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'flush failed'):
            await self.run_service()
        self.assertEqual(await self.frontier(), OLD)
        self.assertIsNone(await catalog.get_meta_int(self.folder, 'full_scan_done'))
        self.assertEqual(await self.pending(), [])
        self.sql('DROP TRIGGER reject_pending', catalog_db=True)
        await self.run_service()
        self.assertEqual(await self.frontier(), START)

    async def test_index_failure_replays_already_durable_catalog(self):
        await self.seed()
        update = index.update_profile_url_sync
        async def fail_commit(*args, **kwargs):
            if kwargs.get('last_synced'): raise RuntimeError('index commit failed')
            return await update(*args, **kwargs)
        with patch.object(ds, 'update_profile_url_sync', fail_commit):
            with self.assertRaisesRegex(RuntimeError, 'index commit failed'):
                await self.run_service()
        self.assertEqual(await self.frontier(), OLD)
        self.assertEqual(len(await self.pending()), 2)
        await self.run_service()
        self.assertEqual(len(await self.pending()), 2)
        self.assertEqual(await self.frontier(), START)

    async def test_legacy_timestamp_cannot_invent_historical_coverage(self):
        await self.seed(full=False)
        self.fail = RuntimeError('partial')
        await self.run_service()
        self.assertEqual(self.since, [None])
        self.assertIsNone(await catalog.get_meta_int(self.folder, 'full_scan_done'))
        self.assertEqual(await self.frontier(), OLD)
        self.fail = None
        await self.run_service()
        self.assertEqual(self.since, [None, None])
        self.assertEqual(await catalog.get_meta_int(self.folder, 'full_scan_done'), 1)

    async def test_failed_forced_rescan_keeps_previous_frontier(self):
        await self.seed()
        self.fail = RuntimeError('partial')
        await self.run_service(force_full=True)
        self.assertEqual(self.since, [None])
        self.assertEqual(await self.frontier(), OLD)

    async def test_artist_failure_never_advances(self):
        await self.seed()
        self.artist_fail = True
        await self.run_service()
        self.assertEqual(await self.frontier(), OLD)
        self.assertEqual(self.calls, 0)

    async def test_auth_denial_and_repeated_failure_keep_frontier(self):
        await self.seed()
        self.fail = ds.NeedsManualAuth('private auth')
        async def deny(*args): return False
        await self.run_service(resolve_auth=deny)
        self.assertEqual(await self.frontier(), OLD)
        async def allow(*args): return True
        await self.run_service(resolve_auth=allow)
        self.assertEqual(await self.frontier(), OLD)
        self.assertEqual(len(await self.pending()), 1)

    async def test_auth_retry_complete_commits_once_without_duplicate_pending(self):
        await self.seed()
        self.fail = lambda n: ds.NeedsManualAuth('renew') if n == 1 else None
        async def allow(*args): return True
        await self.run_service(resolve_auth=allow)
        self.assertEqual(self.calls, 2)
        self.assertEqual(len(await self.pending()), 2)
        self.assertEqual(await self.frontier(), START)

    async def test_extension_filter_does_not_drop_discovered_pending(self):
        await self.seed()
        await self.run_service(scan_only=False, ext_filter={'.zip'})
        self.assertEqual(await self.frontier(), START)
        self.assertEqual(len(await self.pending()), 2)
        self.assertEqual(self.downloads, [])

    async def test_transfer_completion_does_not_move_start_to_finish(self):
        await self.seed()
        result = await self.run_service(scan_only=False)
        self.assertEqual(result.downloaded, 2)
        self.assertEqual(await self.frontier(), START)
        self.assertEqual(await self.pending(), [])
        self.assertEqual((await index.get_profile(self.db, self.pid))['urls'][0]['file_count'], 2)

    async def test_transfer_failure_keeps_completed_discovery_and_queue(self):
        await self.seed()
        self.download_fail = True
        result = await self.run_service(scan_only=False)
        self.assertEqual(result.errors, 2)
        self.assertEqual(await self.frontier(), START)
        self.assertEqual(len(await self.pending()), 2)

    async def test_transfer_cancel_keeps_completed_discovery_and_queue(self):
        await self.seed()
        self.download_pause = asyncio.Event()
        task = self.task(scan_only=False)
        await asyncio.wait_for(self.download_entered.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(await self.frontier(), START)
        self.assertEqual(len(await self.pending()), 2)
        self.assertFalse(any(t.get_name() in {'producer', 'worker-0'} for t in asyncio.all_tasks()))

    async def test_exact_start_boundary_is_included_on_next_sync(self):
        await self.seed()
        self.items = ['first']
        await self.run_service()
        self.items = ['boundary']
        await self.run_service()
        self.assertEqual(self.since[-1], datetime.fromisoformat(START))
        self.assertEqual(len(await self.pending()), 2)

    async def test_bad_watermark_rejects_before_artist_or_count_update(self):
        await self.seed()
        before = self.sql('SELECT * FROM profile_urls')
        for invalid in [1, '', '2026-1-1 01:02:03', START+'Z', '2026-13-01 12:00:00']:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, 'invalid_completed_scan_watermark'):
                    await index.update_profile_url_sync(self.db, self.uid, 'changed', 8, last_synced=invalid)
                self.assertEqual(self.sql('SELECT * FROM profile_urls'), before)

    async def test_concurrent_completions_keep_maximum_frontier_without_ddl(self):
        await self.seed()
        schema = self.sql('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')
        await asyncio.gather(*(index.update_profile_url_sync(self.db, self.uid, last_synced=value)
                               for value in [START, OLD, '2026-10-02 01:00:00', START]))
        self.assertEqual(await self.frontier(), '2026-10-02 01:00:00')
        self.assertEqual(self.sql('SELECT type,name,sql FROM sqlite_master ORDER BY type,name'), schema)

    async def test_aware_legacy_frontier_compares_in_utc(self):
        self.sql('UPDATE profile_urls SET last_synced=? WHERE id=?', ('2026-10-01T10:30:00-02:00', self.uid))
        await index.update_profile_url_sync(self.db, self.uid, last_synced=START)
        self.assertEqual(await self.frontier(), '2026-10-01T10:30:00-02:00')
        await index.update_profile_url_sync(self.db, self.uid, last_synced='2026-10-01 13:00:00')
        self.assertEqual(await self.frontier(), '2026-10-01 13:00:00')

    async def test_invalid_legacy_date_is_replaced_only_by_successful_scan(self):
        self.sql('UPDATE profile_urls SET last_synced=? WHERE id=?', ('invalid legacy', self.uid))
        await index.update_profile_url_sync(self.db, self.uid, artist_id='known')
        self.assertEqual(await self.frontier(), 'invalid legacy')
        await self.run_service()
        self.assertEqual(await self.frontier(), START)

    async def test_each_source_commits_its_own_success_or_keeps_its_old_frontier(self):
        await self.seed()
        second = await index.add_profile_url(self.db, self.pid,
                                             'frontierfixture://second', 'frontierfixture')
        await index.update_profile_url_sync(self.db, second, last_synced=OLD)
        self.fail_by_url['frontierfixture://second'] = RuntimeError('partial second source')
        await self.run_service()
        rows = (await index.get_profile(self.db, self.pid))['urls']
        self.assertEqual({r['id']: r['last_synced'] for r in rows}, {self.uid: START, second: OLD})

    async def test_portable_mirror_failure_keeps_conservative_old_frontier(self):
        await self.seed()
        async def fail_mirror(*args): raise OSError('private mirror failure')
        with patch.object(catalog, 'write_profile_meta', fail_mirror):
            await self.run_service()
        self.assertEqual(await self.frontier(), START)
        self.assertEqual((await catalog.read_profile_meta(self.folder))['urls'][0]['last_synced'], OLD)
        self.assertEqual(len(await self.pending()), 2)
        await index.sync_profile_meta(self.db, self.pid)
        self.assertEqual((await catalog.read_profile_meta(self.folder))['urls'][0]['last_synced'], START)


if __name__ == '__main__':
    unittest.main(verbosity=2)
