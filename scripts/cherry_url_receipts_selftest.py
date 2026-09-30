"""Historial portable y dos sesiones reales del servicio, sin red ni biblioteca real."""
import asyncio
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'third_party'))
from cherry_dl import catalog, download_service as ds
from cherry_dl.config import UserConfig, NetworkConfig
from cherry_dl.engine import DownloadResult
from cherry_dl.file_routing import COUNTER_NAMESPACES, LAYOUT_VERSION
from cherry_dl.templates import _registry
from cherry_dl.templates.base import FileInfo, ArtistInfo, SiteTemplate
from cherry_dl.url_receipts import remember_downloaded_url, merge_download_history

PAYLOAD = b'curated image bytes unchanged'
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cherry-receipt-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / 'portátil ñ'
        await catalog.init_catalog(self.folder)
        await catalog.ensure_layout_version(self.folder, COUNTER_NAMESPACES, LAYOUT_VERSION)
        self.image = self.folder / 'Artist_00001.png'
        self.image.write_bytes(PAYLOAD)
        await catalog.add_file(self.folder, DIGEST, self.image.name, 'source-first', len(PAYLOAD), 1)

    def sql(self, statement, args=(), folder=None):
        with closing(sqlite3.connect((folder or self.folder) / 'catalog.db')) as conn:
            rows = conn.execute(statement, args).fetchall()
            conn.commit()
            return rows

    def info(self, source='source-second', **kwargs):
        return FileInfo(url=source, filename='copy.png', artist_id='fixture', artist_name='fixture', post_id='', **kwargs)

    async def batch(self, folder=None, **kwargs):
        value = await catalog.ScanCatalogBatch.open(folder or self.folder, **kwargs)
        self.addAsyncCleanup(value.close)
        return value

    async def test_duplicate_without_logical_identity_is_remembered(self):
        fi = self.info()
        self.assertTrue(await ds._remember_content_alias(self.folder, fi, DIGEST))
        self.assertTrue(await catalog.url_exists(self.folder, fi.dedup_key, include_previews=False))
        self.assertEqual((await self.batch()).find_downloaded_hash(fi.dedup_key), DIGEST)
        self.assertEqual(self.image.read_bytes(), PAYLOAD)
        self.assertEqual(self.sql('SELECT filename,url_source,counter FROM files'), [(self.image.name, 'source-first', 1)])

    async def test_catalog_guards_do_not_require_physical_copy(self):
        await remember_downloaded_url(self.folder, 'source-second', DIGEST)
        self.image.unlink()
        self.assertTrue(await catalog.url_exists(self.folder, 'source-first'))
        self.assertTrue(await catalog.url_exists(self.folder, 'source-second'))
        self.assertTrue(await catalog.hash_exists(self.folder, DIGEST))
        batch = await self.batch()
        self.assertEqual(batch.find_downloaded_hash('source-second'), DIGEST)
        self.assertEqual(self.sql('SELECT count(*) FROM files'), [(1,)])

    async def test_add_file_same_hash_preserves_both_urls_and_first_receipt(self):
        old = self.sql('SELECT * FROM files')
        await catalog.add_file(self.folder, DIGEST, 'would-replace.png', 'source-second', 99, 99)
        self.assertEqual(self.sql('SELECT * FROM files'), old)
        self.assertTrue(await catalog.url_exists(self.folder, 'source-second'))
        self.assertEqual(self.sql('SELECT count(*) FROM downloaded_url_receipts'), [(1,)])

    async def test_primary_url_and_repeated_alias_do_not_write_noops(self):
        self.assertTrue(await remember_downloaded_url(self.folder, 'source-first', DIGEST))
        self.assertEqual(self.sql('SELECT * FROM downloaded_url_receipts'), [])
        await remember_downloaded_url(self.folder, 'source-second', DIGEST)
        before = self.sql('SELECT * FROM downloaded_url_receipts')
        await remember_downloaded_url(self.folder, 'source-second', DIGEST)
        self.assertEqual(self.sql('SELECT * FROM downloaded_url_receipts'), before)

    async def test_unknown_hash_cannot_invent_receipt(self):
        self.assertFalse(await remember_downloaded_url(self.folder, 'unknown', 'b'*64))
        self.assertEqual(self.sql('SELECT * FROM downloaded_url_receipts'), [])
        with self.assertRaisesRegex(ValueError, 'download_url_history_unproven'):
            await ds._remember_content_alias(self.folder, self.info('unknown'), 'b'*64)

    async def test_preview_cannot_block_full_download(self):
        await catalog.add_file(self.folder, 'b'*64, 'preview.png', 'preview-url', is_preview=True)
        self.assertFalse(await remember_downloaded_url(self.folder, 'preview-alias', 'b'*64))
        self.assertFalse(await catalog.url_exists(self.folder, 'preview-url', include_previews=False))
        self.assertFalse((await self.batch()).is_downloaded('preview-url'))
        self.assertFalse(await ds._remember_content_alias(self.folder, self.info('preview-alias', is_preview=True), 'b'*64))

    async def test_preview_upgrades_to_full_without_remembering_old_preview_url(self):
        await catalog.add_file(self.folder, 'b'*64, 'preview.png', 'preview-url', is_preview=True)
        await catalog.add_file(self.folder, 'b'*64, 'full.png', 'full-url', is_preview=False)
        self.assertTrue(await catalog.url_exists(self.folder, 'full-url', include_previews=False))
        self.assertFalse(await catalog.url_exists(self.folder, 'preview-url', include_previews=False))
        self.assertEqual(self.sql('SELECT filename,is_preview FROM files WHERE hash=?', ('b'*64,)), [('full.png', 0)])

    async def test_archive_members_keep_parent_provenance_without_replacing_url_identity(self):
        await catalog.add_file(self.folder, 'b'*64, 'member-one.png', 'source-first', content_role='archive_member')
        await catalog.add_file(self.folder, 'c'*64, 'member-two.png', 'source-first', content_role='archive_member')
        batch = await self.batch()
        self.assertEqual(batch.find_downloaded_hash('source-first'), DIGEST)
        self.assertTrue(batch.is_downloaded('new-remote', 'b'*64))
        self.assertTrue(await remember_downloaded_url(self.folder, 'member-remote-url', 'b'*64))
        self.assertTrue(await catalog.url_exists(self.folder, 'member-remote-url'))
        destination = await self.destination()
        await merge_download_history(self.folder, destination)
        self.assertEqual((await self.batch(destination)).find_downloaded_hash('source-first'), DIGEST)
        self.assertEqual(self.sql('SELECT count(*) FROM files', folder=destination), [(3,)])

    async def test_same_url_different_hash_refuses_and_rolls_back_file_insert(self):
        with self.assertRaisesRegex(ValueError, 'download_url_history_conflict'):
            await catalog.add_file(self.folder, 'b'*64, 'new.png', 'source-first')
        self.assertEqual(self.sql('SELECT hash FROM files'), [(DIGEST,)])
        self.assertEqual(self.image.read_bytes(), PAYLOAD)

    async def test_alias_conflict_is_not_overwritten(self):
        await remember_downloaded_url(self.folder, 'alias', DIGEST)
        await catalog.add_file(self.folder, 'b'*64, 'other.png', 'other-source')
        old = self.sql('SELECT * FROM downloaded_url_receipts')
        with self.assertRaisesRegex(ValueError, 'download_url_history_conflict'):
            await remember_downloaded_url(self.folder, 'alias', 'b'*64)
        self.assertEqual(self.sql('SELECT * FROM downloaded_url_receipts'), old)

    async def test_concurrent_receipts_are_serialized_without_duplicates(self):
        results = await asyncio.gather(*(remember_downloaded_url(self.folder, 'alias', DIGEST) for _ in range(6)))
        self.assertEqual(results, [True]*6)
        self.assertEqual(self.sql('SELECT count(*) FROM downloaded_url_receipts'), [(1,)])

    async def test_scan_batch_flushes_new_urls_at_boundary_and_reopens(self):
        batch = await self.batch(batch_size=2)
        await batch.stage_downloaded_url('alias-one', DIGEST)
        self.assertEqual(self.sql('SELECT * FROM downloaded_url_receipts'), [])
        await batch.stage_downloaded_url('alias-two', DIGEST)
        self.assertEqual(batch.write_commits, 1)
        self.assertEqual(self.sql('SELECT count(*) FROM downloaded_url_receipts'), [(2,)])
        await batch.close()
        reopened = await self.batch()
        self.assertEqual(reopened.find_downloaded_hash('alias-one'), DIGEST)
        self.assertEqual(reopened.find_downloaded_hash('alias-two'), DIGEST)

    async def test_scan_receipt_failure_rolls_back_pending_and_retains_buffers(self):
        batch = await self.batch()
        await batch.stage_downloaded_url('alias', DIGEST)
        await batch.stage_pending('new-source', 'download-url', 'new.png')
        self.sql("CREATE TRIGGER reject_receipt BEFORE INSERT ON downloaded_url_receipts BEGIN SELECT RAISE(ABORT,'receipt failed'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'receipt failed'):
            await batch.flush()
        self.assertEqual(batch.write_commits, 0)
        self.assertEqual(self.sql('SELECT count(*) FROM pending_queue'), [(0,)])
        self.assertEqual(self.sql('SELECT count(*) FROM downloaded_url_receipts'), [(0,)])
        self.sql('DROP TRIGGER reject_receipt')
        await batch.flush()
        self.assertEqual(batch.write_commits, 1)
        self.assertTrue(await catalog.url_exists(self.folder, 'alias'))
        self.assertEqual(self.sql('SELECT count(*) FROM pending_queue'), [(1,)])

    async def test_scan_rejects_unproven_hash_and_conflicting_cached_url(self):
        batch = await self.batch()
        self.assertFalse(await batch.stage_downloaded_url('unknown', 'b'*64))
        await catalog.add_file(self.folder, 'b'*64, 'b.png', 'b-url')
        await batch.close()
        batch = await self.batch()
        with self.assertRaisesRegex(ValueError, 'download_url_history_conflict'):
            await batch.stage_downloaded_url('source-first', 'b'*64)
        self.assertEqual(self.sql('SELECT * FROM downloaded_url_receipts'), [])

    async def test_portable_folder_move_keeps_guard_without_old_path(self):
        await remember_downloaded_url(self.folder, 'alias', DIGEST)
        moved = self.root / 'moved portable'
        self.folder.rename(moved)
        self.assertTrue(await catalog.url_exists(moved, 'alias'))
        self.assertEqual((await self.batch(moved)).find_downloaded_hash('alias'), DIGEST)

    async def destination(self):
        folder = self.root / 'destination'
        await catalog.init_catalog(folder)
        await catalog.ensure_layout_version(folder, COUNTER_NAMESPACES, LAYOUT_VERSION)
        return folder

    async def test_merge_preserves_history_without_physical_files_and_counters(self):
        await remember_downloaded_url(self.folder, 'alias', DIGEST)
        await catalog.add_content_alias(self.folder, DIGEST, 'fixture', 'post', 'original.png', 'primary', 0)
        self.image.unlink()
        destination = await self.destination()
        before_meta = self.sql('SELECT * FROM meta ORDER BY key', folder=destination)
        source_state = self.sql('SELECT * FROM files')
        await merge_download_history(self.folder, destination)
        self.assertEqual(self.sql('SELECT filename,counter FROM files', folder=destination), [('_purged_'+DIGEST, None)])
        self.assertTrue(await catalog.url_exists(destination, 'source-first'))
        self.assertTrue(await catalog.url_exists(destination, 'alias'))
        self.assertEqual(await catalog.content_alias_hash(destination, 'fixture', 'post', 'original.png', 0), DIGEST)
        self.assertEqual(self.sql('SELECT * FROM meta ORDER BY key', folder=destination), before_meta)
        self.assertEqual(self.sql('SELECT * FROM files'), source_state)
        self.assertFalse(any(destination.glob('*.png')))

    async def test_profile_merge_retains_duplicate_urls_and_existing_representative(self):
        await remember_downloaded_url(self.folder, 'alias', DIGEST)
        destination = await self.destination()
        curated = destination / 'Curated_00001.png'
        curated.write_bytes(PAYLOAD)
        caption = curated.with_suffix('.txt')
        caption.write_text('curated documentation', encoding='utf-8')
        await catalog.add_file(destination, DIGEST, curated.name, 'curated-url', len(PAYLOAD), 1)
        result = await catalog.migrate_unique_files(self.folder, destination)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['orphaned'], 1)
        self.assertEqual(curated.read_bytes(), PAYLOAD)
        self.assertEqual(caption.read_text(encoding='utf-8'), 'curated documentation')
        self.assertEqual(self.sql('SELECT filename,url_source FROM files', folder=destination), [(curated.name, 'curated-url')])
        for url in ('curated-url', 'source-first', 'alias'):
            self.assertTrue(await catalog.url_exists(destination, url))
        self.assertTrue(self.image.exists())  # El caller decide después qué hacer con el huérfano.

    async def test_profile_merge_imports_purged_receipts_instead_of_forgetting_them(self):
        self.image.unlink()
        destination = await self.destination()
        result = await catalog.migrate_unique_files(self.folder, destination)
        self.assertEqual(result['purged'], 1)
        self.assertEqual(result['errors'], [])
        self.assertTrue(await catalog.hash_exists(destination, DIGEST))
        self.assertTrue(await catalog.url_exists(destination, 'source-first'))

    async def test_profile_merge_moves_unique_file_and_copies_all_aliases(self):
        await remember_downloaded_url(self.folder, 'alias', DIGEST)
        await catalog.add_content_alias(self.folder, DIGEST, 'fixture', 'post', 'original.png', 'primary', 0)
        destination = await self.destination()
        result = await catalog.migrate_unique_files(self.folder, destination)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['moved'], 1)
        self.assertFalse(self.image.exists())
        self.assertTrue(await catalog.url_exists(destination, 'alias'))
        self.assertEqual(await catalog.content_alias_hash(destination, 'fixture', 'post', 'original.png', 0), DIGEST)
        name, = self.sql('SELECT filename FROM files', folder=destination)[0]
        self.assertEqual((destination / name).read_bytes(), PAYLOAD)

    async def test_profile_merge_with_stale_counter_preserves_occupied_copy(self):
        destination = await self.destination()
        await catalog.write_profile_meta(destination, {'display_name': 'Artist'})
        existing = destination / 'Artist_00001.png'
        existing.write_bytes(b'documented destination')
        caption = existing.with_suffix('.txt')
        caption.write_text('curated caption', encoding='utf-8')
        retained = hashlib.sha256(existing.read_bytes()).hexdigest()
        await catalog.add_file(destination, retained, existing.name, 'other-source', counter=1)
        result = await catalog.migrate_unique_files(self.folder, destination)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['moved'], 1)
        self.assertEqual(existing.read_bytes(), b'documented destination')
        self.assertEqual(caption.read_text('utf-8'), 'curated caption')
        self.assertEqual((destination / 'Artist_00002.png').read_bytes(), PAYLOAD)
        self.assertTrue(await catalog.hash_exists(destination, retained))
        self.assertTrue(await catalog.url_exists(destination, 'source-first'))

    async def test_history_url_conflict_rolls_back_entire_import(self):
        destination = await self.destination()
        await catalog.add_file(destination, 'b'*64, 'other.png', 'source-first')
        before = self.sql('SELECT * FROM files', folder=destination)
        with self.assertRaisesRegex(ValueError, 'download_url_history_conflict'):
            await merge_download_history(self.folder, destination)
        self.assertEqual(self.sql('SELECT * FROM files', folder=destination), before)
        self.assertFalse(await catalog.hash_exists(destination, DIGEST))

    async def test_history_logical_conflict_preserves_both_catalogs(self):
        await catalog.add_content_alias(self.folder, DIGEST, 'fixture', 'post', 'original.png', 'primary', 0)
        destination = await self.destination()
        await catalog.add_file(destination, 'b'*64, 'other.png', 'other-source')
        await catalog.add_content_alias(destination, 'b'*64, 'fixture', 'post', 'original.png', 'primary', 0)
        before = self.sql('SELECT * FROM files', folder=destination)
        with self.assertRaisesRegex(ValueError, 'download_content_history_conflict'):
            await merge_download_history(self.folder, destination)
        self.assertEqual(self.sql('SELECT * FROM files', folder=destination), before)
        self.assertEqual(self.sql('SELECT hash FROM files'), [(DIGEST,)])

    async def test_legacy_source_without_receipts_table_is_read_only_compatible(self):
        self.sql('DROP TABLE downloaded_url_receipts')
        destination = await self.destination()
        await merge_download_history(self.folder, destination)
        self.assertTrue(await catalog.url_exists(destination, 'source-first'))
        self.assertNotIn(('downloaded_url_receipts',), self.sql("SELECT name FROM sqlite_master WHERE type='table'"))

    async def test_history_does_not_promote_source_previews(self):
        await catalog.add_file(self.folder, 'b'*64, 'preview.png', 'preview-url', is_preview=True)
        destination = await self.destination()
        await merge_download_history(self.folder, destination)
        self.assertFalse(await catalog.hash_exists(destination, 'b'*64))
        self.assertFalse(await catalog.url_exists(destination, 'preview-url'))

    async def test_repeated_history_merge_is_idempotent(self):
        await remember_downloaded_url(self.folder, 'alias', DIGEST)
        destination = await self.destination()
        await merge_download_history(self.folder, destination)
        before = {table: self.sql(f'SELECT * FROM {table}', folder=destination) for table in ('files', 'meta', 'downloaded_url_receipts', 'content_aliases')}
        await merge_download_history(self.folder, destination)
        self.assertEqual({table: self.sql(f'SELECT * FROM {table}', folder=destination) for table in before}, before)

    async def test_same_database_alias_refuses_without_mutation(self):
        before = self.sql('SELECT * FROM files')
        with self.assertRaisesRegex(ValueError, 'download_history_source_alias'):
            await merge_download_history(self.folder, self.folder)
        self.assertEqual(self.sql('SELECT * FROM files'), before)

    async def test_init_additive_migration_keeps_existing_catalog_and_receipts(self):
        before = self.sql('SELECT * FROM files')
        self.sql('DROP TABLE downloaded_url_receipts')
        await catalog.init_catalog(self.folder)
        await remember_downloaded_url(self.folder, 'alias', DIGEST)
        await catalog.init_catalog(self.folder)
        self.assertEqual(self.sql('SELECT * FROM files'), before)
        self.assertTrue(await catalog.url_exists(self.folder, 'alias'))

    async def test_occupied_name_and_broken_link_are_skipped(self):
        with patch.object(catalog.os.path, 'lexists', side_effect=[True, True, False]) as exists:
            counter, name, _, _ = await ds._reserve_destination(self.folder, 'Artist', 'same.png')
        self.assertEqual(counter, 3)
        self.assertIn('00003', name)
        self.assertEqual(exists.call_count, 3)
        self.assertEqual(self.image.read_bytes(), PAYLOAD)

    async def test_target_collisions_have_a_bound_and_preserve_existing_file(self):
        with patch.object(catalog, 'next_counter', AsyncMock(return_value=1)) as reserve:
            with self.assertRaisesRegex(ValueError, 'download_target_conflict'):
                await ds._reserve_destination(self.folder, 'Artist', 'same.png')
        self.assertEqual(reserve.await_count, 1000)
        self.assertEqual(self.image.read_bytes(), PAYLOAD)

    async def test_real_service_two_sessions_do_not_redownload_post_hash_duplicate(self):
        folder = self.folder
        image = self.image
        hits = []
        events = []
        class Template(SiteTemplate):
            name = 'receiptfixture'
            workers = 1
            @classmethod
            def can_handle(cls, url): return url.startswith('receiptfixture://')
            async def get_artist_info(self, url):
                return ArtistInfo('fixture', 'Artist', 'fixture', self.name, url)
            async def iter_files(self, artist, since=None):
                yield FileInfo(url='https://fixture.invalid/new-url', filename='same.png',
                               artist_id='fixture', artist_name='Artist', post_id='')
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
                hits.append(url)
                path = Path(dest_dir) / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(PAYLOAD)
                return DownloadResult(url, filename, path, DIGEST, len(PAYLOAD))
        profile = dict(id=-1, folder_path=str(folder), urls=[dict(id=-1,
            url='receiptfixture://artist', site=Template.name, enabled=1, last_synced=None, file_count=0)])
        config = UserConfig(download_dir=str(self.root), workers=1,
                            network=NetworkConfig(delay_min=0, delay_max=0, retries_api=1, retries_file=1))
        with patch.object(_registry, '_TEMPLATES', [Template]), patch.object(ds, 'INDEX_DB', self.root/'index.db'), \
                patch.object(ds, 'DownloadEngine', Engine):
            first = await ds.run_profile_download(profile, workers=1, ext_filter=set(), config=config, force_full=True, emit=events.append)
            self.assertEqual((first.downloaded, first.skipped, first.errors), (0, 1, 0))
            self.assertEqual(len(hits), 1)
            second = await ds.run_profile_download(profile, workers=1, ext_filter=set(), config=config, force_full=True, emit=events.append)
            await catalog.add_pending(folder, 'https://fixture.invalid/new-url',
                                      'https://fixture.invalid/new-url', 'same.png', profile_url_id=-1)
            third = await ds.run_profile_download(profile, workers=1, ext_filter=set(), config=config,
                                                  force_full=True, emit=events.append)
            self.assertEqual(third.errors, 0)
            self.assertEqual(await catalog.get_pending_files(folder, -1), [])
            moved_curated = self.root / 'curated-collection.png'
            image.rename(moved_curated)
            fourth = await ds.run_profile_download(profile, workers=1, ext_filter=set(), config=config,
                                                   force_full=True, emit=events.append)
            self.assertEqual(fourth.errors, 0)
        self.assertEqual(second.errors, 0)
        self.assertEqual(hits, ['https://fixture.invalid/new-url'])
        self.assertEqual(moved_curated.read_bytes(), PAYLOAD)
        self.assertEqual(self.sql('SELECT count(*) FROM files'), [(1,)])
        self.assertTrue(await catalog.url_exists(folder, 'https://fixture.invalid/new-url'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
