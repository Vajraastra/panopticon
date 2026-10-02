"""Identidad offline/versiones y migración; SQLite/archivos temporales, sin biblioteca real."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.copy_version_tasks import CopyVersionTasks, observe
from core.curation_sidecar import export_curation, read_curation_page, compare_curation_page, NAME
from core.curation_apply import CurationApplyTasks
from core.database_manager import DatabaseManager
from core.library_repository import LibraryRepository
from core.library_service import LibraryError, LibraryService
from core.library_versions_schema import migrate, MARKER


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Versions(unittest.TestCase):

    def test_prepared_parent_case_change_detects_existing_active_alias_before_restore(self):
        self.action('archive_copy')
        alias=str(self.image).upper()
        self.service.submit('register_files_minimal',[dict(path=alias,filename='SAME.PNG',size=11,created=1)]).result(3)
        self.service.backfill_path_identity(); before=self.state()
        self.folder.rename(self.folder.with_name(self.folder.name.upper()))
        with self.assertRaisesRegex(ValueError,'occupied'): self.action('make_copy_online')
        self.assertEqual(self.state(),before); self.assertEqual(self.service.read('count_files'),1)

    def test_prepared_hardlink_different_name_keeps_each_copy_independent(self):
        linked=self.image.with_name('linked.png'); os.link(self.image,linked)
        self.service.submit('register_files_minimal',[dict(path=str(linked),filename=linked.name,size=11,created=1)]).result(3)
        linked_id=self.db.get_file_id(str(linked)); self.service.backfill_path_identity()
        self.action('archive_copy'); self.action('make_copy_online')
        self.assertEqual(self.service.read('count_files'),2)
        self.assertEqual(self.state(linked_id)['state']['tags'],[])
        self.assertEqual(self.state()['state']['tags'][0]['name'],'documented')
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)

    def test_prepared_candidate_added_before_admission_rejects_stale_proof(self):
        import json
        from core.path_identity import observe_path
        self.service.backfill_path_identity(); self.action('archive_copy'); before=self.state()
        native=self.service.submit
        def race(operation,*args,**kwargs):
            if operation=='make_copy_online':
                identity=observe_path(str(self.image))
                with closing(sqlite3.connect(self.service.db_path)) as conn:
                    alias=str(self.image).upper().replace('\\','/')
                    fresh=conn.execute('INSERT INTO files(path,filename) VALUES(?,?)',(alias,'SAME.PNG')).lastrowid
                    conn.execute('INSERT INTO path_identity_records VALUES(?,?,0,?,?,?,NULL)',
                        (fresh,alias,'resolved',identity.canonical,json.dumps(identity.physical))); conn.commit()
            return native(operation,*args,**kwargs)
        with patch.object(self.service,'submit',side_effect=race):
            with self.assertRaisesRegex(LibraryError,'sql_state_changed'): self.action('make_copy_online')
        self.assertEqual(self.state(),before); self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)


    def test_prepared_online_same_id_keeps_curation_and_complete_scope(self):
        self.service.backfill_path_identity()
        self.action('archive_copy'); before=self.state()['state']
        self.action('make_copy_online')
        coverage=self.service.read('get_path_identity_status')
        self.assertEqual((coverage['pending'],coverage['unresolved']),(0,0))
        self.assertEqual(self.service.read_worker('get_folder_count',str(self.folder)),1)
        current=self.state()['state']; current['file']['is_offline']=1
        self.assertEqual(current,before)

    def test_prepared_changed_bytes_have_new_clean_id_and_live_binding(self):
        self.service.backfill_path_identity(); self.action('archive_copy')
        self.image.write_bytes(b'different current bytes')
        self.action('make_copy_online')
        fresh=self.db.get_file_id(str(self.image)); self.assertNotEqual(fresh,self.key)
        self.assertEqual(self.state(fresh)['state']['tags'],[])
        self.assertEqual(self.state(fresh)['state']['file']['rating'],0)
        self.assertEqual(self.state()['state']['file']['is_offline'],1)
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)
        self.assertEqual(self.service.read_worker('get_folder_count',str(self.folder)),1)
        self.assertEqual(self.sql('SELECT status,is_offline FROM path_identity_records WHERE file_id=?',(fresh,)),[('resolved',0)])

    def test_prepared_unknown_bytes_also_keep_old_curation_offline(self):
        self.sql('UPDATE files SET hash_original=NULL WHERE id=?',(self.key,))
        self.service.backfill_path_identity(); self.action('archive_copy'); self.action('make_copy_online')
        fresh=self.db.get_file_id(str(self.image)); self.assertNotEqual(fresh,self.key)
        self.assertEqual(self.state()['state']['tags'][0]['name'],'documented')
        self.assertEqual(self.state(fresh)['state']['tags'],[])
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)

    def test_prepared_online_native_without_worker_identity_is_rejected(self):
        self.service.backfill_path_identity(); self.action('archive_copy'); before=self.state()
        current,observation=observe(self.image)
        with self.assertRaisesRegex(LibraryError,'review_required'):
            self.service.submit('make_copy_online',self.key,expected_fingerprint=before['fingerprint'],
                digest=current,observation=observation).result(3)
        self.assertEqual(self.state(),before)
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)

    def test_prepared_online_binding_failure_rolls_back_availability_and_new_id(self):
        self.service.backfill_path_identity(); self.action('archive_copy'); before=self.state()
        self.image.write_bytes(b'different')
        with patch('core.path_identity_online.store',side_effect=ValueError('binding_failed')):
            with self.assertRaisesRegex(LibraryError,'binding_failed'): self.action('make_copy_online')
        self.assertEqual(self.state(),before); self.assertEqual(self.sql('SELECT count(*) FROM files'),[(1,)])
        self.assertTrue(self.db.check_fts()['consistent'])
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)

    def test_prepared_online_pin_protects_bytes_until_native_commit(self):
        self.service.backfill_path_identity(); self.action('archive_copy')
        original=LibraryRepository.make_copy_online; attempts=[]
        def blocked(repo,*args,**kwargs):
            with self.assertRaises(OSError): self.image.write_bytes(b'unapproved writer')
            attempts.append(True); return original(repo,*args,**kwargs)
        with patch.object(LibraryRepository,'make_copy_online',blocked): self.action('make_copy_online')
        self.assertEqual(attempts,[True]); self.assertEqual(self.image.read_bytes(),b'first bytes')
        self.image.write_bytes(b'after commit')  # The worker released its handle.

    def test_prepared_online_canonical_occupied_alias_preserves_offline_id(self):
        self.action('archive_copy')
        alias=str(self.image).upper().replace('SAME.PNG','same.png')
        self.service.submit('register_files_minimal',[dict(path=alias,filename='same.png',size=11,created=1)]).result(3)
        self.service.backfill_path_identity(); before=self.state()
        with self.assertRaisesRegex((ValueError,LibraryError),'occupied'): self.action('make_copy_online')
        self.assertEqual(self.state(),before)
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)

    def test_preparation_between_legacy_hash_and_admission_does_not_publish_missing_binding(self):
        self.action('archive_copy'); before=self.state(); native=observe
        def race(*args):
            proof=native(*args); self.service.backfill_path_identity(); return proof
        with patch('core.copy_version_tasks.observe',side_effect=race):
            with self.assertRaisesRegex(LibraryError,'review_required'): self.action('make_copy_online')
        self.assertEqual(self.state(),before)
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / 'colección ñ'
        self.folder.mkdir()
        self.image = self.folder / 'same.png'
        self.image.write_bytes(b'first bytes')
        self.service = LibraryService(self.root / 'central.db')
        self.addCleanup(self.service.close)
        self.db = DatabaseManager(service=self.service)
        self.db.register_files_minimal([dict(path=str(self.image), filename=self.image.name,
            size=11, created=1, hash_original=digest(b'first bytes'), origin_url='source',
            origin_site='fixture', artist='Artist')])
        self.key = self.db.get_file_id(str(self.image))
        self.db.edit_curation(str(self.image), add_tags=['documented'], rating=5)
        self.db.update_file_metadata(str(self.image), dict(width=32, height=64, tool='Tool',
            model='Model', positive='prompt', negative='negative', seed='9'))
        self.tasks = CopyVersionTasks(self.service)

    def sql(self, text, args=()):
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            result = conn.execute(text, args).fetchall()
            conn.commit()
            return result

    def state(self, key=None):
        return self.service.read('get_copy_version_state', self.key if key is None else key)

    def action(self, action, key=None):
        key = self.key if key is None else key
        return self.tasks.read(action, copy_id=key, expected_fingerprint=self.state(key)['fingerprint'])

    def test_archive_keeps_identity_all_metadata_curation_and_fts(self):
        before = self.state()['state']
        self.action('archive_copy')
        after = self.state()['state']
        after['file']['is_offline'] = 0
        self.assertEqual(after, before)
        self.assertTrue(self.db.check_fts()['consistent'])
        self.assertEqual(self.sql('SELECT count(*) FROM files_fts'), [(1,)])
        self.assertEqual(self.image.read_bytes(), b'first bytes')

    def test_operational_queries_exclude_offline(self):
        self.action('archive_copy')
        folder, image = str(self.folder), str(self.image)
        self.assertEqual(self.db.count_files(), 0)
        self.assertEqual(self.db.get_folder_count(folder), 0)
        self.assertEqual(self.db.get_folder_preview(folder), [])
        self.assertEqual(self.db.get_files_recursive(folder), [])
        self.assertEqual(self.db.get_known_files_in_folder(folder), set())
        self.assertEqual(self.db.search_by_terms(['documented']), (0, []))
        self.assertEqual(self.db.search_files_paginated(tags=['documented']), (0, []))
        self.assertEqual(self.db.get_library_page(folder=folder)['count'], 0)
        self.assertEqual(self.db.resolve_library_selection(terms=['prompt'])['paths'], [])
        self.assertEqual(self.db.get_library_selection(folder=folder)['count'], 0)
        self.assertIsNone(self.db.get_file_id(image))
        self.assertEqual(self.db.get_tags_for_file(image), [])
        self.assertIsNone(self.db.get_curation_snapshot([image])[0]['id'])
        self.assertEqual(self.db.get_file_rating(image), 0)
        self.assertEqual(self.db.get_files_missing_hash(), [])
        with self.assertRaisesRegex(ValueError, 'file_not_registered'):
            self.db.get_curation_restore_state(image)

    def test_same_current_bytes_reactivate_same_id_and_curation(self):
        self.action('archive_copy')
        before = self.state()['state']['tags']
        self.action('make_copy_online')
        self.assertEqual(self.db.get_file_id(str(self.image)), self.key)
        self.assertEqual(self.state()['state']['tags'], before)
        self.assertEqual(self.db.get_file_rating(str(self.image)), 5)
        self.assertEqual(self.sql('SELECT count(*) FROM files'), [(1,)])

    def test_different_bytes_get_fresh_id_without_curated_data(self):
        before = self.state()['state']
        self.image.write_bytes(b'second bytes')
        self.action('make_copy_online')
        key = self.db.get_file_id(str(self.image))
        self.assertNotEqual(key, self.key)
        old = self.state()['state']
        old['file']['is_offline'] = 0
        self.assertEqual(old, before)
        new = self.state(key)['state']
        self.assertEqual(new['tags'], [])
        self.assertEqual(new['file']['rating'], 0)
        self.assertEqual(new['file']['hash_original'], digest(b'second bytes'))
        for name in ('meta_model', 'meta_tool', 'meta_positive', 'meta_negative', 'meta_seed',
                     'origin_url', 'origin_site', 'artist', 'hash_mod', 'phash'):
            self.assertIsNone(new['file'][name], name)
        self.assertTrue(self.db.check_fts()['consistent'])

    def test_modified_hash_is_the_current_identity(self):
        self.sql('UPDATE files SET hash_mod=? WHERE id=?', (digest(b'first bytes'), self.key))
        self.sql('UPDATE files SET hash_original=? WHERE id=?', (digest(b'raw bytes'), self.key))
        self.action('archive_copy')
        self.action('make_copy_online')
        self.assertEqual(self.db.get_file_id(str(self.image)), self.key)
        self.image.write_bytes(b'raw bytes')
        self.action('make_copy_online')
        self.assertNotEqual(self.db.get_file_id(str(self.image)), self.key)

    def test_inventory_neither_reactivates_nor_inherits_curation(self):
        self.action('archive_copy')
        before = self.state()
        self.assertTrue(self.db.register_files_minimal([dict(path=str(self.image), filename='same.png',
            size=999, created=88, hash_original=digest(b'new'), artist='New')]))
        self.assertEqual(self.state(), before)
        self.assertIsNone(self.db.get_file_id(str(self.image)))

    def test_inventory_leaves_both_versions_unchanged(self):
        self.image.write_bytes(b'new bytes')
        self.action('make_copy_online')
        before = self.sql('SELECT * FROM files ORDER BY id')
        self.db.register_files_minimal([dict(path=str(self.image), filename='same.png', size=99, created=88)])
        self.assertEqual(self.sql('SELECT * FROM files ORDER BY id'), before)

    def test_path_only_writes_cannot_modify_new_version(self):
        self.image.write_bytes(b'new bytes')
        self.action('make_copy_online')
        before = self.sql('SELECT * FROM files ORDER BY id')
        for operation, args in [('edit_curation', (str(self.image),)),
                ('update_file_rating', (str(self.image), 4)), ('add_tag_to_file', (str(self.image), 'wrong')),
                ('remove_tag_from_file', (str(self.image), 'documented')),
                ('update_file_metadata', (str(self.image), {'model': 'wrong'}))]:
            kwargs = {'rating': 4} if operation == 'edit_curation' else {}
            with self.subTest(operation=operation), self.assertRaisesRegex(LibraryError, 'version_identity_required'):
                self.service.submit(operation, *args, **kwargs).result(3)
        self.assertEqual(self.sql('SELECT * FROM files ORDER BY id'), before)

    def test_regular_restore_registration_refuses_historical_path(self):
        incoming = self.db.get_curation_restore_state(str(self.image))['state']
        incoming = {key: value for key, value in incoming.items() if key != 'id'}
        self.action('archive_copy')
        with self.assertRaisesRegex(LibraryError, 'version_identity_required'):
            self.service.submit('register_curation_copy', str(self.image), incoming=incoming,
                observation=dict(filename='same.png', size=11, created=1)).result(3)

    def test_bound_edit_targets_new_copy_and_keeps_offline_curation(self):
        self.image.write_bytes(b'new bytes')
        self.action('make_copy_online')
        old = self.state()
        key = self.db.get_file_id(str(self.image))
        self.assertTrue(self.service.submit('edit_curation', str(self.image), expected_copy_id=key,
                    add_tags=['new-curation'], rating=3).result(3))
        self.assertEqual(self.state(), old)
        self.assertEqual(self.db.get_curation_snapshot([str(self.image)])[0]['tags'], ('new-curation',))

    def test_stale_bound_edit_cannot_modify_replacement_copy(self):
        self.image.write_bytes(b'new bytes')
        self.action('make_copy_online')
        before = self.sql('SELECT * FROM files ORDER BY id')
        with self.assertRaisesRegex(LibraryError, 'curation_copy_changed'):
            self.service.submit('edit_curation', str(self.image), expected_copy_id=self.key,
                                add_tags=['wrong'], rating=4).result(3)
        self.assertEqual(self.sql('SELECT * FROM files ORDER BY id'), before)
        self.assertNotIn('wrong', self.db.get_all_tags())

    def test_curation_changed_since_review_blocks_archive(self):
        reviewed = self.state()['fingerprint']
        self.db.edit_curation(str(self.image), rating=4)
        with self.assertRaisesRegex(ValueError, 'changed_since_review'):
            self.tasks.read('archive_copy', copy_id=self.key, expected_fingerprint=reviewed)
        self.assertEqual(self.state()['state']['file']['is_offline'], 0)

    def test_shared_category_change_invalidates_review(self):
        reviewed = self.state()['fingerprint']
        self.sql("UPDATE tags SET category='changed'")
        with self.assertRaisesRegex(LibraryError, 'changed_since_review'):
            self.service.submit('archive_copy', self.key, expected_fingerprint=reviewed).result(3)

    def test_failed_archive_preserves_state_and_owner(self):
        before = self.state()
        self.sql("CREATE TRIGGER fail_archive BEFORE UPDATE OF is_offline ON files BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaisesRegex(LibraryError, 'fixture'):
            self.action('archive_copy')
        self.assertEqual(self.state(), before)
        self.assertEqual(self.db.count_files(), 1)

    def test_failed_new_fts_rolls_back_old_flag_new_id_and_sequence(self):
        before = self.state()
        sequence = self.sql('SELECT * FROM sqlite_sequence')
        self.image.write_bytes(b'new bytes')
        with patch.object(LibraryRepository, '_fts_sync_file', side_effect=sqlite3.OperationalError('fixture')):
            with self.assertRaisesRegex(LibraryError, 'fixture'):
                self.action('make_copy_online')
        self.assertEqual(self.state(), before)
        self.assertEqual(self.sql('SELECT * FROM sqlite_sequence'), sequence)
        self.assertEqual(self.sql('SELECT count(*) FROM files'), [(1,)])

    def test_same_approval_cannot_create_two_versions(self):
        approved = self.state()['fingerprint']
        self.image.write_bytes(b'new bytes')
        current_digest, observation = observe(self.image)
        requests = [self.service.submit('make_copy_online', self.key, expected_fingerprint=approved,
                    digest=current_digest, observation=observation) for _ in range(2)]
        self.assertIs(requests[0].result(3), True)
        with self.assertRaisesRegex(LibraryError, 'changed_since_review'):
            requests[1].result(3)
        self.assertEqual(self.sql('SELECT count(*) FROM files'), [(2,)])

    def test_other_active_version_blocks_old_reactivation(self):
        self.image.write_bytes(b'new bytes')
        self.action('make_copy_online')
        before = self.sql('SELECT * FROM files ORDER BY id')
        self.image.write_bytes(b'first bytes')
        with self.assertRaisesRegex(LibraryError, 'path_occupied'):
            self.action('make_copy_online')
        self.assertEqual(self.sql('SELECT * FROM files ORDER BY id'), before)

    def test_archive_absent_file_does_not_read_filesystem(self):
        self.image.unlink()
        with patch('core.copy_version_tasks.observe', side_effect=AssertionError('IO')):
            self.action('archive_copy')
        self.assertEqual(self.state()['state']['file']['is_offline'], 1)

    def test_missing_file_cannot_reactivate(self):
        self.action('archive_copy')
        before = self.state()
        self.image.unlink()
        with self.assertRaises(FileNotFoundError):
            self.action('make_copy_online')
        self.assertEqual(self.state(), before)

    def test_cancellation_does_not_hash_or_submit(self):
        event = threading.Event()
        event.set()
        before = self.state()
        with patch('core.copy_version_tasks.observe', side_effect=AssertionError('IO')):
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                self.tasks.read('make_copy_online', copy_id=self.key,
                    expected_fingerprint=before['fingerprint'], cancel_event=event)
        self.assertEqual(self.state(), before)

    def test_symlink_refuses_before_hash(self):
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaisesRegex(ValueError, 'symlink'):
                self.action('make_copy_online')
        self.assertEqual(self.db.count_files(), 1)

    def test_bytes_changed_while_hashing_preserve_native_state(self):
        before = self.state()
        original = hashlib.sha256
        hasher = original()
        class Changing:
            def update(inner, data):
                hasher.update(data)
                self.image.write_bytes(b'replaced with different length')
            def hexdigest(inner):
                return hasher.hexdigest()
        with patch('core.copy_version_tasks.hashlib.sha256', return_value=Changing()):
            # observe directamente: el patch global de hashlib también afecta huellas DB.
            with self.assertRaisesRegex(ValueError, 'file_changed'):
                observe(self.image)
        self.assertEqual(self.state(), before)

    def test_state_changed_after_hash_is_rejected_at_commit(self):
        original = observe
        def observed(*args):
            result = original(*args)
            self.db.edit_curation(str(self.image), rating=3)
            return result
        with patch('core.copy_version_tasks.observe', observed):
            with self.assertRaisesRegex(LibraryError, 'changed_since_review'):
                self.action('make_copy_online')
        self.assertEqual(self.db.get_file_rating(str(self.image)), 3)
        self.assertEqual(self.sql('SELECT count(*) FROM files'), [(1,)])

    def test_pages_are_bounded_and_explicit(self):
        self.action('archive_copy')
        first = self.db.get_copy_versions_page(limit=1)
        self.assertEqual([row['id'] for row in first['copies']], [self.key])
        self.assertEqual(self.db.get_copy_versions_page(after_id=first['next_after_id'])['copies'], [])
        for limit in (0, 257, True):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, 'invalid_copy_versions_page'):
                self.db.get_copy_versions_page(limit=limit)

    def test_signed_legacy_ids_are_queryable_and_archivable(self):
        for key in (-7, 0):
            self.sql('INSERT INTO files(id,path,filename) VALUES(?,?,?)', (key, f'legacy-{key}.png', 'legacy.png'))
            self.action('archive_copy', key)
        self.assertEqual([row['id'] for row in self.db.get_copy_versions_page()['copies']], [-7, 0])

    def test_large_generation_payload_does_not_archive_blindly(self):
        self.sql('UPDATE files SET meta_positive=? WHERE id=?', ('x'*65537, self.key))
        with self.assertRaisesRegex(ValueError, 'payload_too_large'):
            self.state()
        self.assertEqual(self.sql('SELECT is_offline FROM files'), [(0,)])

    def test_probe_excludes_offline_without_writing(self):
        from core import library_probe
        with patch.object(library_probe, 'db_path', return_value=Path(self.service.db_path)):
            self.assertEqual(library_probe.artist_summary('Artist')['files'], 1)
            self.action('archive_copy')
            self.assertEqual(library_probe.artist_summary('Artist')['files'], 0)
            self.assertEqual(library_probe.known_artists(), [])

    def test_export_preserves_both_versions_and_curated_history_portably(self):
        cherry = self.folder / 'catalog.db'
        cherry.write_bytes(b'Cherry sentinel')
        self.image.write_bytes(b'new bytes')
        self.action('make_copy_online')
        self.assertEqual(export_curation(self.service.db_path, self.folder)['copies'], 2)
        copies = read_curation_page(self.folder)['copies']
        self.assertEqual([row['relative_path'] for row in copies], ['same.png', 'same.png'])
        self.assertEqual([row['is_offline'] for row in copies], [1, 0])
        self.assertEqual([row['rating'] for row in copies], [5, 0])
        self.assertEqual(copies[0]['tags'][0]['name'], 'documented')
        self.assertEqual(copies[1]['tags'], [])
        self.assertEqual(cherry.read_bytes(), b'Cherry sentinel')
        self.assertEqual(self.image.read_bytes(), b'new bytes')
        moved = self.root / 'movida ñ'
        self.folder.rename(moved)
        self.assertEqual(read_curation_page(moved)['copies'], copies)
        with closing(sqlite3.connect(moved / NAME)) as db:
            self.assertEqual({row[1] for row in db.execute('PRAGMA table_info(curation_copies)')},
                {'id', 'relative_path', 'hash_original', 'hash_mod', 'rating', 'file_size', 'created_date', 'is_offline'})

    def test_offline_sidecar_cannot_restore_or_register_through_regular_apply(self):
        self.action('archive_copy')
        export_curation(self.service.db_path, self.folder)
        page = read_curation_page(self.folder)
        before = self.state()
        with patch('core.curation_apply.verify_file', side_effect=AssertionError('IO')):
            with self.assertRaisesRegex(ValueError, 'offline_version_requires_review'):
                CurationApplyTasks(self.service).read('apply_curation', folder=str(self.folder),
                    copy_id=1, relative_path='same.png', export_id=page['export_id'],
                    expected_fingerprint=None, mode='register')
        self.assertEqual(self.state(), before)
        report = compare_curation_page(self.service.db_path, self.folder)['reports'][0]
        self.assertEqual(report['status'], 'offline_history')
        self.assertIsNone(report['central'])

    def legacy_sidecar(self):
        path = self.folder / NAME
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value)')
            db.executemany('INSERT INTO meta VALUES(?,?)', [('owner', 'panopticon'),
                ('panopticon_curation_version', '1'), ('export_id', 'a'*32), ('copy_count', 1)])
            db.execute('CREATE TABLE curation_copies(id INTEGER PRIMARY KEY,relative_path TEXT UNIQUE NOT NULL,'
                'hash_original TEXT,hash_mod TEXT,rating INTEGER,file_size INTEGER,created_date)')
            db.execute('INSERT INTO curation_copies VALUES(1,?,?,?,?,?,?)',
                ('same.png', digest(b'first bytes'), None, 5, 11, 1))
            db.execute('CREATE TABLE curation_tags(copy_id INTEGER,name TEXT,source TEXT,category TEXT)')
            db.execute("INSERT INTO curation_tags VALUES(1,'documented','manual','manual')")

    def test_legacy_v1_is_readable_and_explicit_export_can_upgrade_it(self):
        self.legacy_sidecar()
        old = read_curation_page(self.folder)['copies'][0]
        self.assertEqual(old['is_offline'], 0)
        self.assertEqual(old['tags'][0]['name'], 'documented')
        self.action('archive_copy')
        export_curation(self.service.db_path, self.folder)
        current = read_curation_page(self.folder)['copies'][0]
        self.assertEqual(current['is_offline'], 1)
        self.assertEqual(current['tags'], old['tags'])

    def test_legacy_online_export_detects_central_offline_history(self):
        self.legacy_sidecar()
        self.action('archive_copy')
        report = compare_curation_page(self.service.db_path, self.folder)['reports'][0]
        self.assertEqual(report['status'], 'offline_history')

    def test_downgraded_marker_cannot_hide_exported_offline_state(self):
        self.action('archive_copy')
        export_curation(self.service.db_path, self.folder)
        path = self.folder / NAME
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE meta SET value='1' WHERE key='panopticon_curation_version'")
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'foreign_or_unsupported_sidecar'):
            read_curation_page(self.folder)
        with self.assertRaisesRegex(ValueError, 'foreign_or_unsupported_sidecar'):
            export_curation(self.service.db_path, self.folder)
        self.assertEqual(path.read_bytes(), before)

    def test_removal_keeps_history_and_does_not_reactivate_on_reindex(self):
        self.db.remove_files([str(self.image)])
        self.assertEqual(self.state()['state']['file']['is_offline'], 1)
        self.assertEqual(self.state()['state']['tags'][0]['name'], 'documented')
        self.db.register_files_minimal([dict(path=str(self.image), filename='same.png', size=11, created=2)])
        self.assertIsNone(self.db.get_file_id(str(self.image)))

    def test_watch_removal_archives_only_its_folder_with_curated_history(self):
        sibling = self.root / (self.folder.name + '-other') / 'other.png'
        self.db.register_files_minimal([dict(path=str(sibling), filename='other.png', size=1, created=1)])
        self.db.add_watched_folder(str(self.folder))
        self.db.remove_watched_folder(str(self.folder))
        self.assertEqual(self.state()['state']['file']['is_offline'], 1)
        self.assertIsNotNone(self.db.get_file_id(str(sibling)))
        self.assertTrue(self.db.check_fts()['consistent'])


class Schema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'legacy # ñ.db'
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(self.conn.close)
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.execute('CREATE TABLE files(id INTEGER PRIMARY KEY AUTOINCREMENT,path TEXT UNIQUE NOT NULL,'
            'filename TEXT,file_size INTEGER DEFAULT 0,width INTEGER DEFAULT 0,height INTEGER DEFAULT 0,'
            'created_date TEXT,meta_tool TEXT,meta_model TEXT,meta_positive TEXT,meta_negative TEXT,meta_seed TEXT,'
            'rating INTEGER DEFAULT 0,hash_original TEXT,hash_mod TEXT,phash TEXT,origin_url TEXT,origin_site TEXT,artist TEXT,'
            'extra BLOB)')
        repo = LibraryRepository(self.conn)
        repo.create_schema()
        repo.run_transformations()
        self.conn.execute("CREATE TABLE panopticon_schema(name TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.conn.execute("INSERT INTO panopticon_schema VALUES('integrity_v1','1')")
        self.conn.execute("INSERT INTO files(id,path,filename,rating,meta_positive,extra) VALUES(9,'same.png','same.png',5,'prompt',X'0001FF')")
        self.conn.execute("INSERT INTO tags(id,name,category) VALUES(3,'documented','manual')")
        self.conn.execute("INSERT INTO file_tags VALUES(9,3,'manual')")
        repo.repair_fts()
        self.conn.execute("UPDATE sqlite_sequence SET seq=99 WHERE name='files'")

    def snapshot(self):
        return {name: self.conn.execute(f'SELECT * FROM {name}').fetchall() for name in
                ('files', 'tags', 'file_tags', 'files_fts', 'panopticon_schema')}

    def test_migration_preserves_rows_ids_links_blob_fts_and_high_sequence(self):
        before = self.snapshot()
        backup = migrate(self.conn, self.path)
        self.assertTrue(backup.is_file())
        self.assertEqual(self.conn.execute('SELECT * FROM files').fetchall(), [(*row, 0) for row in before['files']])
        for name in ('tags', 'file_tags', 'files_fts'):
            self.assertEqual(self.conn.execute(f'SELECT * FROM {name}').fetchall(), before[name])
        self.assertEqual(self.conn.execute("SELECT seq FROM sqlite_sequence WHERE name='files'").fetchone(), (99,))
        self.conn.execute("INSERT INTO files(path,filename) VALUES('next.png','next.png')")
        self.assertEqual(self.conn.execute("SELECT id FROM files WHERE path='next.png'").fetchone(), (100,))
        with closing(sqlite3.connect(backup)) as original:
            self.assertEqual(original.execute('SELECT * FROM files').fetchall(), before['files'])
            self.assertFalse(any(row[1] == 'is_offline' for row in original.execute('PRAGMA table_info(files)')))
        self.assertEqual(self.conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_partial_unique_path_allows_versions_but_only_one_online(self):
        migrate(self.conn, self.path)
        self.conn.execute('UPDATE files SET is_offline=1 WHERE id=9')
        self.conn.execute("INSERT INTO files(path,filename) VALUES('same.png','new.png')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO files(path,filename) VALUES('same.png','collision.png')")
        self.assertEqual(self.conn.execute('SELECT count(*) FROM files').fetchone(), (2,))

    def test_empty_legacy_table_keeps_deleted_id_high_watermark(self):
        self.conn.execute('DELETE FROM files')
        self.conn.execute('DELETE FROM files_fts')
        migrate(self.conn, self.path)
        self.assertEqual(self.conn.execute("SELECT seq FROM sqlite_sequence WHERE name='files'").fetchone(), (99,))
        self.conn.execute("INSERT INTO files(path) VALUES('first-after-empty.png')")
        self.assertEqual(self.conn.execute('SELECT id FROM files').fetchone(), (100,))

    def test_indexes_and_triggers_survive_without_triggering_copy(self):
        self.conn.execute('CREATE TABLE changes(id INTEGER)')
        self.conn.execute('CREATE INDEX custom_rating ON files(rating)')
        self.conn.execute('CREATE TRIGGER audit_rating AFTER UPDATE OF rating ON files BEGIN INSERT INTO changes VALUES(new.id); END')
        migrate(self.conn, self.path)
        self.assertEqual(self.conn.execute('SELECT * FROM changes').fetchall(), [])
        self.conn.execute('UPDATE files SET rating=4 WHERE id=9')
        self.assertEqual(self.conn.execute('SELECT * FROM changes').fetchall(), [(9,)])
        self.assertTrue(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='custom_rating'").fetchone())

    def test_repeat_migration_is_noop_without_another_backup(self):
        migrate(self.conn, self.path)
        trace = []
        self.conn.set_trace_callback(trace.append)
        self.assertIsNone(migrate(self.conn, self.path))
        self.assertEqual(len(list(self.root.glob('*.bak'))), 1)
        self.assertFalse(any(text.startswith(('INSERT', 'UPDATE', 'DELETE', 'BEGIN')) for text in trace))

    def test_failure_after_reconstruction_rolls_back_entire_schema(self):
        before = self.snapshot()
        with patch('core.library_versions_schema.initialize_new', side_effect=sqlite3.OperationalError('fixture')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'fixture'):
                migrate(self.conn, self.path)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.conn.execute('PRAGMA foreign_keys').fetchone(), (1,))
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT seq FROM sqlite_sequence WHERE name='files'").fetchone(), (99,))

    def test_extra_unique_path_index_requires_review_without_mutation(self):
        self.conn.execute('CREATE UNIQUE INDEX custom_path ON files(path)')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'unsupported_files_path_index'):
            migrate(self.conn, self.path)
        self.assertEqual(self.snapshot(), before)

    def test_view_requires_review_without_mutation(self):
        self.conn.execute('CREATE VIEW external_files AS SELECT * FROM files')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'requires_view_review'):
            migrate(self.conn, self.path)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.conn.execute('SELECT id FROM external_files').fetchall(), [(9,)])

    def test_unknown_version_marker_refuses_without_backup(self):
        self.conn.execute('INSERT INTO panopticon_schema VALUES(?,?)', (MARKER, '2'))
        with self.assertRaisesRegex(ValueError, 'unsupported_copy_versions_schema'):
            migrate(self.conn, self.path)
        self.assertEqual(list(self.root.glob('*.bak')), [])

    def test_foreign_key_failure_preserves_original_schema(self):
        self.conn.execute('PRAGMA foreign_keys=OFF')
        self.conn.execute("INSERT INTO file_tags VALUES(999,3,'orphan')")
        self.conn.execute('PRAGMA foreign_keys=ON')
        before = self.snapshot()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'foreign_key_check_failed'):
            migrate(self.conn, self.path)
        self.assertEqual(self.snapshot(), before)

    def test_strict_owner_refuses_old_schema_without_migration(self):
        before = self.snapshot()
        self.conn.execute('PRAGMA journal_mode=WAL')
        with self.assertRaisesRegex(LibraryError, 'library_not_ready'):
            LibraryService(self.path, require_ready=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.root.glob('*.bak')), [])

    def test_regular_owner_migrates_legacy_and_reopens_without_backup_growth(self):
        service = LibraryService(self.path)
        service.close()
        self.assertEqual(self.conn.execute('SELECT is_offline FROM files').fetchall(), [(0,)])
        service = LibraryService(self.path, require_ready=True)
        service.close()
        self.assertEqual(len(list(self.root.glob('*.bak'))), 1)

    def test_initial_integrity_upgrade_takes_only_one_backup(self):
        self.conn.execute('DELETE FROM panopticon_schema')
        service = LibraryService(self.path)
        self.addCleanup(service.close)
        self.assertEqual(service.read('get_file_id', 'same.png'), 9)
        self.assertEqual(len(list(self.root.glob('*.bak'))), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
