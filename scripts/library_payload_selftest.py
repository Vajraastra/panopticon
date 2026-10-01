"""Admisión y curación acotadas; SQLite, archivos y threads privados."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import library_payload as payload
from core.library_repository import LibraryRepository
from core.library_service import LibraryError, LibraryService


class PayloadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='native-payload-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.service = LibraryService(self.root/'native.db', capacity=1)
        self.addCleanup(self.service.close)
        self.releases = []
        self.addCleanup(lambda: [event.set() for event in self.releases])
        self.file = (self.root/'ñ.png').as_posix()

    def observation(self, path=None):
        return dict(path=path or self.file, filename='ñ.png', size=1, created=1)

    def register(self):
        self.assertTrue(self.service.submit('register_files_minimal', [self.observation()]).result(3))

    def sql(self, statement, args=()):
        with closing(sqlite3.connect(self.root/'native.db')) as conn:
            rows = conn.execute(statement, args).fetchall()
            conn.commit()
            return rows

    def state(self):
        return [self.sql(statement) for statement in [
            'SELECT * FROM files', 'SELECT * FROM tags', 'SELECT * FROM file_tags',
            'SELECT rowid,* FROM files_fts ORDER BY rowid']]

    def hold(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = LibraryRepository.register_files_minimal
        def held(repo, *args, **kwargs):
            entered.set()
            if not release.wait(5): raise AssertionError('private fixture timeout')
            return original(repo, *args, **kwargs)
        return entered, release, patch.object(LibraryRepository, 'register_files_minimal', held)

    def rejected(self, value, reason='write_payload_too_large'):
        before = self.service.metrics_snapshot()
        with self.assertRaisesRegex(LibraryError, reason):
            self.service.submit('register_files_minimal', value).result(3)
        after = self.service.metrics_snapshot()
        self.assertEqual(after['accepted'], before['accepted'])
        self.assertEqual(after['begin_count'], before['begin_count'])
        self.assertEqual(after['rejected_payload'], before['rejected_payload']+1)
        self.assertEqual(self.sql('SELECT count(*) FROM files'), [(0,)])

    def test_simple_payload_detaches_mutable_containers(self):
        shared = {'tags': ['uno'], 'bytes': b'x', 'path': self.root}
        original = ([shared, shared], {1, 2}, frozenset({'x'}), (None, True, 4, 1.5))
        args, kwargs = payload.snapshot(original, {'data': shared})
        shared['tags'].append('later')
        self.assertEqual(args[0][0]['tags'], ['uno'])
        self.assertEqual(kwargs['data']['tags'], ['uno'])
        self.assertEqual(args[1:], original[1:])

    def test_oversized_field_rejects_without_writer_admission(self):
        self.rejected([dict(self.observation(), filename='x'*payload.MAX_FIELD_BYTES)])

    def test_oversized_bytes_rejects_without_writer_admission(self):
        self.rejected([dict(self.observation(), raw=b'x'*payload.MAX_FIELD_BYTES)])

    def test_aggregate_budget_counts_repeated_references(self):
        self.rejected(['x'*(512*1024)]*9)

    def test_container_limit_rejects_without_prefix_registration(self):
        self.rejected([self.observation()]*10001)

    def test_node_budget_rejects_small_but_excessive_structure(self):
        self.rejected([(0, 1, 2, 3, 4, 5, 6)]*10000)

    def test_depth_budget_rejects_before_recursion_overflow(self):
        value = []
        for _ in range(18): value = [value]
        self.rejected(value)

    def test_cycles_reject_without_deepcopy_or_recursion(self):
        value = []
        value.append(value)
        self.rejected(value, 'invalid_write_payload')

    def test_custom_payload_hooks_are_not_executed(self):
        class Unsafe:
            def __deepcopy__(self, memo): raise AssertionError('copy hook ran')
            def __fspath__(self): raise AssertionError('path hook ran')
            def __str__(self): raise AssertionError('str hook ran')
        self.rejected([Unsafe()], 'invalid_write_payload')

    def test_container_subclass_hooks_are_not_executed(self):
        class Unsafe(list):
            def __iter__(self): raise AssertionError('iteration hook ran')
        self.rejected(Unsafe(), 'invalid_write_payload')

    def test_queue_full_does_not_copy_or_charge_payload(self):
        entered, release, held = self.hold()
        with held:
            first = self.service.submit('register_files_minimal', [])
            self.assertTrue(entered.wait(3))
            second = self.service.submit('register_files_minimal', [])
            with patch('core.library_service.snapshot_write_payload', side_effect=AssertionError('unneeded copy')):
                with self.assertRaisesRegex(LibraryError, 'queue_full'):
                    self.service.submit('register_files_minimal', object()).result(3)
            release.set()
            self.assertTrue(first.result(3))
            self.assertTrue(second.result(3))
        self.assertEqual(self.service.metrics_snapshot()['rejected_full'], 1)
        self.assertEqual(self.service.metrics_snapshot()['rejected_payload'], 0)

    def test_closed_service_does_not_copy_payload(self):
        self.service.close()
        with patch('core.library_service.snapshot_write_payload', side_effect=AssertionError('unneeded copy')):
            with self.assertRaisesRegex(LibraryError, 'service_closed'):
                self.service.submit('register_files_minimal', object()).result(3)

    def test_close_during_snapshot_cannot_admit_late_request(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        result = []
        def held(args, kwargs):
            entered.set()
            if not release.wait(5): raise AssertionError('private timeout')
            return payload.snapshot(args, kwargs)
        with patch('core.library_service.snapshot_write_payload', held):
            thread = threading.Thread(target=lambda: result.append(
                self.service.submit('register_files_minimal', [self.observation()])))
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.service.close()
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        with self.assertRaisesRegex(LibraryError, 'service_closed'): result[0].result(3)
        self.assertEqual(self.sql('SELECT count(*) FROM files'), [(0,)])

    def test_queue_snapshot_survives_producer_changes(self):
        entered, release, held = self.hold()
        with held:
            first = self.service.submit('register_files_minimal', [])
            self.assertTrue(entered.wait(3))
            batch = [self.observation()]
            second = self.service.submit('register_files_minimal', batch)
            batch[0]['filename'] = 'changed'
            batch.append(self.observation((self.root/'late.png').as_posix()))
            release.set()
            self.assertTrue(first.result(3))
            self.assertTrue(second.result(3))
        self.assertEqual(self.sql('SELECT filename FROM files'), [('ñ.png',)])

    def test_valid_batch_after_rejection_is_not_poisoned(self):
        self.rejected(['x'*payload.MAX_FIELD_BYTES])
        self.register()
        self.assertEqual(self.sql('SELECT count(*) FROM files'), [(1,)])

    def test_payload_rejection_does_not_log_sensitive_fields(self):
        self.rejected(['secret-prompt'*payload.MAX_FIELD_BYTES])
        snapshot = self.service.metrics_snapshot()
        self.assertNotIn('secret-prompt', repr(snapshot))
        self.assertEqual(snapshot['rejected_payload'], 1)

    def fill_tags(self, count=256):
        self.register()
        self.assertTrue(self.service.submit('edit_curation', self.file,
            add_tags=['tag-'+str(i) for i in range(count)]).result(3))

    def test_legacy_add_tag_cannot_exceed_per_copy_capacity(self):
        self.fill_tags()
        before = self.state()
        with self.assertRaisesRegex(LibraryError, 'curation_tag_limit_exceeded'):
            self.service.submit('add_tag_to_file', self.file, 'overflow').result(3)
        self.assertEqual(self.state(), before)

    def test_delta_cannot_exceed_capacity_or_commit_rating_partially(self):
        self.fill_tags()
        before = self.state()
        with self.assertRaisesRegex(LibraryError, 'curation_tag_limit_exceeded'):
            self.service.submit('edit_curation', self.file, add_tags=['overflow'], rating=5).result(3)
        self.assertEqual(self.state(), before)

    def test_replacement_and_duplicate_at_capacity_do_not_grow(self):
        self.fill_tags()
        self.assertTrue(self.service.submit('add_tag_to_file', self.file, ' TAG-1 ').result(3))
        self.assertTrue(self.service.submit('edit_curation', self.file,
            add_tags=['replacement'], remove_tags=['tag-1']).result(3))
        self.assertEqual(len(self.service.read('get_tags_for_file', self.file)), 256)
        self.assertIn('replacement', self.service.read('get_tags_for_file', self.file))

    def test_oversized_tag_does_not_lazily_register_file(self):
        with self.assertRaisesRegex(LibraryError, 'invalid_tags'):
            self.service.submit('add_tag_to_file', self.file, 'x'*257,
                                file_data=self.observation()).result(3)
        self.assertEqual(self.sql('SELECT count(*) FROM files'), [(0,)])

    def test_unicode_lowercase_expansion_cannot_create_unrestorable_tag(self):
        self.register()
        before = self.state()
        for operation, args, kwargs in [
            ('add_tag_to_file', (self.file, 'İ'*256), {}),
            ('edit_curation', (self.file,), {'add_tags':['İ'*256]}),
        ]:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(LibraryError, 'invalid_tags'):
                    self.service.submit(operation, *args, **kwargs).result(3)
                self.assertEqual(self.state(), before)

    def test_oversized_legacy_curation_can_be_reduced_without_data_loss(self):
        self.fill_tags()
        self.sql("INSERT INTO tags(name,category) VALUES(?,'manual')", ('x'*300,))
        self.sql('INSERT INTO file_tags(file_id,tag_id,source) SELECT f.id,t.id,? FROM files f,tags t WHERE t.name=?',
                 ('legacy', 'x'*300))
        self.assertTrue(self.service.submit('update_file_rating', self.file, 4).result(3))
        self.assertTrue(self.service.submit('edit_curation', self.file, remove_tags=['tag-0']).result(3))
        self.assertTrue(self.service.submit('remove_tag_from_file', self.file, 'x'*300).result(3))
        self.assertEqual(len(self.service.read('get_tags_for_file', self.file)), 255)
        self.assertEqual(self.service.read('get_file_rating', self.file), 4)

    def test_serialized_producers_compete_for_last_tag_slot(self):
        self.fill_tags(255)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = LibraryRepository.add_tag_to_file
        def held(repo, path, name, **kwargs):
            if name == 'first':
                entered.set()
                if not release.wait(5): raise AssertionError('private timeout')
            return original(repo, path, name, **kwargs)
        with patch.object(LibraryRepository, 'add_tag_to_file', held):
            first = self.service.submit('add_tag_to_file', self.file, 'first')
            self.assertTrue(entered.wait(3))
            second = self.service.submit('add_tag_to_file', self.file, 'second')
            self.assertFalse(second.done())
            release.set()
            self.assertTrue(first.result(3))
            with self.assertRaisesRegex(LibraryError, 'curation_tag_limit_exceeded'):
                second.result(3)
        self.assertEqual(len(self.service.read('get_tags_for_file', self.file)), 256)


if __name__ == '__main__': unittest.main(verbosity=2)
