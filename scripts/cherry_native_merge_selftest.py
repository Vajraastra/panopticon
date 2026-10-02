"""Private Cherry/Native catalogs and actual Windows handles; no network or collection."""
import asyncio
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.library_service import LibraryService
from core.file_transfers import recover
from modules.cherry.logic import merge
from third_party.cherry_dl import catalog, config, index
from third_party.cherry_dl.file_routing import LAYOUT_VERSION


class NativeMerge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = LibraryService(self.root/'native.db'); self.addCleanup(self.service.close)
        self.journals = self.root/'journals'
        self.index = self.root/'index.db'
        self.patch = patch.object(config, 'INDEX_DB', self.index); self.patch.start(); self.addCleanup(self.patch.stop)
        await index.init_index(self.index)
        self.src, self.dst = self.root/'newer', self.root/'older'
        ids = []
        for folder in (self.dst, self.src):
            await catalog.init_catalog(folder)
            await catalog.set_meta_int(folder, 'layout_version', LAYOUT_VERSION)
            await catalog.set_meta_int(folder, 'counter:visual', 0)
            ids.append(await index.create_profile(self.index, folder.name, folder, 'patreon'))
        self.pair = dict(id_a=ids[0], id_b=ids[1],name_a='older',name_b='newer',
            folder_a=str(self.dst),folder_b=str(self.src),created_at_a='2026-01-01',created_at_b='2026-02-01')

    async def file(self, name='source.png', data=b'actual private image', *, native=True, caption=False):
        path=self.src/name; path.write_bytes(data)
        sha=hashlib.sha256(data).hexdigest()
        await catalog.add_file(self.src,sha,name,'https://private.invalid/source',len(data),1)
        if caption:path.with_suffix('.txt').write_bytes(b'curated caption')
        if native:
            self.service.submit('register_files_minimal',[dict(path=str(path),filename=name,
                size=len(data),created=1,hash_original=sha,origin_url='documented')]).result(5)
            self.service.submit('edit_curation',str(path),add_tags=['curated'],rating=5).result(5)
            key=self.service.read('get_file_id',str(path))
        else:key=None
        return path,sha,key

    async def run_merge(self, **kwargs):
        return await merge.merge_pair(self.pair,data_service=self.service,journal_root=self.journals,**kwargs)

    def state(self,key):return self.service.read('get_copy_version_state',key)

    async def test_curated_move_preserves_id_caption_curation_fts_and_receipts(self):
        path,sha,key=await self.file(caption=True)
        before=self.state(key)['state']
        result=await self.run_merge()
        self.assertTrue(result.ok,result.errors); self.assertEqual(result.moved,1)
        current=self.state(key)['state']; output=Path(current['file']['path'])
        self.assertFalse(path.exists()); self.assertFalse(path.with_suffix('.txt').exists())
        self.assertEqual(output.read_bytes(),b'actual private image')
        self.assertEqual(output.with_suffix('.txt').read_bytes(),b'curated caption')
        self.assertEqual(current['tags'],before['tags']); self.assertEqual(current['file']['rating'],5)
        self.assertEqual(current['file']['origin_url'],'documented')
        self.assertTrue(self.service.read('check_fts')['consistent'])
        self.assertIn(sha,await catalog.get_all_hashes(self.src))
        self.assertIn(sha,await catalog.get_all_hashes(self.dst))
        self.assertEqual(len(result.touched_folders),2)
        self.assertEqual(len(list(self.journals.glob('*/journal.json'))),1)

    async def test_stale_cherry_digest_rejects_before_bytes_or_native_move(self):
        path,sha,key=await self.file(); before=self.state(key)
        await catalog.add_file(self.src,'f'*64,path.name,None,path.stat().st_size,1)
        # The old hash still names the same path; whichever row runs first cannot
        # authorize a conflicting stored digest after the first verified transfer.
        with patch('third_party.cherry_dl.catalog.get_all_files',return_value=[dict(hash='f'*64,filename=path.name)]):
            result=await self.run_merge()
        self.assertFalse(result.ok); self.assertFalse(result.index_merged)
        self.assertTrue(path.exists()); self.assertEqual(self.state(key),before)

    async def test_changed_native_curation_blocks_published_commit_and_keeps_journal(self):
        path,sha,key=await self.file()
        from core import file_transfers
        publish=file_transfers.published_pin
        def concurrent(*args,**kwargs):
            pin=publish(*args,**kwargs)
            self.service.submit('edit_curation',str(path),rating=3).result(5)
            return pin
        with patch('core.file_transfers.published_pin',side_effect=concurrent):
            result=await self.run_merge()
        self.assertFalse(result.ok); self.assertFalse(result.index_merged)
        self.assertEqual(self.state(key)['state']['file']['rating'],3)
        self.assertEqual(len(result.touched_folders),2)
        journals=list(self.journals.glob('*/journal.json')); self.assertEqual(len(journals),1)
        import json
        self.assertEqual(json.loads(journals[0].read_bytes())['phase'],'review_required')

    async def test_catalog_ack_failure_reports_committed_native_move(self):
        path,sha,key=await self.file()
        with patch('third_party.cherry_dl.catalog.add_file',side_effect=OSError('private catalog failure')):
            result=await self.run_merge()
        self.assertFalse(result.ok); self.assertFalse(result.index_merged)
        self.assertFalse(path.exists()); self.assertTrue(Path(self.state(key)['state']['file']['path']).exists())
        self.assertEqual(self.state(key)['state']['file']['rating'],5)
        self.assertEqual(len(result.touched_folders),2)
        self.assertIn(sha,await catalog.get_all_hashes(self.src))

    async def test_orphan_delete_and_rename_rejected_before_any_effect(self):
        path,sha,key=await self.file()
        before=self.state(key)
        for action in (merge.ORPHAN_DELETE,merge.ORPHAN_RENAME):
            result=await self.run_merge(orphan_action=action)
            self.assertFalse(result.ok); self.assertFalse(result.index_merged)
            self.assertEqual(result.errors,['cherry_orphans_require_duplicate_review'])
        self.assertTrue(path.exists()); self.assertEqual(self.state(key),before)

    async def test_curated_duplicate_survives_even_if_destination_is_raw(self):
        path,sha,key=await self.file()
        target=self.dst/'raw.png'; target.write_bytes(path.read_bytes())
        await catalog.add_file(self.dst,sha,target.name,None,target.stat().st_size,1)
        before=self.state(key)
        result=await self.run_merge()
        self.assertTrue(result.ok,result.errors); self.assertEqual(result.orphaned,1)
        self.assertTrue(path.exists()); self.assertTrue(target.exists())
        self.assertEqual(self.state(key),before)
        self.assertTrue((self.src/'catalog.db').exists())

    async def test_indexed_text_moves_without_treating_it_as_own_caption(self):
        path,sha,key=await self.file(name='document.txt')
        result=await self.run_merge()
        self.assertTrue(result.ok,result.errors); self.assertFalse(path.exists())
        self.assertEqual(Path(self.state(key)['state']['file']['path']).read_bytes(),b'actual private image')

    async def test_unindexed_file_does_not_silently_create_native_identity(self):
        path,sha,key=await self.file(native=False)
        result=await self.run_merge()
        self.assertTrue(result.ok,result.errors)
        files=await catalog.get_all_files(self.dst)
        self.assertIsNone(self.service.read('get_file_id',str(self.dst/files[0]['filename'])))

    async def test_curated_identity_without_hash_receives_verified_sha_during_move(self):
        path,sha,_=await self.file(native=False)
        self.service.submit('register_files_minimal',[dict(path=str(path),filename=path.name,
            size=path.stat().st_size,created=1)]).result(5)
        self.service.submit('edit_curation',str(path),rating=5).result(5)
        key=self.service.read('get_file_id',str(path))
        result=await self.run_merge()
        self.assertTrue(result.ok,result.errors)
        self.assertEqual(self.state(key)['state']['file']['hash_original'],sha)
        self.assertEqual(self.state(key)['state']['file']['rating'],5)

    async def test_prepared_identity_move_updates_online_binding_and_preserves_id(self):
        path,sha,key=await self.file()
        self.service.submit('prepare_path_identity').result(5)
        self.service.backfill_path_identity()
        result=await self.run_merge()
        self.assertTrue(result.ok,result.errors)
        current=self.state(key)['state']['file']['path']
        resolution=self.service.resolve_path_identity(current)
        self.assertEqual(resolution['status'],'unique'); self.assertEqual(resolution['file_id'],key)
        self.assertEqual(self.service.resolve_path_identity(str(path))['status'],'unavailable')

    async def test_no_native_service_rejects_before_any_move_or_index_change(self):
        path,sha,key=await self.file(); before=self.state(key)
        result=await merge.merge_pair(self.pair)
        self.assertEqual(result.errors,['cherry_native_service_unavailable'])
        self.assertFalse(result.index_merged); self.assertTrue(path.exists())
        self.assertEqual(self.state(key),before)

    async def test_corrupt_relative_path_cannot_move_file_outside_source_profile(self):
        outside=self.root/'external.png'; outside.write_bytes(b'protected external bytes')
        sha=hashlib.sha256(outside.read_bytes()).hexdigest()
        with patch('third_party.cherry_dl.catalog.get_all_files',return_value=[dict(hash=sha,filename='../external.png')]):
            result=await self.run_merge()
        self.assertFalse(result.ok); self.assertFalse(result.index_merged)
        self.assertTrue(outside.exists()); self.assertIn('outside_profile',result.errors[0])

    async def test_corrupt_catalog_record_cannot_move_the_catalog_itself(self):
        original=(self.src/'catalog.db').read_bytes()
        with patch('third_party.cherry_dl.catalog.get_all_files',return_value=[dict(hash='e'*64,filename='catalog.db')]):
            result=await self.run_merge()
        self.assertFalse(result.ok); self.assertFalse(result.index_merged)
        self.assertEqual((self.src/'catalog.db').read_bytes(),original)
        self.assertIn('catalog_protected',result.errors[0])


    async def test_lost_native_ack_can_recover_same_id_without_replaying_curation(self):
        path,sha,key=await self.file()
        submit=self.service.submit
        def failed(operation,*args,**kwargs):
            if operation=='commit_transfer':raise OSError('private native failure')
            return submit(operation,*args,**kwargs)
        with patch.object(self.service,'submit',side_effect=failed):
            result=await self.run_merge()
        self.assertFalse(result.ok); self.assertFalse(result.index_merged)
        journal=next(self.journals.glob('*/journal.json'))
        # Recover is a synchronous worker operation, never run this on GUI.
        recovery=await asyncio.to_thread(recover,self.service,journal,journal_root=self.journals)
        self.assertEqual(recovery['status'],'source_restored')
        self.assertEqual(self.state(key)['state']['file']['path'],str(path).replace('\\','/'))
        self.assertTrue(path.exists())
        self.assertEqual(self.state(key)['state']['file']['rating'],5)

    async def test_failed_indexed_text_move_can_restore_original_without_caption(self):
        path,sha,key=await self.file(name='document.txt')
        submit=self.service.submit
        def failed(operation,*args,**kwargs):
            if operation=='commit_transfer':raise OSError('private native failure')
            return submit(operation,*args,**kwargs)
        with patch.object(self.service,'submit',side_effect=failed):result=await self.run_merge()
        self.assertFalse(result.ok)
        journal=next(self.journals.glob('*/journal.json'))
        recovery=await asyncio.to_thread(recover,self.service,journal,journal_root=self.journals)
        self.assertEqual(recovery['status'],'source_restored'); self.assertTrue(path.exists())
        self.assertEqual(self.state(key)['state']['file']['rating'],5)


if __name__=='__main__':unittest.main()
