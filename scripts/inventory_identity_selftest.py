"""Prepared inventory/retirement identities; private Windows files and DB only."""
from contextlib import closing
from pathlib import Path
import sqlite3, sys, threading, unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import path_identity_ingest_selftest as helpers
from core.library_connections import open_reader
from core.library_service import LibraryError
from core.path_identity import observe_path
from modules.librarian.logic.indexer import IndexerWorker
from core.inventory import InventoryStatus


class InventoryIdentity(unittest.TestCase):
    setUp=helpers.Ingest.setUp
    image=helpers.Ingest.image
    row=helpers.Ingest.row
    seed=helpers.Ingest.seed
    sql=helpers.Ingest.sql

    def approved(self,path):
        value=self.service.read('get_duplicate_snapshot',[path.as_posix()])[0]
        return dict(path=value['path'],copy_id=value['state']['file']['id'],fingerprint=value['fingerprint'])

    def test_prepared_unicode_inventory_reuses_one_lease_without_snapshot_during_probes(self):
        first=self.image('Árbol/One.png'); second=self.image('Árbol/Two.png')
        self.seed(first); self.seed(second); self.service.backfill_path_identity()
        connections=[]
        def reader(path):
            conn=open_reader(path); connections.append(conn); return conn
        def observe(path,**kwargs):
            self.assertEqual(self.service.metrics_snapshot()['reads_active'],0)
            self.assertEqual(len(connections),1); self.assertFalse(connections[0].in_transaction)
            return observe_path(path,**kwargs)
        with patch('core.library_service.open_reader',side_effect=reader),patch('core.folder_tasks.observe_path',side_effect=observe):
            with self.db.inventory_reader() as scoped:
                page=scoped.get_inventory_page(str(self.root/'árbol'),limit=1)
                self.assertEqual(page['rows'][0][1],first.as_posix())
                next_page=scoped.get_inventory_page(str(self.root/'árbol'),limit=1,after_id=page['after_id'],upper_id=page['upper_id'])
                self.assertEqual(next_page['rows'][0][1],second.as_posix())
        self.assertEqual(len(connections),1)

    def test_inventory_frontier_excludes_later_confirmed_additions(self):
        first=self.image('Images/One.png'); self.seed(first); self.service.backfill_path_identity()
        with self.db.inventory_reader() as scoped:
            page=scoped.get_inventory_page(str(first.parent),limit=1)
            other=self.image('Images/Two.png')
            self.service.register_files_observed([self.row(other)],cancel_event=self.event)
            tail=scoped.get_inventory_page(str(first.parent),limit=1,after_id=page['after_id'],upper_id=page['upper_id'])
            self.assertEqual(tail['rows'],[])

    def test_prepared_multi_batch_missing_archive_keeps_registry_complete_and_curation(self):
        paths=[self.image(f'Images/{i}.png') for i in range(300)]
        self.service.submit('register_files_minimal',[self.row(path) for path in paths]).result(5)
        self.service.submit('edit_curation',str(paths[0]),rating=5,add_tags=['keep']).result(3)
        self.service.backfill_path_identity()
        for path in paths:path.unlink()
        worker=IndexerWorker(self.db,[str(paths[0].parent)],deep_clean=True); worker.run()
        self.assertEqual((worker.outcome,worker.archived_count),(InventoryStatus.COMPLETE,300))
        status=self.service.read('get_path_identity_status'); self.assertEqual((status['pending'],status['unresolved']),(0,0))
        self.assertEqual(self.sql('SELECT count(*) FROM files WHERE is_offline=1'),[(300,)])
        self.assertEqual(self.sql('SELECT rating FROM files WHERE path=\''+paths[0].as_posix()+"'"),[(5,)])
        self.assertEqual(self.sql("SELECT t.name FROM tags t JOIN file_tags ft ON t.id=ft.tag_id"),[('keep',)])

    def test_prepared_owner_rejects_case_distinct_wrong_scope_even_with_valid_fingerprint(self):
        path=self.image('Root/One.png'); self.seed(path); self.service.backfill_path_identity()
        scope=(self.root/'root').as_posix()
        proof=dict(path=scope,canonical=scope,physical=observe_path(str(path.parent)).physical)
        with self.assertRaisesRegex(LibraryError,'scope_changed'):
            self.service.submit('archive_missing_copies',[self.approved(path)],folder=scope,scope=proof).result(3)
        self.assertEqual(self.sql('SELECT is_offline FROM files'),[(0,)])

    def test_prepared_owner_requires_observed_scope_for_inventory_retirement(self):
        path=self.image('Images/One.png'); self.seed(path); self.service.backfill_path_identity()
        with self.assertRaisesRegex(LibraryError,'review_required'):
            self.service.submit('archive_missing_copies',[self.approved(path)],folder=path.parent.as_posix()).result(3)
        self.assertEqual(self.sql('SELECT is_offline FROM files'),[(0,)])

    def test_scalar_removal_preserves_complete_offline_identity_without_schema_migration(self):
        path=self.image(); key=self.seed(path); self.service.backfill_path_identity()
        self.service.submit('remove_files',[str(path)]).result(3)
        self.assertEqual(self.sql('SELECT file_id,is_offline,status,canonical,physical FROM path_identity_records'),[(key,1,'offline',None,None)])
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)
        self.assertTrue(path.exists())

    def test_reviewed_copy_archive_keeps_prepared_registry_complete(self):
        path=self.image(); key=self.seed(path); self.service.backfill_path_identity()
        state=self.service.read('get_copy_version_state',key)
        self.service.submit('archive_copy',key,expected_fingerprint=state['fingerprint']).result(3)
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)
        self.assertEqual(self.sql('SELECT status FROM path_identity_records'),[('offline',)])

    def test_binding_failure_rolls_back_flag_and_registry_together(self):
        path=self.image(); self.seed(path); self.service.backfill_path_identity()
        with patch('core.path_identity_offline.query',side_effect=sqlite3.IntegrityError('private binding failure')):
            with self.assertRaisesRegex(LibraryError,'operation_failed'):
                self.service.submit('remove_files',[str(path)]).result(3)
        self.assertEqual(self.sql('SELECT is_offline FROM files'),[(0,)])
        self.assertEqual(self.sql('SELECT is_offline,status FROM path_identity_records'),[(0,'resolved')])

    def test_unprepared_retirement_does_not_create_identity_schema(self):
        path=self.image(); self.seed(path)
        self.service.submit('remove_files',[str(path)]).result(3)
        self.assertEqual(self.sql("SELECT name FROM sqlite_master WHERE name='path_identity_records'"),[])


if __name__=='__main__':unittest.main(verbosity=2)
