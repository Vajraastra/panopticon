"""Real Windows handles and private Native fixtures for Pickler."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.library_service import LibraryService
from core.library_repository import LibraryRepository
from core.pinned_file import PinnedFile
from modules.pickler.logic import transfers


class PicklerTransfer(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name); self.service=LibraryService(self.root/'private.db'); self.addCleanup(self.service.close)
        self.folder=self.root/'input'; self.folder.mkdir()
        self.source=self.folder/'image Ñ.png'; self.source.write_bytes(b'private image bytes')
        self.caption=self.source.with_suffix('.txt'); self.caption.write_bytes(b'private caption')
        self.output=self.root/'output'; self.journals=self.root/'journals'
        self.service.submit('register_files_minimal',[dict(path=str(self.source),filename=self.source.name,
            size=self.source.stat().st_size,created=1,hash_original=hashlib.sha256(self.source.read_bytes()).hexdigest(),origin_url='receipt')]).result(5)
        self.key=self.service.read('get_file_id',str(self.source))
        self.service.submit('edit_curation',str(self.source),add_tags=['curated'],rating=5).result(5)

    def state(self,key=None):return self.service.read('get_copy_version_state',self.key if key is None else key)
    def transfer(self,action='move',**kwargs):
        return transfers.transfer(self.service,self.source,self.output,action,journal_root=self.journals,**kwargs)
    def undo(self,previous):return transfers.undo(self.service,previous,journal_root=self.journals)

    def test_move_pair_preserves_identity_curation_and_bytes(self):
        before=self.state()['state']; result=self.transfer()
        self.assertFalse(self.source.exists()); self.assertFalse(self.caption.exists())
        self.assertEqual(Path(result['current']).read_bytes(),b'private image bytes')
        self.assertEqual(Path(result['current']).with_suffix('.txt').read_bytes(),b'private caption')
        self.assertEqual(result['receipt']['result_copy_id'],self.key)
        self.assertEqual(self.state()['state']['tags'],before['tags'])
        self.assertEqual(self.state()['state']['file']['rating'],5)
        self.assertTrue(self.service.read('check_fts')['consistent'])
        self.assertEqual(json.loads(Path(result['journal']).read_bytes())['phase'],'complete')

    def test_copy_pair_initial_curation_is_independent(self):
        before=self.state(); result=self.transfer('copy'); key=result['receipt']['result_copy_id']
        self.assertTrue(self.source.exists()); self.assertTrue(self.caption.exists()); self.assertNotEqual(key,self.key)
        self.assertEqual(self.state(key)['state']['tags'],before['state']['tags'])
        self.service.submit('edit_curation',result['current'],rating=1,remove_tags=['curated']).result(5)
        self.assertEqual(before,self.state())

    def test_undo_move_restores_pair_same_id(self):
        result=self.transfer(); self.undo(result)
        self.assertEqual(self.source.read_bytes(),b'private image bytes'); self.assertEqual(self.caption.read_bytes(),b'private caption')
        self.assertFalse(Path(result['current']).exists()); self.assertEqual(self.state()['state']['file']['path'],self.source.resolve().as_posix())

    def test_undo_move_never_overwrites_new_original(self):
        result=self.transfer(); self.source.write_bytes(b'new unrelated file')
        with self.assertRaisesRegex(ValueError,'occupied'):self.undo(result)
        self.assertEqual(self.source.read_bytes(),b'new unrelated file'); self.assertTrue(Path(result['current']).exists())

    def test_undo_copy_removes_verified_pair_and_keeps_offline_curation(self):
        result=self.transfer('copy'); key=result['receipt']['result_copy_id']; self.undo(result)
        self.assertFalse(Path(result['current']).exists()); self.assertFalse(Path(result['current']).with_suffix('.txt').exists())
        self.assertEqual(self.state(key)['state']['file']['is_offline'],1)
        self.assertEqual(self.state(key)['state']['file']['rating'],5); self.assertTrue(self.source.exists())

    def test_undo_copy_preserves_changed_image(self):
        result=self.transfer('copy'); output=Path(result['current']); output.write_bytes(b'edited copy')
        with self.assertRaisesRegex(ValueError,'changed'):self.undo(result)
        self.assertEqual(output.read_bytes(),b'edited copy'); self.assertEqual(self.state(result['receipt']['result_copy_id'])['state']['file']['is_offline'],0)

    def failed_journal(self):
        paths=list(self.journals.glob('*/journal.json')); self.assertEqual(len(paths),1)
        return paths[0]

    def test_failed_native_move_recovery_restores_pair_and_current_curation(self):
        with patch.object(LibraryRepository,'_fts_sync_file',side_effect=RuntimeError('FTS injected')):
            with self.assertRaisesRegex(RuntimeError,'FTS injected'):self.transfer()
        self.assertFalse(self.source.exists()); self.assertFalse(self.caption.exists())
        self.service.submit('edit_curation',str(self.source),rating=3,add_tags=['later']).result(5)
        result=transfers.recover(self.service,self.failed_journal(),journal_root=self.journals)
        self.assertEqual(result['status'],'source_restored')
        self.assertEqual(self.source.read_bytes(),b'private image bytes'); self.assertEqual(self.caption.read_bytes(),b'private caption')
        self.assertEqual(self.state()['state']['file']['rating'],3)
        self.assertEqual(self.state()['state']['file']['id'],self.key)
        self.assertEqual([tag['name'] for tag in self.state()['state']['tags']],['curated','later'])
        self.assertEqual(list(self.output.iterdir()),[])
        saved=self.service.read('get_transfer_receipt',result['request']['operation_id'])['receipt']
        self.assertEqual(saved['phase'],'aborted')

    def test_recovery_preserves_new_original_after_failed_native_move(self):
        with patch.object(LibraryRepository,'_fts_sync_file',side_effect=RuntimeError('FTS injected')):
            with self.assertRaises(RuntimeError):self.transfer()
        self.source.write_bytes(b'new unrelated original')
        with self.assertRaisesRegex(ValueError,'changed'):
            transfers.recover(self.service,self.failed_journal(),journal_root=self.journals)
        self.assertEqual(self.source.read_bytes(),b'new unrelated original')
        self.assertTrue((self.output/self.source.name).exists())

    def test_undo_copy_refuses_changed_native_curation(self):
        result=self.transfer('copy')
        self.service.submit('edit_curation',result['current'],rating=2).result(5)
        with self.assertRaisesRegex(Exception,'undo_changed'):self.undo(result)
        self.assertTrue(Path(result['current']).exists())

    def test_undo_copy_refuses_changed_original_and_caption(self):
        result=self.transfer('copy'); self.caption.write_bytes(b'edited original caption')
        with self.assertRaisesRegex(ValueError,'changed'):self.undo(result)
        self.assertTrue(Path(result['current']).exists()); self.assertTrue(Path(result['current']).with_suffix('.txt').exists())

    def test_undo_move_refuses_changed_caption(self):
        result=self.transfer(); Path(result['current']).with_suffix('.txt').write_bytes(b'changed caption')
        with self.assertRaisesRegex(ValueError,'caption_changed'):self.undo(result)
        self.assertFalse(self.source.exists()); self.assertTrue(Path(result['current']).exists())

    def test_collision_uses_pair_suffix_and_undo_original_name(self):
        self.output.mkdir(); (self.output/self.source.name).write_bytes(b'existing image')
        (self.output/self.source.name).with_suffix('.txt').write_bytes(b'existing caption')
        result=self.transfer(); self.assertEqual(Path(result['current']).name,'image Ñ_1.png')
        self.undo(result); self.assertTrue(self.source.exists())
        self.assertEqual((self.output/self.source.name).read_bytes(),b'existing image')

    def test_destination_race_cannot_overwrite_foreign_image(self):
        rename=transfers.os.rename
        def race(src,dst):
            if str(dst).endswith('.png'):Path(dst).write_bytes(b'foreign raced file')
            return rename(src,dst)
        with patch('modules.pickler.logic.transfers.os.rename',side_effect=race):
            with self.assertRaises(RuntimeError):self.transfer()
        self.assertEqual((self.output/self.source.name).read_bytes(),b'foreign raced file'); self.assertTrue(self.source.exists())
        transfers.recover(self.service,self.failed_journal(),journal_root=self.journals)
        self.assertEqual((self.output/self.source.name).read_bytes(),b'foreign raced file'); self.assertTrue(self.source.exists())

    def test_partial_caption_publication_recovery_keeps_original_pair(self):
        rename=transfers.os.rename
        def fail(src,dst):
            if str(dst).endswith('.txt'):raise OSError('caption publication injected')
            return rename(src,dst)
        with patch('modules.pickler.logic.transfers.os.rename',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'caption publication injected'):self.transfer()
        self.assertTrue(self.source.exists()); self.assertTrue(self.caption.exists())
        transfers.recover(self.service,self.failed_journal(),journal_root=self.journals)
        self.assertEqual(self.source.read_bytes(),b'private image bytes'); self.assertEqual(self.caption.read_bytes(),b'private caption')
        self.assertFalse((self.output/self.source.name).exists())

    def test_cancel_before_publication_keeps_originals_and_aborts_reservation(self):
        event=threading.Event(); prepare=transfers.prepare_output
        def stop(*args):
            value=prepare(*args)
            event.set(); return value
        with patch('modules.pickler.logic.transfers.prepare_output',side_effect=stop):
            with self.assertRaisesRegex(RuntimeError,'cancelled'):self.transfer(event=event)
        self.assertTrue(self.source.exists()); self.assertTrue(self.caption.exists())
        data=json.loads(self.failed_journal().read_bytes())
        self.assertEqual(self.service.read('get_transfer_receipt',data['operation_id'])['receipt']['phase'],'aborted')
        self.assertEqual(self.state()['state']['file']['rating'],5)

    def test_cancel_after_publication_finishes_admitted_pair_and_native(self):
        event=threading.Event(); publish=transfers.published_pin
        def stop(*args):
            result=publish(*args); event.set(); return result
        with patch('modules.pickler.logic.transfers.published_pin',side_effect=stop):result=self.transfer(event=event)
        self.assertEqual(result['receipt']['phase'],'complete'); self.assertFalse(self.source.exists())
        self.assertTrue(Path(result['current']).with_suffix('.txt').exists())

    def test_undo_partial_handle_deletion_can_resume(self):
        result=self.transfer('copy'); mark=PinnedFile.mark_delete
        def fail(pin):
            if pin.path.suffix=='.png':raise OSError('image deletion injected')
            return mark(pin)
        with patch.object(PinnedFile,'mark_delete',fail):
            with self.assertRaisesRegex(OSError,'image deletion injected'):self.undo(result)
        self.assertEqual(self.state(result['receipt']['result_copy_id'])['state']['file']['is_offline'],1)
        self.assertTrue(Path(result['current']).exists()); self.assertFalse(Path(result['current']).with_suffix('.txt').exists())
        transfers.recover(self.service,result['journal'],journal_root=self.journals)
        self.assertFalse(Path(result['current']).exists()); self.assertTrue(self.source.exists())

    def test_no_caption_pair_and_new_caption_blocks_undo(self):
        self.caption.unlink(); result=self.transfer('copy')
        Path(result['current']).with_suffix('.txt').write_bytes(b'new caption')
        with self.assertRaisesRegex(ValueError,'caption_changed'):self.undo(result)
        self.assertTrue(Path(result['current']).exists())

    def test_shared_caption_requires_review_before_native_or_files(self):
        self.source.with_suffix('.jpg').write_bytes(b'other image')
        with self.assertRaisesRegex(ValueError,'shared_caption'):self.transfer()
        self.assertTrue(self.source.exists()); self.assertTrue(self.caption.exists()); self.assertFalse(self.journals.exists())

    def test_unindexed_input_gets_native_identity_before_copy(self):
        other=self.folder/'unindexed.png'; other.write_bytes(b'unindexed bytes')
        result=transfers.transfer(self.service,other,self.output,'copy',journal_root=self.journals)
        self.assertTrue(other.exists()); self.assertNotEqual(result['receipt']['result_copy_id'],result['receipt']['source_copy_id'])

    def test_prepared_copy_undo_and_repeated_copy_keep_complete_identity(self):
        self.service.backfill_path_identity()
        first=self.transfer('copy')
        self.undo(first)
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)
        second=self.transfer('copy')
        self.assertNotEqual(first['receipt']['result_copy_id'],second['receipt']['result_copy_id'])
        self.assertEqual(self.service.resolve_path_identity(second['current'])['file_id'],second['receipt']['result_copy_id'])
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)

    def test_prepared_identity_move_and_recovery(self):
        self.service.submit('prepare_path_identity').result(5)
        self.service.backfill_path_identity()
        result=self.transfer()
        self.assertEqual(self.service.resolve_path_identity(result['current'])['file_id'],self.key)
        self.undo(result)
        self.assertEqual(self.service.resolve_path_identity(str(self.source))['file_id'],self.key)

    def test_recovery_never_rolls_back_confirmed_native_receipt(self):
        result=self.transfer(); original=self.state()
        data=json.loads(Path(result['journal']).read_bytes()); data['phase']='review_required'
        Path(result['journal']).write_text(json.dumps(data),encoding='utf-8')
        recovered=transfers.recover(self.service,result['journal'],journal_root=self.journals)
        self.assertEqual(recovered['status'],'already_confirmed'); self.assertEqual(original,self.state())
        self.assertFalse(self.source.exists()); self.assertTrue(Path(result['current']).exists())

    def test_pending_ingestion_rejected_without_altering_curation(self):
        with patch.object(LibraryRepository,'_fts_sync_file',side_effect=RuntimeError('FTS injected')):
            with self.assertRaises(RuntimeError):self.transfer()
        before=self.state(); output=self.output/self.source.name
        with self.assertRaisesRegex(Exception,'ingestion_pending'):
            self.service.register_files_observed([dict(path=str(output),filename=output.name,size=19,created=1)])
        self.assertEqual(before,self.state()); self.assertEqual(self.service.read('count_files'),1)

    def test_missing_service_cannot_move_files(self):
        with self.assertRaisesRegex(ValueError,'unavailable'):transfers.transfer(None,self.source,self.output,'move')
        self.assertTrue(self.source.exists()); self.assertTrue(self.caption.exists())

    def test_prepared_registry_recovery_restores_fresh_physical_identity(self):
        self.service.submit('prepare_path_identity').result(5); self.service.backfill_path_identity()
        with patch.object(LibraryRepository,'_fts_sync_file',side_effect=RuntimeError('FTS injected')):
            with self.assertRaises(RuntimeError):self.transfer()
        transfers.recover(self.service,self.failed_journal(),journal_root=self.journals)
        self.assertEqual(self.service.resolve_path_identity(str(self.source))['file_id'],self.key)
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)

    def test_recovery_list_finds_pending_and_skips_confirmed(self):
        result=self.transfer('copy')
        self.assertEqual(transfers.list_recovery(self.service,journal_root=self.journals)['records'],[])
        other=self.folder/'pending.png'; other.write_bytes(b'pending bytes')
        self.service.submit('register_files_minimal',[dict(path=str(other),filename=other.name,size=13,created=1)]).result(5)
        with patch.object(LibraryRepository,'_fts_sync_file',side_effect=RuntimeError('FTS injected')):
            with self.assertRaises(RuntimeError):transfers.transfer(self.service,other,self.output,'move',journal_root=self.journals)
        listed=transfers.list_recovery(self.service,journal_root=self.journals)
        self.assertEqual(len(listed['records']),1); self.assertEqual(listed['records'][0]['request']['source_path'],other.as_posix())
        self.assertFalse(listed['truncated'])

    def test_recovery_rejects_changed_journal_request_and_outside_scope(self):
        result=self.transfer('copy'); path=Path(result['journal']); data=json.loads(path.read_bytes())
        data['request']['digest']='f'*64; path.write_text(json.dumps(data),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'journal_changed'):transfers.recover(self.service,path,journal_root=self.journals)
        outside=self.root/'journal.json'; outside.write_bytes(path.read_bytes())
        with self.assertRaisesRegex(ValueError,'scope'):transfers.recover(self.service,outside,journal_root=self.journals)
        self.assertTrue(self.source.exists()); self.assertTrue(Path(result['current']).exists())

    def test_case_renamed_parent_with_stale_registry_preserves_curated_source(self):
        self.service.submit('prepare_path_identity').result(5); self.service.backfill_path_identity()
        self.folder.rename(self.root/'Input')
        result=self.transfer('copy')
        self.assertEqual(result['receipt']['source_copy_id'],self.key)
        copied=self.service.read('get_copy_version_state',result['receipt']['result_copy_id'])['state']
        self.assertEqual(copied['file']['rating'],5); self.assertEqual(copied['tags'][0]['name'],'curated')
        self.assertEqual(self.service.read('count_files'),2)

    def test_copy_after_undo_uses_new_name_and_preserves_offline_history(self):
        first=self.transfer('copy'); self.undo(first)
        second=self.transfer('copy')
        self.assertNotEqual(first['receipt']['result_copy_id'],second['receipt']['result_copy_id'])
        self.assertNotEqual(first['current'],second['current'])
        self.assertEqual(self.state(first['receipt']['result_copy_id'])['state']['file']['is_offline'],1)
        self.assertEqual(self.state(second['receipt']['result_copy_id'])['state']['file']['rating'],5)


if __name__=='__main__':unittest.main(verbosity=2)
