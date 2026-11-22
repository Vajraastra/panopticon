"""Private real-Native/engine tests; no network, real catalogs or image reads."""
import asyncio
from contextlib import ExitStack, asynccontextmanager, closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT/'.work/pickler-repair-tests/counter-highwater'
HERE.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT))

from core import library_service as native
from core.library_repository import LibraryRepository
from core.path_identity import local_path
from core.path_identity_registry import prepare, store
from core.library_source_names import page,repository_method
from third_party.cherry_dl import catalog as production_catalog
def load_catalog():return production_catalog
from modules.cherry.logic.counter_highwater import (compile_minima,guarded_sync,namespace_counter,
                              prepare_artist_highwater as prepare_highwater,
                              prepare_highwater as prepare_profile_highwater)
from modules.cherry.logic.profile_scope import async_profile_scope
from modules.cherry.logic import profile_scope as scope_module


class FixtureRepository(LibraryRepository):
    get_source_names_page = repository_method

    def fixture_seed(self,rows):
        for value in rows:
            self.conn.execute('INSERT INTO files(path,filename,file_size,created_date,is_offline) VALUES(?,?,?,?,?)',
                              (local_path(value['path']),Path(value['path']).name,1,1,int(value.get('offline',False))))
        return True

    def fixture_registry(self,copy_id,canonical):
        prepare(self.conn)
        path,offline = self.conn.execute('SELECT path,is_offline FROM files WHERE id=?',(copy_id,)).fetchone()
        store(self.conn,[dict(file_id=copy_id,stored_path=path,is_offline=offline,status='resolved',
                              canonical=local_path(canonical),physical=[1,2,3,0,1,5,6],reason=None)])
        return True

    def fixture_rename(self):
        self.conn.execute("UPDATE files SET path=path||'.renamed' WHERE id=1")
        return True


