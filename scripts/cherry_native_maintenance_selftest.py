"""Private Windows real service/filesystem/canonical-engine callback fixtures."""
import asyncio
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
HERE=ROOT/'.work'/'native-maintenance-selftest'
HERE.mkdir(parents=True,exist_ok=True)
from third_party.cherry_dl import archives,project_maintenance
def load_engine():return archives,project_maintenance
from modules.cherry.logic.native_maintenance import NativePhaseSession,RecoveryRequired,read_roster
from types import SimpleNamespace
from core.library_service import LibraryService
from core.maintenance_backend import Backend
def make_namespaces():return SimpleNamespace(Service=LibraryService)
from core.pinned_move import PendingMove
from modules.cherry.logic.profile_scope import profile_scope
from core.pinned_file import PinnedFile
from third_party.cherry_dl import catalog


def sha(data):return hashlib.sha256(data).hexdigest()


class Orchestration(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='private-orchestrator-',dir=HERE);self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name);self.root=self.base/'Artist';self.root.mkdir()
        self.database=self.base/'native.db';self.namespaces=make_namespaces()
        self.service=self.namespaces.Service(self.database);self.backend=Backend(self.service)
        self.addCleanup(self.close)
        self.backend.write('prepare_maintenance_operations')
        self.archives,self.maintenance=load_engine()
        asyncio.run(catalog.init_catalog(self.root))

    def close(self):
        if self.service is not None:self.service.close();self.service=None

    def session(self,backend=None):
        return NativePhaseSession(self.root,1,backend or self.backend,profile_guard=profile_scope(self.root))

    def add(self,name,data,*,original=None,curated=False,cataloged=True,catalog_size='actual'):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        digest=sha(data);original=original or digest
        self.backend.write('register_files_minimal',[dict(path=path.as_posix(),filename=path.name,
            size=len(data),created=path.stat().st_birthtime,hash_original=original)])
        key=self.backend.read('get_file_id',path.as_posix())
        if curated:self.backend.write('edit_curation',path.as_posix(),rating=5,add_tags=['keep'])
        if original!=digest:
            self.close()
            with closing(sqlite3.connect(self.database)) as conn:
                conn.execute('UPDATE files SET hash_mod=? WHERE id=?',(digest,key));conn.commit()
            self.service=self.namespaces.Service(self.database,require_ready=True);self.backend=Backend(self.service)
        if cataloged:asyncio.run(catalog.add_file(self.root,original,name,file_size=len(data) if catalog_size=='actual' else catalog_size,counter=1))
        return path,key

    def plan(self,files):
        return dict(folder=str(self.root),files=files,duplicates=[],errors=[],status='planned',
                    counters={'legacy':len(files)},audit_version=0)

    async def run_manual(self,session,plan):
        await session.before_apply(plan)
        # The canonical engine performs all staging before any publication.
        await self.maintenance.apply_plan(plan,move_file=session.move_file,native_review=True)
        await session.after_apply(plan)

    def test_cycle_101_preserves_ids_curation_and_202_receipts(self):
        files=[];before={}
        for index in range(101):
            path,key=self.add(f'Artist_{index+1:05d}.png',f'unique bytes {index}'.encode(),curated=index==0)
            before[key]=self.backend.read('get_copy_version_state',key)
            files.append(dict(old=path.name,new=f'Artist_{(index+1)%101+1:05d}.png',hash=sha(path.read_bytes()),
                              size=path.stat().st_size,counter=(index+1)%101+1))
        plan=self.plan(files)
        with self.session() as session:
            asyncio.run(self.run_manual(session,plan));opid=session.completed[0]
        for item,old in zip(files,before.values()):
            key=old['state']['file']['id'];current=self.backend.read('get_copy_version_state',key)
            self.assertEqual(current['state']['file']['path'],(self.root/item['new']).as_posix())
            self.assertEqual(current['state']['tags'],old['state']['tags'])
            self.assertEqual(current['state']['file']['rating'],old['state']['file']['rating'])
            for leg in ('stage','target'):
                self.assertEqual(self.backend.read('get_maintenance_receipt',opid,item['_native_step_id'],leg)['receipt']['copy_id'],key)
        self.assertEqual(self.backend.read('get_maintenance_operation',opid)['operation']['phase'],'complete')

    def test_metadata_modified_uses_actual_digest_and_catalog_original_key(self):
        original=sha(b'original9')
        source,key=self.add('Artist_00005.png',b'modified10',original=original,curated=True,catalog_size=9)
        before=self.backend.read('get_copy_version_state',key)
        with self.session() as session:
            plan=asyncio.run(self.maintenance.plan_project(self.root,'Artist',native_observation=session.plan_observation))
            self.assertFalse(plan['errors']);self.assertEqual(plan['hashed'],0)
            self.assertEqual(plan['files'][0]['catalog_hash'],original)
            self.assertEqual(plan['files'][0]['hash'],sha(source.read_bytes()))
            asyncio.run(self.run_manual(session,plan))
        rows=asyncio.run(catalog.get_all_files(self.root));self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['hash'],original);self.assertEqual(rows[0]['filename'],'Artist_00001.png')
        self.assertEqual(rows[0]['file_size'],9)
        current=self.backend.read('get_copy_version_state',key)
        self.assertEqual(current['state']['file']['hash_original'],original)
        self.assertEqual(current['state']['file']['hash_mod'],before['state']['file']['hash_mod'])
        self.assertEqual(current['state']['tags'],before['state']['tags'])
        self.assertEqual(current['state']['file']['file_size'],10)

    def test_preview_unchanged_reads_no_image_bytes_and_rejects_catalog_drift(self):
        source,key=self.add('Artist_00001.png',b'stable source')
        plan=self.plan([dict(old=source.name,new=source.name,hash=sha(source.read_bytes()),size=source.stat().st_size,counter=1)])
        with self.session() as session:
            with patch.object(PinnedFile,'verify',side_effect=AssertionError('preview read bytes')):
                self.assertEqual(len(session.preview(plan)),1)
            plan['files'][0]['hash']='f'*64
            with self.assertRaisesRegex(ValueError,'native_catalog_digest_disagrees'):session.preview(plan)
        self.assertEqual(self.backend.read('get_copy_version_state',key)['state']['file']['path'],source.as_posix())

    def test_duplicate_plan_refuses_before_filesystem_or_native_reservation(self):
        source,key=self.add('Artist_00001.png',b'stable source')
        plan=self.plan([]);plan['duplicates']=[dict(old=source.name)]
        with self.session() as session:
            with self.assertRaisesRegex(ValueError,'duplicates_or_errors'):asyncio.run(session.before_apply(plan))
            self.assertIsNone(session.phase)
        self.assertEqual(source.read_bytes(),b'stable source')

    def test_stage_ack_failure_keeps_handle_and_explicit_live_resume_settles_same_step(self):
        source,key=self.add('Artist_00005.png',b'failure protected bytes',curated=True)
        plan=self.plan([dict(old=source.name,new='Artist_00001.png',hash=sha(source.read_bytes()),size=source.stat().st_size,counter=1)])
        backend=self.backend
        class FailingBackend:
            owner_nonce=backend.owner_nonce
            service=backend.service
            failed=False
            def read(self,*args,**kwargs):return backend.read(*args,**kwargs)
            def write(self,name,*args,**kwargs):
                if name=='maintenance_ack_relocations' and not self.failed:
                    self.failed=True;raise RuntimeError('injected before SQL ack')
                return backend.write(name,*args,**kwargs)
        session=self.session(FailingBackend())
        with self.assertRaises(RecoveryRequired):
            with session:
                asyncio.run(session.before_apply(plan))
                item=plan['files'][0]
                asyncio.run(session.move_file(source,Path(item['native_staging']),item))
        opid=session.phase['operation_id'];staging=Path(plan['files'][0]['native_staging'])
        self.assertTrue(staging.exists());self.assertFalse(source.exists())
        with self.assertRaises(OSError):os.rename(staging,staging.with_suffix('.other'))
        self.assertEqual(session.finish_pending_moves(),dict(settled=1,replayed_plan=False))
        asyncio.run(session.move_file(staging,self.root/'Artist_00001.png',plan['files'][0]))
        asyncio.run(session.after_apply(plan));session.__exit__(None,None,None)
        self.assertEqual(self.backend.read('get_maintenance_operation',opid)['operation']['phase'],'complete')
        self.assertEqual(self.backend.read('get_copy_version_state',key)['state']['file']['rating'],5)

    def test_archive_keeps_package_and_does_not_replace_stamped_member(self):
        original_member=b'original archived member';original_hash=sha(original_member)
        stamped,key=self.add('Artist_00005.png',b'member stamped by Metadata Hub',original=original_hash,curated=True)
        payload=io.BytesIO()
        with zipfile.ZipFile(payload,'w') as archive:
            archive.writestr('original.png',original_member);archive.writestr('new.png',b'new extracted image')
        package,package_key=self.add('Comprimidos/Artist_00006.zip',payload.getvalue())
        rows=asyncio.run(catalog.get_all_files(self.root));known={row['hash']:row for row in rows}
        row=known[sha(payload.getvalue())];result=self.archives.ArchiveResult()
        with self.session() as session:
            asyncio.run(self.archives._process(self.root,'Artist',row,known,self.root/'.cherry-archives',result,lambda _:None,session))
        self.assertTrue(package.is_file());self.assertEqual(sha(package.read_bytes()),row['hash'])
        self.assertEqual(stamped.read_bytes(),b'member stamped by Metadata Hub')
        self.assertEqual(self.backend.read('get_copy_version_state',key)['state']['file']['rating'],5)
        self.assertEqual(result.added,1);self.assertEqual(result.processed,1)
        receipt=json.loads((self.root/'.cherry-archives'/(row['hash']+'.native.json')).read_text('utf-8'))
        self.assertEqual(receipt['state'],'completed_keep_package')
        old=next(entry for entry in receipt['entries'] if entry['hash']==original_hash)
        self.assertEqual(old['native_current']['current_digest'],sha(stamped.read_bytes()))
        new=next(entry for entry in receipt['entries'] if not entry['known'])
        new_id=self.backend.read('get_file_id',(self.root/new['target']).as_posix())
        self.assertNotIn(new_id,(key,package_key));self.assertEqual(self.backend.read('get_copy_version_state',new_id)['state']['tags'],[])
        # A retained package with a completed manifest validates current Native
        # member bindings; it never extracts the original over a stamped member.
        again=self.archives.ArchiveResult()
        with self.session() as session,patch.object(self.archives,'_extract',side_effect=AssertionError('reextracted completed package')):
            asyncio.run(self.archives._process(self.root,'Artist',row,known,self.root/'.cherry-archives',again,lambda _:None,session))
        self.assertEqual(again.added,0);self.assertTrue(package.exists())
        self.assertEqual(stamped.read_bytes(),b'member stamped by Metadata Hub')

    def test_native_false_callback_refuses_without_path_rename_fallback(self):
        source,key=self.add('Artist_00005.png',b'no fallback bytes')
        plan=self.plan([dict(old=source.name,new='Artist_00001.png',hash=sha(source.read_bytes()),size=source.stat().st_size,counter=1)])
        async def refuse(*args):return False
        with self.assertRaisesRegex(ValueError,'no hay fallback'):
            asyncio.run(self.maintenance.apply_plan(plan,move_file=refuse,native_review=True))
        self.assertTrue(source.exists());self.assertFalse((self.root/'Artist_00001.png').exists())

    def test_released_failed_stage_reopens_fresh_report_without_replaying(self):
        source,key=self.add('Artist_00005.png',b'process recovery protected bytes')
        plan=self.plan([dict(old=source.name,new='Artist_00001.png',hash=sha(source.read_bytes()),size=source.stat().st_size,counter=1)])
        backend=self.backend
        class FailingBackend:
            owner_nonce=backend.owner_nonce
            service=backend.service
            def read(self,*args,**kwargs):return backend.read(*args,**kwargs)
            def write(self,name,*args,**kwargs):
                if name=='maintenance_ack_relocations':raise RuntimeError('simulated process boundary before ack')
                return backend.write(name,*args,**kwargs)
        session=self.session(FailingBackend())
        with self.assertRaises(RecoveryRequired):
            with session:
                asyncio.run(session.before_apply(plan));item=plan['files'][0]
                asyncio.run(session.move_file(source,Path(item['native_staging']),item))
        opid=session.phase['operation_id'];staging=Path(item['native_staging'])
        session.release_for_recovery();self.close()
        self.service=self.namespaces.Service(self.database,require_ready=True);self.backend=Backend(self.service)
        with self.session() as fresh:
            report=fresh.inspect_recovery(opid)
            self.assertFalse(report['replayed']);self.assertTrue(report['requires_review'])
            self.assertEqual(report['placements'][0]['phase'],'stage_intent')
            self.assertEqual([row['path'] for row in report['placements'][0]['locations']],[staging.as_posix()])
        self.assertTrue(staging.exists());self.assertFalse((self.root/item['new']).exists())
        self.assertEqual(self.backend.read('get_maintenance_operation',opid)['operation']['phase'],'review')

    def test_duplicate_package_members_never_publish_or_delete_package(self):
        payload=io.BytesIO()
        with zipfile.ZipFile(payload,'w') as archive:
            archive.writestr('one.png',b'same member');archive.writestr('two.png',b'same member')
        package,key=self.add('Comprimidos/Artist_00006.zip',payload.getvalue())
        rows=asyncio.run(catalog.get_all_files(self.root));known={row['hash']:row for row in rows};row=rows[0]
        with self.session() as session,patch.object(session,'publish_member',side_effect=AssertionError('duplicate published')):
            with self.assertRaisesRegex(ValueError,'repetidos'):
                asyncio.run(self.archives._process(self.root,'Artist',row,known,self.root/'.cherry-archives',self.archives.ArchiveResult(),lambda _:None,session))
        self.assertTrue(package.exists());self.assertEqual(package.read_bytes(),payload.getvalue())
        self.assertEqual(self.backend.read('get_copy_version_state',key)['state']['file']['path'],package.as_posix())

    def test_organizer_callbacks_move_original_with_same_native_id(self):
        source,key=self.add('Artist_00005.mp4',b'toy video bytes',curated=True)
        with self.session() as session:result=asyncio.run(self.archives.organize_project(self.root,native_hooks=session))
        self.assertEqual(result.organized,1);self.assertFalse(source.exists())
        current=self.backend.read('get_copy_version_state',key)
        self.assertEqual(current['state']['file']['path'],(self.root/'Videos'/source.name).as_posix())
        self.assertEqual(current['state']['file']['rating'],5)

    def test_roster_reader_buffers_raw_pinned_stream_and_detaches(self):
        source=self.base/'roster.jsonl';source.write_bytes(b'{"x":1}\n'*12000)
        with PinnedFile(source) as pin:
            self.assertEqual(len(read_roster(pin)),12000)
            self.assertFalse(pin.stream.closed);pin.stream.seek(0);self.assertEqual(pin.stream.read(1),b'{')

    def test_scope_names_are_scanned_once_and_states_use_primary_keys(self):
        sources=[self.add(f'Artist_{index+1:05d}.png',f'bytes {index}'.encode())[0] for index in range(17)]
        rows=asyncio.run(catalog.get_all_files(self.root));by_path={row['filename']:row for row in rows}
        original=self.backend.read;calls=[]
        def observe(name,*args,**kwargs):
            calls.append(name)
            if name=='get_maintenance_copies':raise AssertionError('global UDF scan per file')
            return original(name,*args,**kwargs)
        with self.session() as session,patch.object(self.backend,'read',side_effect=observe):
            for source in sources:asyncio.run(session.plan_observation(source,[by_path[source.name]]))
            self.assertEqual(calls.count('get_source_names_page'),1)
            self.assertEqual(calls.count('get_maintenance_copy_states'),17)

    def test_canonical_process_connects_organize_extract_and_compact_phases(self):
        video,key=self.add('Artist_00005.mp4',b'video current bytes',curated=True)
        payload=io.BytesIO()
        with zipfile.ZipFile(payload,'w') as archive:archive.writestr('new.png',b'brand new image bytes')
        package,package_id=self.add('Artist_00006.zip',payload.getvalue())
        with self.session() as session:
            result=asyncio.run(self.archives.process_project(self.root,'Artist',native_hooks=session))
            self.assertEqual(result.errors,[])
            self.assertEqual(len(session.completed),3)
        self.assertEqual(result.processed,1);self.assertEqual(result.added,1)
        current=self.backend.read('get_copy_version_state',key)
        self.assertEqual(current['state']['file']['rating'],5)
        self.assertEqual(current['state']['tags'][0]['name'],'keep')
        package_current=self.backend.read('get_copy_version_state',package_id)['state']['file']['path']
        self.assertTrue(Path(package_current).exists());self.assertEqual(sha(Path(package_current).read_bytes()),sha(payload.getvalue()))


    def interrupted_stage(self,*,original=None,data=b'current bytes for restart recovery',catalog_size='actual'):
        source,key=self.add('Artist_00005.png',data,original=original,curated=True,catalog_size=catalog_size)
        plan=self.plan([dict(old=source.name,new='Artist_00001.png',hash=original or sha(source.read_bytes()),size=source.stat().st_size,
                            catalog_size=source.stat().st_size if catalog_size=='actual' else catalog_size,counter=1)])
        backend=self.backend
        class FailingBackend:
            owner_nonce=backend.owner_nonce
            service=backend.service
            def read(self,*args,**kwargs):return backend.read(*args,**kwargs)
            def write(self,name,*args,**kwargs):
                if name=='maintenance_ack_relocations':raise RuntimeError('simulated crash after physical stage')
                return backend.write(name,*args,**kwargs)
        session=self.session(FailingBackend())
        with self.assertRaises(RecoveryRequired):
            with session:
                asyncio.run(session.before_apply(plan));item=plan['files'][0]
                asyncio.run(session.move_file(source,Path(item['native_staging']),item))
        opid=session.phase['operation_id'];session.release_for_recovery();self.close()
        self.service=self.namespaces.Service(self.database,require_ready=True);self.backend=Backend(self.service)
        return source,key,opid,plan

    def test_restart_review_token_resumes_stage_target_catalog_and_identity(self):
        source,key,opid,plan=self.interrupted_stage()
        with self.session() as fresh:
            report=fresh.review_recovery(opid)
            self.assertEqual(report['counts']['packages_deleted'],0)
            result=asyncio.run(fresh.resume_recovery(opid,report['review_token']))
            self.assertFalse(result['replayed_engine_plan'])
        current=self.backend.read('get_copy_version_state',key)
        self.assertEqual(current['state']['file']['path'],(self.root/'Artist_00001.png').as_posix())
        self.assertEqual(current['state']['file']['rating'],5)
        self.assertEqual(current['state']['tags'][0]['name'],'keep')
        self.assertEqual(self.backend.read('get_maintenance_operation',opid)['operation']['phase'],'complete')
        self.assertEqual(asyncio.run(catalog.get_all_files(self.root))[0]['filename'],'Artist_00001.png')
        saved=json.loads((self.root/'.cherry-maintenance'/'plan.json').read_text('utf-8'))
        self.assertEqual(saved['status'],'completed');self.assertEqual(saved['native_operation_id'],opid)

    def test_restart_recovery_preserves_original_catalog_and_modified_native_hash(self):
        original=sha(b'original before stamping')
        source,key,opid,plan=self.interrupted_stage(original=original)
        with self.session() as fresh:
            report=fresh.review_recovery(opid)
            asyncio.run(fresh.resume_recovery(opid,report['review_token']))
        current=self.backend.read('get_copy_version_state',key)['state']['file']
        self.assertEqual(current['hash_original'],original)
        self.assertEqual(current['hash_mod'],sha(b'current bytes for restart recovery'))
        rows=asyncio.run(catalog.get_all_files(self.root));self.assertEqual(rows[0]['hash'],original)

    def test_recovery_changed_file_rejects_stale_review_before_mutation(self):
        source,key,opid,plan=self.interrupted_stage()
        with self.session() as fresh:
            report=fresh.review_recovery(opid)
            stage=Path(plan['files'][0]['native_staging']);info=stage.stat()
            os.utime(stage,ns=(info.st_atime_ns,info.st_mtime_ns+1000000))
            with self.assertRaisesRegex(ValueError,'mtime_changed|review_changed'):
                asyncio.run(fresh.resume_recovery(opid,report['review_token']))
        self.assertTrue(stage.exists());self.assertFalse((self.root/'Artist_00001.png').exists())
        self.assertEqual(self.backend.read('get_maintenance_operation',opid)['operation']['phase'],'review')

    def test_restart_archive_admission_recovers_manifest_without_deleting_package(self):
        original_member=b'original member in package';original_hash=sha(original_member)
        stamped,old_id=self.add('Artist_00005.png',b'legitimate Metadata Hub stamp',original=original_hash,curated=True)
        payload=io.BytesIO()
        with zipfile.ZipFile(payload,'w') as archive:
            archive.writestr('old.png',original_member);archive.writestr('new.png',b'new bytes for private admission')
        package,package_id=self.add('Comprimidos/Artist_00006.zip',payload.getvalue())
        rows=asyncio.run(catalog.get_all_files(self.root));known={row['hash']:row for row in rows};row=known[sha(payload.getvalue())]
        backend=self.backend
        class FailingBackend:
            owner_nonce=backend.owner_nonce
            service=backend.service
            def read(self,*args,**kwargs):return backend.read(*args,**kwargs)
            def write(self,name,*args,**kwargs):
                if name=='maintenance_admit_descendants':raise RuntimeError('simulated crash before member admission')
                return backend.write(name,*args,**kwargs)
        session=self.session(FailingBackend())
        with self.assertRaises(RecoveryRequired):
            with session:
                asyncio.run(self.archives._process(self.root,'Artist',row,known,self.root/'.cherry-archives',self.archives.ArchiveResult(),lambda _:None,session))
        opid=session.phase['operation_id'];session.release_for_recovery();self.close()
        self.service=self.namespaces.Service(self.database,require_ready=True);self.backend=Backend(self.service)
        with self.session() as fresh:
            report=fresh.review_recovery(opid)
            asyncio.run(fresh.resume_recovery(opid,report['review_token']))
        self.assertTrue(package.exists());self.assertEqual(sha(package.read_bytes()),row['hash'])
        self.assertEqual(stamped.read_bytes(),b'legitimate Metadata Hub stamp')
        self.assertEqual(self.backend.read('get_copy_version_state',old_id)['state']['file']['rating'],5)
        receipt=json.loads((self.root/'.cherry-archives'/(row['hash']+'.native.json')).read_text('utf-8'))
        self.assertEqual(receipt['state'],'completed_keep_package')
        member=next(value for value in receipt['entries'] if not value['known'])
        new_id=self.backend.read('get_file_id',(self.root/member['target']).as_posix())
        self.assertNotIn(new_id,(old_id,package_id));self.assertEqual(self.backend.read('get_copy_version_state',new_id)['state']['tags'],[])

    def completed_one_member(self):
        payload=io.BytesIO();member=b'archived member for retirement history'
        with zipfile.ZipFile(payload,'w') as archive:archive.writestr('member.png',member)
        package,package_id=self.add('Comprimidos/Artist_00006.zip',payload.getvalue())
        rows=asyncio.run(catalog.get_all_files(self.root));known={row['hash']:row for row in rows};row=rows[0]
        with self.session() as session:
            asyncio.run(self.archives._process(self.root,'Artist',row,known,self.root/'.cherry-archives',self.archives.ArchiveResult(),lambda _:None,session))
        saved=json.loads((self.root/'.cherry-archives'/(row['hash']+'.native.json')).read_text('utf-8'))
        member_path=self.root/saved['entries'][0]['target'];key=self.backend.read('get_file_id',member_path.as_posix())
        self.backend.write('edit_curation',member_path.as_posix(),rating=5,add_tags=['keep'])
        return package,package_id,row,member_path,key

    def test_completed_package_after_pickler_move_keeps_curated_member_without_restore(self):
        from core.file_transfers import transfer
        package,package_id,row,member,key=self.completed_one_member()
        picked=self.root/'picked';picked.mkdir();journals=self.base/'pickler';journals.mkdir()
        transfer(self.service,member,picked,'move',journal_root=journals,register_missing=False)
        moved=self.backend.read('get_copy_version_state',key)['state']['file']['path']
        with self.session() as session,patch.object(self.archives,'_extract',side_effect=AssertionError('retired member reextracted')):
            result=asyncio.run(self.archives.process_project(self.root,'Artist',native_hooks=session))
        self.assertEqual(result.errors,[]);self.assertFalse(member.exists());self.assertTrue(Path(moved).exists())
        self.assertEqual(self.backend.read('get_copy_version_state',key)['state']['file']['rating'],5)
        saved=json.loads((self.root/'.cherry-archives'/(row['hash']+'.native.json')).read_text('utf-8'))
        self.assertEqual(saved['entries'][0]['native_disposition']['reason'],'native_moved')
        self.assertTrue(Path(self.backend.read('get_copy_version_state',package_id)['state']['file']['path']).exists())

    def test_completed_package_after_native_offline_member_never_resurrects_it(self):
        package,package_id,row,member,key=self.completed_one_member()
        before=self.backend.read('get_copy_version_state',key)
        self.backend.write('archive_copy',key,expected_fingerprint=before['fingerprint'])
        with PinnedFile(member,delete=True) as pin:
            pin.verify(before['state']['file']['hash_original'],pin.check());pin.mark_delete()
        with self.session() as session,patch.object(self.archives,'_extract',side_effect=AssertionError('offline member reextracted')):
            result=asyncio.run(self.archives.process_project(self.root,'Artist',native_hooks=session))
        self.assertEqual(result.errors,[]);self.assertFalse(member.exists())
        current=self.backend.read('get_copy_version_state',key)['state']['file']
        self.assertEqual(current['is_offline'],1);self.assertEqual(current['rating'],5)
        saved=json.loads((self.root/'.cherry-archives'/(row['hash']+'.native.json')).read_text('utf-8'))
        self.assertEqual(saved['entries'][0]['native_disposition']['reason'],'native_offline')
        self.assertTrue(Path(self.backend.read('get_copy_version_state',package_id)['state']['file']['path']).exists())

    def test_completed_operation_after_later_move_never_rewrites_catalog(self):
        from unittest.mock import AsyncMock
        source,key=self.add('Artist_00005.png',b'same physical identity moved later',curated=True)
        first=self.plan([dict(old=source.name,new='Artist_00001.png',hash=sha(source.read_bytes()),size=source.stat().st_size,counter=1)])
        with self.session() as session:
            asyncio.run(self.run_manual(session,first));oldop=session.completed[0]
        target=self.root/'Artist_00001.png'
        second=self.plan([dict(old=target.name,new=source.name,hash=sha(target.read_bytes()),size=target.stat().st_size,counter=5)])
        with self.session() as session:asyncio.run(self.run_manual(session,second))
        before=asyncio.run(catalog.get_all_files(self.root))
        with self.session() as session,patch.object(catalog,'apply_project_maintenance',new=AsyncMock()) as writer:
            with self.assertRaisesRegex(ValueError,'location_changed'):
                asyncio.run(session.resume_recovery(oldop,'0'*64))
            writer.assert_not_awaited()
        self.assertEqual(asyncio.run(catalog.get_all_files(self.root)),before)
        self.assertEqual(self.backend.read('get_copy_version_state',key)['state']['file']['path'],source.as_posix())

    def test_preflight_pending_error_exposes_existing_operation_before_engine(self):
        from modules.cherry.logic.native_maintenance import process_native_project,NativeMaintenanceFailure
        source,key,opid,plan=self.interrupted_stage()
        with self.assertRaises(NativeMaintenanceFailure) as caught:
            asyncio.run(process_native_project(self.root,'Artist',data_service=self.service))
        self.assertEqual(caught.exception.operation_id,opid)
        self.assertEqual(self.backend.read('get_maintenance_operation',opid)['operation']['phase'],'review')

    def test_native_offline_number_is_reserved_during_compaction(self):
        ghost,ghost_id=self.add('picked/Artist_00001.png',b'historical selection',cataloged=False,curated=True)
        before=self.backend.read('get_copy_version_state',ghost_id)
        self.backend.write('archive_copy',ghost_id,expected_fingerprint=before['fingerprint'])
        with PinnedFile(ghost,delete=True) as pin:pin.verify(sha(b'historical selection'),pin.check());pin.mark_delete()
        history=self.backend.read('get_copy_version_state',ghost_id)
        source,key=self.add('Artist_00005.png',b'active compact source')
        with self.session() as session:
            plan=asyncio.run(self.maintenance.plan_project(self.root,'Artist',native_observation=session.plan_observation,
                                                          native_reserved=session.reserved_numbers))
            self.assertEqual(plan['files'][0]['new'],'Artist_00002.png')
            self.assertIn(1,plan['reserved_counters']['legacy'])
            asyncio.run(self.run_manual(session,plan))
        self.assertEqual(self.backend.read('get_copy_version_state',ghost_id),history)
        self.assertEqual(self.backend.read('get_copy_version_state',key)['state']['file']['path'],(self.root/'Artist_00002.png').as_posix())

    def test_archive_counter_skips_native_ghost_before_allocating_members(self):
        ghost,ghost_id=self.add('discarded/Artist_00099.png',b'old retired number',cataloged=False)
        self.backend.write('archive_copy',ghost_id,expected_fingerprint=self.backend.read('get_copy_version_state',ghost_id)['fingerprint'])
        with PinnedFile(ghost,delete=True) as pin:pin.verify(sha(b'old retired number'),pin.check());pin.mark_delete()
        payload=io.BytesIO()
        with zipfile.ZipFile(payload,'w') as archive:archive.writestr('new.png',b'fresh member')
        package,package_id=self.add('Comprimidos/manual.zip',payload.getvalue())
        rows=asyncio.run(catalog.get_all_files(self.root));known={row['hash']:row for row in rows};row=rows[0]
        with self.session() as session:
            asyncio.run(self.archives._process(self.root,'Artist',row,known,self.root/'.cherry-archives',self.archives.ArchiveResult(),lambda _:None,session))
        saved=json.loads((self.root/'.cherry-archives'/(row['hash']+'.native.json')).read_text('utf-8'))
        self.assertEqual(saved['entries'][0]['target'],'Artist_00100.png');self.assertTrue(package.exists())
        self.assertEqual(self.backend.read('get_copy_version_state',ghost_id)['state']['file']['is_offline'],1)

    def test_restart_preserves_original_catalog_size_while_physical_size_is_modified(self):
        original=sha(b'original9')
        source,key,opid,plan=self.interrupted_stage(original=original,data=b'modified10',catalog_size=9)
        with self.session() as fresh:
            report=fresh.review_recovery(opid);asyncio.run(fresh.resume_recovery(opid,report['review_token']))
        row=asyncio.run(catalog.get_all_files(self.root))[0]
        self.assertEqual((row['hash'],row['file_size']),(original,9))
        native=self.backend.read('get_copy_version_state',key)['state']['file']
        self.assertEqual((native['hash_mod'],native['file_size']),(sha(b'modified10'),10))

    def test_unknown_original_catalog_size_stays_unknown_after_modified_move(self):
        original=sha(b'original9')
        source,key=self.add('Artist_00005.png',b'modified10',original=original,catalog_size=None)
        with self.session() as session:
            plan=asyncio.run(self.maintenance.plan_project(self.root,'Artist',native_observation=session.plan_observation))
            asyncio.run(self.run_manual(session,plan))
        self.assertIsNone(asyncio.run(catalog.get_all_files(self.root))[0]['file_size'])
        self.assertEqual(self.backend.read('get_copy_version_state',key)['state']['file']['file_size'],10)

    def test_post_native_complete_manifest_failure_keeps_exact_operation_for_recovery(self):
        from modules.cherry.logic.native_maintenance import process_native_project,NativeMaintenanceFailure
        payload=io.BytesIO()
        with zipfile.ZipFile(payload,'w') as archive:archive.writestr('new.png',b'new member bytes')
        package,package_id=self.add('Comprimidos/manual.zip',payload.getvalue())
        write=self.archives._write
        def fail_completed(path,value):
            if value.get('state')=='completed_keep_package':raise OSError('injected manifest publication failure')
            return write(path,value)
        with patch.object(self.archives,'_write',side_effect=fail_completed):
            with self.assertRaises(NativeMaintenanceFailure) as caught:
                asyncio.run(process_native_project(self.root,'Artist',data_service=self.service))
        opid=caught.exception.operation_id;self.assertIsNotNone(opid)
        self.assertEqual(self.backend.read('get_maintenance_operation',opid)['operation']['phase'],'complete')
        with self.session() as session:
            review=session.review_recovery(opid);asyncio.run(session.resume_recovery(opid,review['review_token']))
        receipt=json.loads((self.root/'.cherry-archives'/(sha(payload.getvalue())+'.native.json')).read_text('utf-8'))
        self.assertEqual(receipt['state'],'completed_keep_package');self.assertTrue(package.exists())

    def test_corrupt_package_before_native_phase_never_offers_prior_organizer_recovery(self):
        from modules.cherry.logic.native_maintenance import process_native_project,NativeMaintenanceFailure
        package,key=self.add('manual.zip',b'not a ZIP package')
        with self.assertRaises(NativeMaintenanceFailure) as caught:
            asyncio.run(process_native_project(self.root,'Artist',data_service=self.service))
        self.assertIsNone(caught.exception.operation_id)
        self.assertTrue((self.root/'Comprimidos/manual.zip').exists())

    def test_legacy_windows_alias_requires_review_without_admitting_another_id(self):
        import ctypes
        from core.pinned_move import api
        source,key=self.add('Artist_00005.png',b'legacy named copy',curated=True)
        short=api().GetShortPathNameW;short.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint32];short.restype=ctypes.c_uint32
        buffer=ctypes.create_unicode_buffer(32768);self.assertTrue(short(str(source),buffer,len(buffer)))
        alias=Path(buffer.value).as_posix()
        if alias==source.as_posix():alias=source.as_posix().upper()
        self.close()
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute('UPDATE files SET path=? WHERE id=?',(alias,key));conn.commit()
        self.service=self.namespaces.Service(self.database,require_ready=True);self.backend=Backend(self.service)
        before=self.backend.read('get_copy_version_state',key)
        with self.session() as session:
            with self.assertRaisesRegex(ValueError,'source_alias_requires_review'):
                asyncio.run(self.maintenance.plan_project(self.root,'Artist',native_observation=session.plan_observation))
        self.assertEqual(self.backend.read('get_copy_version_state',key),before)
        self.assertIsNone(self.backend.read('get_file_id',source.as_posix()))



if __name__=='__main__':unittest.main(verbosity=2)
