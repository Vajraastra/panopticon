"""Ordering evidence on private Native DBs; no priority policy or real library changes."""
from contextlib import contextmanager
import copy
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.curation_restore import FIELDS
from core.library_repository import LibraryRepository
from core.library_service import LibraryService


class Scheduling(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LibraryService(Path(self.temp.name) / 'private.db')
        self.addCleanup(self.service.close)
        self.paths = [(Path(self.temp.name) / name).as_posix()
                      for name in ('one.png', 'two.png')]
        self.service.submit('register_files_minimal', [
            dict(path=path, filename=Path(path).name, size=1, created=1,
                 hash_original='a' * 64) for path in self.paths]).result(3)
        for path in self.paths:
            self.service.submit('edit_curation', path,
                                add_tags=['old'], rating=3).result(3)

    def read(self, path=None):
        return self.service.read('get_curation_restore_state', path or self.paths[0])

    def reviewed_restore(self, snapshot):
        value = {field: copy.deepcopy(snapshot['state'][field]) for field in FIELDS}
        value.update(rating=5, tags=[dict(name='imported', source='sidecar', category=None)])
        return self.service.submit('restore_curation', self.paths[0],
                                   expected_fingerprint=snapshot['fingerprint'], incoming=value)

    @contextmanager
    def hold_writer(self):
        entered, release = threading.Event(), threading.Event()
        original = LibraryRepository.register_files_minimal

        def held(repo, observations):
            if not observations:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('private_writer_hold_timeout')
            return original(repo, observations)

        with patch.object(LibraryRepository, 'register_files_minimal', held):
            blocker = self.service.submit('register_files_minimal', [])
            try:
                self.assertTrue(entered.wait(3), 'private writer did not enter')
                yield
            finally:
                release.set()
                blocker.result(3)

    def test_fifo_keeps_accepted_review_then_interactive_edit(self):
        reviewed = self.read()
        with self.hold_writer():
            restored = self.reviewed_restore(reviewed)
            edited = self.service.submit('edit_curation', self.paths[0],
                                         add_tags=['ui'], expected_copy_id=reviewed['state']['id'])
            self.assertFalse(restored.done())
            self.assertFalse(edited.done())
        self.assertTrue(restored.result(3))
        self.assertTrue(edited.result(3))
        state = self.read()['state']
        self.assertEqual(state['rating'], 5)
        self.assertEqual([tag['name'] for tag in state['tags']], ['imported', 'ui'])
        self.assertEqual(state['tags'][0]['source'], 'sidecar')
        self.assertEqual(state['id'], reviewed['state']['id'])

    def test_simulated_overtaking_invalidates_previously_reviewed_restore(self):
        reviewed = self.read()
        # Submit the simulated execution order; production still uses FIFO.
        with self.hold_writer():
            edited = self.service.submit('edit_curation', self.paths[0], add_tags=['ui'])
            restored = self.reviewed_restore(reviewed)
        self.assertTrue(edited.result(3))
        with self.assertRaisesRegex(Exception, 'curation_changed_since_review'):
            restored.result(3)
        state = self.read()['state']
        self.assertEqual(state['rating'], 3)
        self.assertEqual([tag['name'] for tag in state['tags']], ['old', 'ui'])
        self.assertEqual(state['id'], reviewed['state']['id'])

    def test_same_copy_ratings_depend_on_execution_order(self):
        observed = []
        for order in ((1, 5), (5, 1)):
            with self.hold_writer():
                futures = [self.service.submit('edit_curation', self.paths[0], rating=rating)
                           for rating in order]
            for future in futures:
                self.assertTrue(future.result(3))
            observed.append(self.read()['state']['rating'])
        self.assertEqual(observed, [5, 1])

    def test_distinct_copy_ratings_have_same_final_state_in_both_orders(self):
        observed = []
        for order in ((0, 1), (1, 0)):
            for path in self.paths:
                self.service.submit('edit_curation', path, rating=3).result(3)
            with self.hold_writer():
                futures = [self.service.submit('edit_curation', self.paths[index], rating=index + 4)
                           for index in order]
            for future in futures:
                self.assertTrue(future.result(3))
            observed.append([self.read(path)['state'] for path in self.paths])
        self.assertEqual(observed[0], observed[1])
        self.assertEqual([state['rating'] for state in observed[0]], [4, 5])


if __name__ == '__main__':
    unittest.main(verbosity=2)
