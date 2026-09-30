"""Scope DB-wide: miembros íntegros en el dueño, recuperación fuera de fuentes."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.curation_apply import _identity
from core.duplicate_policy import DuplicatePolicy
from modules.duplicate_finder.logic.database_review import review_group
from modules.duplicate_finder.logic.retirement import DuplicateRetirementTasks
from modules.duplicate_finder.logic.exact_review import _probe
import duplicate_database_review_selftest as prior


class Retirement(prior.Review):
    def test_short_path_cannot_hide_cherry_ancestor_from_recovery_guard(self):
        import ctypes
        raw=self.file('LongCherryProfileDirectory/subdirectory/raw.png')
        curated=self.file('curated/keeper.png',rating=5,tags=['documented'])
        receipt=raw.parent.parent/'catalog.db'
        receipt.write_bytes(b'portable Cherry receipts')
        buffer=ctypes.create_unicode_buffer(32768)
        get_short=ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes=(ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint)
        get_short.restype=ctypes.c_uint
        length=get_short(str(raw),buffer,len(buffer))
        if not length or Path(buffer.value).as_posix().casefold()==raw.as_posix().casefold():
            self.skipTest('Windows filesystem does not expose an 8.3 alias')
        state=self.service.read('get_duplicate_snapshot',[raw.as_posix()])[0]
        self.service.submit('archive_copy',state['state']['file']['id'],expected_fingerprint=state['fingerprint']).result(3)
        short=Path(buffer.value)
        self.service.submit('register_files_minimal',[dict(path=short.as_posix(),filename=short.name,
            size=raw.stat().st_size,created=1,hash_original=prior.DIGEST)]).result(3)
        self.service.submit('prepare_duplicate_search').result(3)
        value=review_group(self.service,prior.DIGEST)
        self.assertTrue(value['complete'])
        value.update(folder=str(self.root),mode='hash',policy=asdict(DuplicatePolicy()),cancelled=False,error=None,token=1)
        result=self.retire(value,short,recovery=raw.parent.parent/'recovery')
        self.assertFalse(result['complete'])
        self.assertFalse(result['groups'][0]['archived'])
        self.assertEqual(result['groups'][0]['error'],'duplicate_recovery_inside_cherry_project')
        self.assertTrue(raw.exists() and curated.exists())
        self.assertEqual(receipt.read_bytes(),b'portable Cherry receipts')

    def packet(self, *, prepare=True):
        raw,curated=self.pair()
        if prepare:
            self.service.submit('prepare_duplicate_search').result(3)
        result=review_group(self.service,prior.DIGEST)
        # El contexto tiene una raíz que NO abarca la copia curada.
        result.update(folder=str(raw.parent),mode='hash',policy=asdict(DuplicatePolicy()),
                      cancelled=False,error=None,token=1)
        return raw,curated,result
    def retire(self,value,path,*,recovery=None):
        return DuplicateRetirementTasks(self.service).read('retire_duplicates',review=value,
            selected=[path.as_posix()],recovery_folder=str(recovery or (Path(self.temp.name)/'recovery').resolve()))

    def test_global_review_retires_raw_outside_single_folder_scope_and_preserves_curated(self):
        raw,curated,value=self.packet()
        before=self.service.read('get_duplicate_snapshot',[raw.as_posix(),curated.as_posix()])
        receipt=raw.parent/'catalog.db'
        receipt.write_bytes(b'Cherry history/URLs')
        result=self.retire(value,raw)
        self.assertTrue(result['complete'])
        self.assertFalse(raw.exists())
        self.assertTrue(curated.exists())
        self.assertEqual(receipt.read_bytes(),b'Cherry history/URLs')
        self.assertEqual(self.service.read('get_copy_version_state',before[1]['state']['file']['id'])['state'],before[1]['state'])
        after=self.service.read('get_copy_version_state',before[0]['state']['file']['id'])['state']
        expected=deepcopy(before[0]['state'])
        expected['file']['is_offline']=1
        self.assertEqual(after,expected)
        journal=json.loads(Path(result['groups'][0]['journal']).read_text(encoding='utf-8'))
        self.assertEqual(journal['review_scope'],'database')
        self.assertEqual(journal['phase'],'complete')

    def test_global_retirement_requires_explicit_index_preparation(self):
        raw,curated,value=self.packet(prepare=False)
        with self.assertRaisesRegex(ValueError,'search_not_prepared'):
            self.retire(value,raw)
        self.assertTrue(raw.exists() and curated.exists())
        self.assertEqual(self.service.read('count_files'),2)

    def test_new_member_between_snapshot_and_owner_cas_rejects_group(self):
        raw,curated,value=self.packet()
        submit=self.service.submit
        def changed(operation,*args,**kwargs):
            if operation=='archive_duplicate_copies':
                self.file('new-member.png',tags=['new-data'])
            return submit(operation,*args,**kwargs)
        # self.file usa submit; operaciones distintas no vuelven a modificar el grupo.
        with patch.object(self.service,'submit',changed):
            result=self.retire(value,raw)
        self.assertFalse(result['complete'])
        self.assertFalse(result['groups'][0]['archived'])
        self.assertIn('duplicate_candidate_group_changed',result['groups'][0]['error'])
        self.assertTrue(raw.exists() and curated.exists())
        self.assertEqual(self.service.read('count_files'),3)

    def test_native_member_check_cannot_be_bypassed_by_count_only_subset(self):
        raw,curated,value=self.packet()
        third=self.file('third.png')
        report=value['reports'][prior.DIGEST]
        approved=[dict(path=record['path'],copy_id=record['state']['file']['id'],fingerprint=record['fingerprint'])
                  for record in report['copies']]
        with self.assertRaisesRegex(Exception,'group_changed'):
            self.service.submit('archive_duplicate_copies',approved,[raw.as_posix()],digest=prior.DIGEST,
                policy=value['policy'],expected_scope=[],database_scope=True).result(3)
        self.assertTrue(raw.exists() and curated.exists() and third.exists())
        self.assertEqual(self.service.read('count_files'),3)

    def test_recovery_inside_any_source_folder_requires_review(self):
        raw,curated,value=self.packet()
        result=self.retire(value,raw,recovery=curated.parent/'retired')
        self.assertFalse(result['complete'])
        self.assertFalse(result['groups'][0]['archived'])
        self.assertEqual(result['groups'][0]['error'],'duplicate_recovery_inside_collection')
        self.assertTrue(raw.exists() and curated.exists())

    def test_recovery_inside_watched_folder_still_rejected_for_database_scope(self):
        raw,curated,value=self.packet()
        self.service.submit('add_watched_folder',Path(self.temp.name).resolve().as_posix()).result(3)
        with self.assertRaisesRegex(ValueError,'duplicate_recovery_inside_library'):
            self.retire(value,raw)
        self.assertTrue(raw.exists() and curated.exists())

    def test_keeper_other_volume_identity_is_allowed_without_cross_volume_move(self):
        raw,curated,value=self.packet()
        keeper_stat=curated.stat()
        before=value['proofs'][curated.as_posix()]['identity']
        simulated=(before[0]+1,*before[1:])
        value['proofs'][curated.as_posix()]['identity']=simulated
        def identity(observation):
            result=_identity(observation)
            return (result[0]+1,*result[1:]) if observation.st_ino==keeper_stat.st_ino else result
        def probe(path,event):
            proof,error=_probe(path,event)
            if path==curated.as_posix() and proof:
                proof['identity']=(proof['identity'][0]+1,*proof['identity'][1:])
            return proof,error
        with (patch('modules.duplicate_finder.logic.retirement._identity',identity),
              patch('modules.duplicate_finder.logic.retirement._probe',probe)):
            result=self.retire(value,raw)
        self.assertTrue(result['complete'])
        self.assertFalse(raw.exists())
        self.assertTrue(curated.exists())

    def test_unknown_review_scope_never_admits_any_archive(self):
        raw,curated,value=self.packet()
        value['scope']='everything'
        with patch.object(self.service,'submit',side_effect=AssertionError('admitted unknown scope')):
            with self.assertRaisesRegex(ValueError,'invalid_duplicate_archive_scope'):
                self.retire(value,raw)
        self.assertTrue(raw.exists() and curated.exists())


if __name__=='__main__':
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('Retirement.'+name,sys.modules[__name__])
        for name in Retirement.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