class CounterHighwater(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp_root=HERE/'fixture-temp';temp_root.mkdir(exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=temp_root);self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.profile=self.root/'Профиль';self.profile.mkdir()
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(scope_module.ProjectPaths,'root',return_value=self.root))
        self.stack.enter_context(patch.object(native,'LibraryRepository',FixtureRepository))
        self.stack.enter_context(patch.object(native,'READS',native.READS|{'get_source_names_page'}))
        self.stack.enter_context(patch.object(native,'WRITES',native.WRITES|{'fixture_seed','fixture_registry','fixture_rename'}))
        self.service=native.LibraryService(self.root/'native.db');self.addCleanup(self.service.close)
        self.engine=load_catalog()
        self.artist='Художник'

    def seed(self,*rows):
        self.service.submit('fixture_seed',list(rows)).result(5)

    async def catalog(self,minimum=100):
        await self.engine.init_catalog(self.profile)
        await self.engine.ensure_layout_version(self.profile,('visual','archive','project','video','audio','document','other'),2)
        await self.engine.ensure_counter_at_least(self.profile,{'visual':minimum})

    def catalog_rows(self):
        with closing(sqlite3.connect(self.profile/'catalog.db')) as conn:
            return dict(conn.execute("SELECT key,value FROM meta WHERE key LIKE 'counter:%'"))

    def library_rows(self):
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            return list(conn.iterdump())

    async def test_active_offline_absent_9000_reserves_9001_without_native_write(self):
        self.seed(dict(path=str(self.profile/f'{self.artist}_00150.png')),
                  dict(path=str(self.profile/'picked'/f'{self.artist}_09000.png'),offline=True))
        self.assertFalse((self.profile/'picked').exists())
        await self.catalog();before=self.library_rows()
        async with async_profile_scope(self.profile) as ticket:
            review=await prepare_highwater(self.service,ticket,self.artist,self.engine)
            self.assertEqual(review['counters'],{'visual':9000})
            counter,name,folder,relative=await self.engine.reserve_destination(self.profile,self.artist,'remote.png')
        self.assertEqual(counter,9001);self.assertEqual(name,f'{self.artist}_09001.png')
        self.assertEqual(before,self.library_rows())
        self.assertEqual(list(self.profile.glob('*.png')),[])

    async def test_unrelated_curated_names_do_not_raise(self):
        self.seed(dict(path=str(self.profile/'picked'/'Manual_99999.png'),offline=True),
                  dict(path=str(self.profile/'picked'/f'{self.artist}_0000009000.png')),
                  dict(path=str(self.profile/'elsewhere-99999.png')),
                  dict(path=str(self.root/'outside'/f'{self.artist}_99999.png')))
        await self.catalog()
        async with async_profile_scope(self.profile) as ticket:
            review=await prepare_highwater(self.service,ticket,self.artist,self.engine)
        self.assertEqual(review['minima'],{});self.assertEqual(self.catalog_rows()['counter:visual'],100)

    async def test_namespaces_old_and_new_names_cyrillic_casefold(self):
        self.seed(dict(path=str(self.profile/'COMPRIMIDOS'/f'ARC-{self.artist.upper()}-09000.ZIP')),
                  dict(path=str(self.profile/'picked'/f'{self.artist}_08000.PSD'),offline=True),
                  dict(path=str(self.profile/f'{self.artist}_00150.PNG')),
                  dict(path=str(self.profile/'Videos'/f'vid-{self.artist}-00200.mp4')))
        await self.catalog()
        async with async_profile_scope(str(self.profile).upper()) as ticket:
            review=await prepare_highwater(self.service,ticket,self.artist,self.engine)
        self.assertEqual(review['minima'],dict(archive=9000,project=8000,visual=150,video=200))
        number,*_=await self.engine.reserve_destination(self.profile,self.artist,'remote.zip')
        self.assertEqual(number,9001)

    async def test_metadata_stamped_canonical_name_reserves_without_history_mutation(self):
        self.seed(dict(path=str(self.root/'short-stored.png')))
        self.service.submit('fixture_registry',1,str(self.profile/f'{self.artist}_09000.png')).result(5)
        await self.catalog();before=self.library_rows()
        async with async_profile_scope(self.profile) as ticket:
            review=await prepare_highwater(self.service,ticket,self.artist,self.engine)
        self.assertEqual(review['minima'],{'visual':9000})
        self.assertIsNotNone(review['snapshot']['clock']);self.assertEqual(before,self.library_rows())

    async def test_no_counter_decrease_or_noop_write(self):
        await self.catalog(9000)
        with closing(sqlite3.connect(self.profile/'catalog.db')) as conn:
            conn.execute('CREATE TRIGGER no_decrease BEFORE UPDATE ON meta BEGIN SELECT RAISE(ABORT,\'unexpected_update\'); END')
            conn.commit()
        result=await self.engine.ensure_counter_at_least(self.profile,{'visual':100})
        self.assertEqual(result,{'visual':9000})

    async def test_engine_batch_atomic_on_invalid_existing_counter(self):
        await self.catalog(100)
        with closing(sqlite3.connect(self.profile/'catalog.db')) as conn:
            conn.execute("UPDATE meta SET value='bad' WHERE key='counter:visual'");conn.commit()
        before=self.catalog_rows()
        with self.assertRaisesRegex(ValueError,'counter_state_invalid'):
            await self.engine.ensure_counter_at_least(self.profile,dict(archive=9000,visual=9000))
        self.assertEqual(before,self.catalog_rows())

    async def test_engine_concurrent_raises_and_allocations_do_not_lose_maximum(self):
        await self.catalog(100)
        await asyncio.gather(*(self.engine.ensure_counter_at_least(self.profile,{'visual':n}) for n in (9000,200,5000)))
        self.assertEqual(self.catalog_rows()['counter:visual'],9000)
        values=await asyncio.gather(*(self.engine.next_counter(self.profile,'visual') for _ in range(4)))
        self.assertEqual(sorted(values),[9001,9002,9003,9004])

    async def test_missing_root_and_none_service_fail_before_writes_or_sync(self):
        calls=[]
        async def sync():calls.append('sync')
        profile=dict(folder_path=str(self.root/'missing'),name=self.artist)
        for service in (None,self.service):
            with self.assertRaises((ValueError,FileNotFoundError)):
                await guarded_sync(profile,data_service=service,scope_factory=async_profile_scope,
                                   catalog_api=self.engine,run_sync=sync)
        self.assertEqual(calls,[]);self.assertFalse((self.root/'missing').exists())
        self.assertFalse((self.profile/'catalog.db').exists())

    async def test_missing_catalog_does_not_create_one(self):
        with self.assertRaisesRegex(ValueError,'counter_profile_missing'):
            await self.engine.ensure_counter_at_least(self.profile,{'visual':9000})
        self.assertFalse((self.profile/'catalog.db').exists())

    async def test_shared_scope_held_through_sync_and_released_on_exception(self):
        self.seed(dict(path=str(self.profile/f'{self.artist}_09000.png'),offline=True));await self.catalog()
        async def sync(hook):
            await hook(self.artist)
            self.assertEqual(self.catalog_rows()['counter:visual'],9000)
            with self.assertRaisesRegex(ValueError,'cherry_profile_busy'):
                async with async_profile_scope(str(self.profile).upper()):pass
            raise RuntimeError('fixture_sync_failure')
        with self.assertRaisesRegex(RuntimeError,'fixture_sync_failure'):
            await guarded_sync(dict(folder_path=str(self.profile),name=self.artist),data_service=self.service,
                               scope_factory=async_profile_scope,catalog_api=self.engine,run_sync=sync)
        async with async_profile_scope(self.profile) as ticket:ticket.assert_current()

    async def test_actual_engine_artists_share_namespace_without_profile_name_guess(self):
        self.seed(dict(path=str(self.profile/f'{self.artist}_09000.png'),offline=True),
                  dict(path=str(self.profile/'picked'/'Another_12000.png'),offline=True),
                  dict(path=str(self.profile/'picked'/'UnrelatedManual_99999.png'),offline=True))
        await self.catalog();calls=[]
        original=self.engine.ensure_counter_at_least
        async def apply(*args):calls.append(args[1]);return await original(*args)
        with patch.object(self.engine,'ensure_counter_at_least',side_effect=apply):
            async with async_profile_scope(self.profile) as ticket:
                hook=await prepare_profile_highwater(self.service,ticket,catalog_api=self.engine)
                self.assertEqual(self.catalog_rows()['counter:visual'],100)
                await asyncio.gather(hook(self.artist),hook(self.artist.upper()))
                self.assertEqual(self.catalog_rows()['counter:visual'],9000)
                await hook('Another')
                self.assertEqual(self.catalog_rows()['counter:visual'],12000)
                await hook('Uncatalogued')
                self.assertEqual(self.catalog_rows()['counter:visual'],12000)
        self.assertEqual(len(calls),2)

    async def test_hook_indexes_routed_names_and_ignores_unrelated_overflow(self):
        self.seed(dict(path=str(self.profile/'Comprimidos'/f'arc-{self.artist}-09000.zip'),offline=True),
                  dict(path=str(self.profile/('UnrelatedManual_'+'9'*100+'.png')),offline=True))
        await self.catalog()
        async with async_profile_scope(self.profile) as ticket:
            hook=await prepare_profile_highwater(self.service,ticket,catalog_api=self.engine)
            self.assertEqual(await hook(self.artist),{'archive':9000})
            with self.assertRaisesRegex(ValueError,'source_counter_out_of_range'):
                await hook('UnrelatedManual')
        self.assertEqual(self.catalog_rows()['counter:visual'],100)

    async def test_target_names_changed_during_page_prevents_counter_writes(self):
        self.seed(*(dict(path=str(self.profile/f'{self.artist}_{n:05d}.png')) for n in range(1,300)))
        await self.catalog();changed=False
        def mutate(*args,**kwargs):
            nonlocal changed
            result=page(*args,**kwargs)
            if not changed:
                changed=True;self.service.submit('fixture_rename').result(5)
            return result
        with patch('core.library_source_names.page',side_effect=mutate):
            async with async_profile_scope(self.profile) as ticket:
                with self.assertRaisesRegex(ValueError,'source_names_owner_changed'):
                    await prepare_highwater(self.service,ticket,self.artist,self.engine)
        self.assertEqual(self.catalog_rows()['counter:visual'],100)

    async def test_concurrent_receipts_and_unrelated_names_do_not_block_sync(self):
        outside = self.root/'outside'
        self.seed(*(dict(path=str(outside/f'{n}.png')) for n in range(300)),
                  dict(path=str(self.profile/f'{self.artist}_09000.png'), offline=True))
        await self.catalog()
        self.service.submit('add_watched_folder', self.root.as_posix()).result(5)
        self.service.submit('prepare_ingest_reviews').result(5)
        revisions = []

        def ingest_after_read(*args, **kwargs):
            result = page(*args, **kwargs)
            if not revisions:
                self.seed(dict(path=str(outside/f'{self.artist}_99999.png')))
            self.service.submit('register_catalog_files_observed', [], self.root.as_posix(),
                                operation_id=str(uuid4())).result(5)
            revisions.append(self.service.revision_snapshot())
            return result

        with patch('core.library_source_names.page', side_effect=ingest_after_read):
            async with async_profile_scope(self.profile) as ticket:
                hook = await prepare_profile_highwater(self.service, ticket, catalog_api=self.engine)
                self.assertEqual(await hook(self.artist), {'visual': 9000})
        self.assertEqual(len(set(revisions)), 4)
        self.assertEqual(hook.review['scanned'], 301)
        self.assertEqual(self.catalog_rows()['counter:visual'], 9000)

    async def test_cancellation_never_updates_counter(self):
        self.seed(dict(path=str(self.profile/f'{self.artist}_09000.png')));await self.catalog()
        event=threading.Event();event.set()
        async with async_profile_scope(self.profile) as ticket:
            with self.assertRaisesRegex(ValueError,'source_names_cancelled'):
                await prepare_highwater(self.service,ticket,self.artist,self.engine,cancel_event=event)
        self.assertEqual(self.catalog_rows()['counter:visual'],100)

    async def test_async_cancellation_stops_the_source_names_worker(self):
        entered, stopped = threading.Event(), threading.Event()

        def capture(*args, cancel_event):
            entered.set()
            if cancel_event.wait(3):
                stopped.set()
            raise ValueError('source_names_cancelled')

        with patch('modules.cherry.logic.counter_highwater.compile_minima', side_effect=capture):
            async with async_profile_scope(self.profile) as ticket:
                task = asyncio.create_task(prepare_profile_highwater(self.service, ticket, catalog_api=self.engine))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(await asyncio.to_thread(stopped.wait, 2))

    async def test_pk_pages_sparse_roots_bounded_and_snapshot_frozen(self):
        self.seed(*(dict(path=str(self.root/'outside'/f'{n}.png')) for n in range(600)),
                  dict(path=str(self.profile/f'{self.artist}_09000.png'),offline=True))
        roots=[self.profile.as_posix()];snapshot=None;after=0;ids=[];scanned=0;pages=0
        while True:
            result=self.service.read('get_source_names_page',roots,after_id=after,snapshot=snapshot)
            self.assertLessEqual(result['scanned'],256);pages+=1;scanned+=result['scanned']
            snapshot=result['snapshot'];after=result['after_id'];ids.extend(row['id'] for row in result['records'])
            if result['done']:break
        self.assertEqual((pages,scanned,ids),(3,601,[601]))
        self.seed(dict(path=str(self.profile/f'{self.artist}_09999.png')))
        # Max ID is a frontier, not an authority against edits of existing IDs.
        result=self.service.read('get_source_names_page',roots,after_id=after,snapshot=snapshot)
        self.assertEqual(result['records'],[]);self.assertEqual(result['snapshot']['max_id'],601)

    async def test_prepared_clock_rejects_changed_binding(self):
        self.seed(dict(path=str(self.root/'short-stored.png')))
        self.service.submit('fixture_registry',1,str(self.profile/f'{self.artist}_09000.png')).result(5)
        roots=[self.profile.as_posix()]
        first=self.service.read('get_source_names_page',roots)
        self.service.submit('fixture_rename').result(5)
        with self.assertRaisesRegex(ValueError,'source_names_snapshot_changed'):
            self.service.read('get_source_names_page',roots,snapshot=first['snapshot'])

    async def test_payload_validation_and_manual_extremes(self):
        self.assertIsNone(namespace_counter(self.artist,str(self.profile/('manual_'+'9'*100+'.png'))))
        for minima in ({},{'invalid':1},{'visual':True},{'visual':-1},{'visual':2**63-1}):
            with self.assertRaisesRegex(ValueError,'invalid_counter_minima'):
                await self.engine.ensure_counter_at_least(self.profile,minima)
        with self.assertRaisesRegex(ValueError,'source_counter_out_of_range'):
            namespace_counter(self.artist,str(self.profile/(self.artist+'_'+str(2**63)+'.png')))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='backslashreplace')
    unittest.main(verbosity=2)
