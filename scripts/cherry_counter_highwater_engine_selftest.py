"""Exercise the full existing download service privately, with no network."""
from contextlib import ExitStack
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from scripts import cherry_manual_copy_selftest as manual
from core import library_service as native
from third_party.cherry_dl import catalog as production_catalog
def load_catalog():return production_catalog
from modules.cherry.logic.counter_highwater import guarded_sync
from cherry_counter_highwater_selftest import FixtureRepository
from modules.cherry.logic.profile_scope import async_profile_scope
from modules.cherry.logic import profile_scope as scope_module


class EngineHighwater(manual.ManualCopyTests):
    async def asyncSetUp(self):
        stack=ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.object(native,'LibraryRepository',FixtureRepository))
        stack.enter_context(patch.object(native,'READS',native.READS|{'get_source_names_page'}))
        stack.enter_context(patch.object(native,'WRITES',native.WRITES|{'fixture_seed'}))
        await super().asyncSetUp()
        stack.enter_context(patch.object(scope_module.ProjectPaths,'root',return_value=self.root))
        self.service.submit('fixture_seed',[dict(path=str(self.folder/'picked'/'Artist_09000.png'),offline=True)]).result(5)
        self.counter_api=load_catalog();self.artists=[]

    async def sync(self):
        profile=await manual.index.get_profile(self.db,self.pid)
        self.assertNotEqual(profile.get('display_name'),'Artist produced by unrelated source')
        async def run(hook):
            async def before_artist(artist):
                self.assertEqual(self.downloads,[])
                self.artists.append(artist)
                return await hook(artist)
            return await manual.ds.run_profile_download(profile,workers=1,ext_filter=set(),config=self.config,
                force_full=True,emit=self.events.append,prepare_artist=before_artist)
        result,_=await guarded_sync(profile,data_service=self.service,scope_factory=async_profile_scope,
                                    catalog_api=self.counter_api,run_sync=run)
        return result

    async def test_real_service_allocates_9001_and_leaves_offline_native_intact(self):
        before=self.service.read('get_copy_version_state',2)
        result=await self.sync()
        self.assertEqual(result.downloaded,1);self.assertEqual(self.artists,['Artist'])
        self.assertEqual(self.downloads[0].name,'Artist_09001.png')
        self.assertEqual(before,self.service.read('get_copy_version_state',2))

    async def test_rejected_hook_aborts_before_any_download_or_worker(self):
        profile=await manual.index.get_profile(self.db,self.pid)
        hook=AsyncMock(side_effect=ValueError('fixture_highwater_rejected'))
        with self.assertRaisesRegex(ValueError,'fixture_highwater_rejected'):
            await manual.ds.run_profile_download(profile,workers=1,ext_filter=set(),config=self.config,
                force_full=True,emit=self.events.append,prepare_artist=hook)
        hook.assert_awaited_once_with('Artist')
        self.assertEqual(self.downloads,[])
        self.assertFalse(any(isinstance(event,manual.ds.WorkerStart) for event in self.events))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='backslashreplace')
    unittest.main(verbosity=2)
