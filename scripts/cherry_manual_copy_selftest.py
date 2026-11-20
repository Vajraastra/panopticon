"""Private Windows Sync fixtures preserve indexed/manual originals; no network."""
import asyncio
from contextlib import ExitStack
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'third_party'))
from core.library_service import LibraryService
from cherry_dl import catalog, config, download_service as ds, index, live_metrics, site_budget, site_throttle
from cherry_dl.engine import DownloadResult
from cherry_dl.file_routing import COUNTER_NAMESPACES, LAYOUT_VERSION
from cherry_dl.templates import _registry
from cherry_dl.templates.base import ArtistInfo, FileInfo, SiteTemplate

PAYLOAD = b'private manual image and identical downloaded bytes'
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class ManualCopyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cherry-manual-copy-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / 'Artist'
        self.db = self.root / 'index.db'
        await catalog.init_catalog(self.folder)
        await catalog.ensure_layout_version(self.folder, COUNTER_NAMESPACES, LAYOUT_VERSION)
        await index.init_index(self.db)
        self.pid = await index.create_profile(self.db, 'Artist', self.folder, 'manualfixture')
        await index.add_profile_url(self.db, self.pid, 'manualfixture://artist', 'manualfixture')
        self.manual = self.folder / 'manual-original.png'
        self.manual.write_bytes(PAYLOAD)
        self.service = LibraryService(self.root / 'native.db')
        self.addCleanup(self.service.close)
        self.service.submit('register_files_minimal', [dict(path=str(self.manual), filename=self.manual.name,
            size=len(PAYLOAD), created=1, hash_original=DIGEST, origin_url='documented manual copy')]).result(5)
        self.manual_id = self.service.read('get_file_id', str(self.manual))
        self.service.submit('edit_curation', str(self.manual), add_tags=['curated'], rating=5).result(5)
        self.before = self.service.read('get_copy_version_state', self.manual_id)
        self.physical = self.identity(self.manual)
        self.downloads = []
        self.events = []
        owner = self

        class Template(SiteTemplate):
            name = 'manualfixture'
            workers = 1

            @classmethod
            def can_handle(cls, url):
                return url.startswith('manualfixture://')

            async def get_artist_info(self, url):
                return ArtistInfo('artist', 'Artist', 'fixture', self.name, url)

            async def iter_files(self, artist, since=None):
                yield FileInfo(url='https://fixture.invalid/image', filename='download.png',
                               artist_id='artist', artist_name='Artist', post_id='post-one')

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
                target = Path(dest_dir) / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(PAYLOAD)
                owner.downloads.append(target)
                return DownloadResult(url, filename, target, DIGEST, len(PAYLOAD))

        stack = ExitStack()
        self.addCleanup(stack.close)
        for obj, name, value in (
            (_registry, '_TEMPLATES', [Template]), (ds, 'INDEX_DB', self.db),
            (ds, 'DownloadEngine', Engine), (config, 'INDEX_DB', self.db),
            (config, 'CONFIG_FILE', self.root / 'config.toml'),
            (config, 'SESSION_FILE', self.root / 'session.json'),
            (live_metrics, 'METRICS_FILE', self.root / 'metrics.jsonl'),
            (site_budget, 'BUDGET_FILE', self.root / 'budget.json'),
            (site_throttle, 'THROTTLE_FILE', self.root / 'throttle.json'),
        ):
            stack.enter_context(patch.object(obj, name, value))
        self.config = config.UserConfig(download_dir=str(self.root), workers=1,
            network=config.NetworkConfig(delay_min=0, delay_max=0, retries_api=1, retries_file=1))

    @staticmethod
    def identity(path):
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_birthtime_ns

    async def sync(self):
        profile = await index.get_profile(self.db, self.pid)
        return await asyncio.wait_for(ds.run_profile_download(profile, workers=1, ext_filter=set(),
            config=self.config, force_full=True, emit=self.events.append), 8)

    async def check_preserved_and_catalogued(self):
        self.assertEqual(self.manual.read_bytes(), PAYLOAD)
        self.assertEqual(self.identity(self.manual), self.physical)
        self.assertEqual(self.service.read('get_copy_version_state', self.manual_id), self.before)
        self.assertEqual(self.service.read('get_file_id', str(self.manual)), self.manual_id)
        self.assertEqual(len(self.downloads), 1)
        self.assertNotEqual(self.downloads[0], self.manual)
        self.assertEqual(self.downloads[0].read_bytes(), PAYLOAD)
        files = await catalog.get_all_files(self.folder)
        current = [row for row in files if row['hash'] == DIGEST]
        self.assertEqual(len(current), 1)
        self.assertEqual(self.folder / current[0]['filename'], self.downloads[0])
        self.assertTrue(await catalog.url_exists(self.folder, 'https://fixture.invalid/image'))
        self.assertEqual(await catalog.get_pending_files(self.folder), [])

    async def test_manual_matching_hash_keeps_original_id_and_catalogues_new_download(self):
        result = await self.sync()
        self.assertEqual((result.downloaded, result.errors), (1, 0))
        await self.check_preserved_and_catalogued()

    async def test_sync_never_attempts_replace_or_unlink_of_manual_original(self):
        attempted_replace, attempted_unlink = [], []
        replace, unlink = Path.replace, Path.unlink

        def replace_spy(source, target):
            if source == self.manual:
                attempted_replace.append((source, target))
                raise OSError('injected manual-original replace failure')
            return replace(source, target)

        def unlink_spy(source, *args, **kwargs):
            if source == self.manual:
                attempted_unlink.append(source)
                raise OSError('protected manual original')
            return unlink(source, *args, **kwargs)

        with patch.object(Path, 'replace', replace_spy), patch.object(Path, 'unlink', unlink_spy):
            result = await self.sync()
        self.assertEqual(attempted_replace, [])
        self.assertEqual(attempted_unlink, [])
        self.assertEqual((result.downloaded, result.errors), (1, 0))
        await self.check_preserved_and_catalogued()

    async def test_sync_does_not_reorganize_existing_indexed_project_file(self):
        legacy = self.folder / 'legacy-project.psd'
        content = b'private existing layered project bytes'
        legacy.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        self.service.submit('register_files_minimal', [dict(path=str(legacy), filename=legacy.name,
            size=len(content), created=1, hash_original=digest)]).result(5)
        key = self.service.read('get_file_id', str(legacy))
        before = self.service.read('get_copy_version_state', key)
        physical = self.identity(legacy)
        await catalog.add_file(self.folder, digest, legacy.name, 'manual://layered-project', len(content), 1)
        organizer = AsyncMock(side_effect=AssertionError('Sync must preserve existing layout'))
        with patch.object(ds, 'organize_project', organizer, create=True):
            result = await self.sync()
        organizer.assert_not_awaited()
        self.assertEqual((result.downloaded, result.errors), (1, 0))
        self.assertEqual(legacy.read_bytes(), content)
        self.assertEqual(self.identity(legacy), physical)
        self.assertEqual(self.service.read('get_copy_version_state', key), before)
        self.assertEqual(self.service.read('get_file_id', str(legacy)), key)
        self.assertFalse((self.folder / 'Proyectos' / legacy.name).exists())
        files = await catalog.get_all_files(self.folder)
        self.assertEqual([row['filename'] for row in files if row['hash'] == digest], [legacy.name])
        await self.check_preserved_and_catalogued()


if __name__ == '__main__':
    unittest.main(verbosity=2)
