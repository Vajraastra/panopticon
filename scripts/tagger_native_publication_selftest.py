"""Tagger end-to-end with mock provider, private files and private Native owner."""
from concurrent.futures import Future
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image
from PySide6.QtWidgets import QApplication
from core.database_manager import DatabaseManager
from core.library_service import LibraryService
from modules.dataset_tagger.logic.caption_worker import CaptionWorker
from modules.dataset_tagger.logic import sidecar


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class NativePublication(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source' / 'a.png'
        self.source.parent.mkdir()
        Image.new('RGB', (24, 20), 'red').save(self.source)
        self.service = LibraryService(self.root / 'private.db')
        self.addCleanup(self.service.close)
        self.db = DatabaseManager(service=self.service)
        self.db.register_files_minimal([dict(path=str(self.source), filename='a.png',
            size=self.source.stat().st_size, created=self.source.stat().st_ctime,
            hash_original=sha(self.source), origin_url='private download receipt', artist='Artist')])
        self.db.edit_curation(str(self.source), add_tags=['curated'], rating=5)
        self.provider = Mock()
        self.provider.caption.return_value = 'caption'
        self.out = sidecar.output_dir(self.source.parent, 'mock', 'natural')
        self.image = self.out / 'a.png'
        self.caption = self.out / 'a.txt'
        self.journals = self.root / 'recovery'

    def worker(self):
        return CaptionWorker(self.provider, 'mock', str(self.source.parent), ['flux'],
            images=[str(self.source)], policy=sidecar.OVERWRITE,
            native_service=self.service, journal_root=self.journals)

    def run_worker(self, worker=None):
        worker = worker or self.worker()
        errors, done, terminal = [], [], []
        worker.error.connect(lambda *a: errors.append(a))
        worker.image_done.connect(lambda *a: done.append(a))
        worker.finished_all.connect(lambda *a: terminal.append(a))
        worker.run()
        return errors, done, terminal

    def test_native_copy_and_caption_complete_with_independent_curation(self):
        key = self.db.get_file_id(str(self.source))
        before = self.service.read('get_copy_version_state', key)
        errors, done, terminal = self.run_worker()
        self.assertFalse(errors, errors)
        self.assertEqual(len(done), 1)
        self.assertEqual(terminal, [(1, 0)])
        self.assertEqual(sha(self.image), sha(self.source))
        copy = self.db.get_file_id(str(self.image))
        self.assertNotEqual(copy, key)
        state = self.service.read('get_copy_version_state', copy)['state']
        self.assertEqual(state['file']['rating'], 5)
        self.assertEqual(state['file']['origin_url'], 'private download receipt')
        self.assertEqual(state['tags'], before['state']['tags'])
        self.assertEqual(self.service.read('get_copy_version_state', key), before)
        journal = json.loads(next(self.journals.glob('*/journal.json')).read_text())
        self.assertEqual(journal['phase'], 'complete')
        self.assertEqual(journal['native_confirmation'], 'confirmed')
        self.assertFalse(list(self.out.glob('*.guard')))
        self.assertTrue(self.db.check_fts()['consistent'])

    def test_repeat_caption_keeps_existing_native_id_and_current_curation(self):
        self.assertFalse(self.run_worker()[0])
        key = self.db.get_file_id(str(self.image))
        self.db.edit_curation(str(self.image), add_tags=['independent'], rating=2)
        before = self.service.read('get_copy_version_state', key)
        self.provider.caption.return_value = 'second caption'
        errors, done, _ = self.run_worker()
        self.assertFalse(errors, errors)
        self.assertEqual(len(done), 1)
        self.assertEqual(self.caption.read_text(), 'second caption')
        self.assertEqual(self.db.get_file_id(str(self.image)), key)
        self.assertEqual(self.service.read('get_copy_version_state', key), before)
        self.assertEqual(len(list(self.journals.glob('*/journal.json'))), 1)

    def test_changed_source_curation_during_provider_stops_before_publication(self):
        def changed(*args, **kwargs):
            self.db.edit_curation(str(self.source), rating=1)
            return 'caption'
        self.provider.caption.side_effect = changed
        errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertTrue(any('changed_since_review' in error[1] for error in errors), errors)
        self.assertFalse(done)
        self.assertFalse(self.image.exists())
        self.assertFalse(self.caption.exists())

    def test_registration_failure_keeps_pair_and_journal_for_review(self):
        real = self.service.submit
        def failed(operation, *args, **kwargs):
            if operation == 'register_derived_copy':
                future = Future()
                future.set_exception(OSError('private Native failure'))
                return future
            return real(operation, *args, **kwargs)
        with patch.object(self.service, 'submit', side_effect=failed):
            errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertTrue(self.image.exists())
        self.assertTrue(self.caption.exists())
        self.assertTrue((self.out / 'a.txt.caption.guard').exists())
        self.assertIsNone(self.db.get_file_id(str(self.image)))
        journal = json.loads(next(self.journals.glob('*/journal.json')).read_text())
        self.assertEqual(journal['phase'], 'review_required')
        self.assertTrue(journal['published'])

    def test_lost_acknowledgement_confirms_receipt_without_double_registration(self):
        real = self.service.submit
        calls = []
        def lost(operation, *args, **kwargs):
            value = real(operation, *args, **kwargs)
            if operation == 'register_derived_copy':
                self.assertTrue(value.result(3))
                calls.append(operation)
                future = Future()
                future.set_exception(OSError('private lost acknowledgement'))
                return future
            return value
        with patch.object(self.service, 'submit', side_effect=lost):
            errors, done, _ = self.run_worker()
        self.assertFalse(errors, errors)
        self.assertEqual(len(done), 1)
        self.assertEqual(calls, ['register_derived_copy'])
        self.assertIsNotNone(self.db.get_file_id(str(self.image)))

    def test_cancellation_before_write_does_not_register_or_publish(self):
        worker = self.worker()
        self.provider.caption.side_effect = lambda *a, **k: (worker.cancel() or 'stale caption')
        errors, done, _ = self.run_worker(worker)
        self.assertFalse(errors)
        self.assertFalse(done)
        self.assertFalse(self.image.exists())
        self.assertIsNone(self.db.get_file_id(str(self.image)))

    def test_late_cancellation_finishes_native_registration(self):
        worker = self.worker()
        real = os.rename
        def cancel_after_image(source, destination):
            value = real(source, destination)
            if Path(destination).resolve() == self.image.resolve():
                worker.cancel()
            return value
        with patch.object(os, 'rename', side_effect=cancel_after_image):
            errors, done, terminal = self.run_worker(worker)
        self.assertFalse(errors, errors)
        self.assertEqual(len(done), 1)
        self.assertEqual(terminal, [(1, 0)])
        self.assertIsNotNone(self.db.get_file_id(str(self.image)))

    def test_late_cancellation_never_hides_registration_failure(self):
        worker = self.worker()
        real = os.rename
        def cancel_and_change(source, destination):
            value = real(source, destination)
            if Path(destination).resolve() == self.image.resolve():
                worker.cancel()
                self.db.edit_curation(str(self.source), rating=1)
            return value
        with patch.object(os, 'rename', side_effect=cancel_and_change):
            errors, done, _ = self.run_worker(worker)
        self.assertTrue(errors)
        self.assertTrue(any('changed_since_review' in error[1] for error in errors), errors)
        self.assertFalse(done)
        self.assertTrue(self.image.exists())
        self.assertTrue(self.caption.exists())
        self.assertIsNone(self.db.get_file_id(str(self.image)))
        self.assertTrue((self.out / 'a.txt.caption.guard').exists())

    def test_external_source_exports_without_creating_native_identity_or_schema(self):
        external = self.source.parent / 'external.png'
        Image.new('RGB', (24, 20), 'blue').save(external)
        worker = self.worker()
        worker._images = [str(external)]
        errors, done, _ = self.run_worker(worker)
        self.assertFalse(errors, errors)
        self.assertEqual(len(done), 1)
        output = self.out / 'external.png'
        self.assertTrue(output.exists())
        self.assertIsNone(self.db.get_file_id(str(output)))
        self.assertEqual(self.service.read('get_derived_copies_status'), {'prepared': False})
        self.assertFalse(self.journals.exists())

    def test_native_destination_history_blocks_new_publication(self):
        self.out.mkdir()
        self.db.register_files_minimal([dict(path=str(self.image), filename='a.png',
            size=self.source.stat().st_size, created=1, hash_original=sha(self.source))])
        key = self.db.get_file_id(str(self.image))
        before = self.service.read('get_copy_version_state', key)
        errors, done, _ = self.run_worker()
        self.assertTrue(any('destination_occupied' in error[1] for error in errors), errors)
        self.assertFalse(done)
        self.assertFalse(self.image.exists())
        self.assertEqual(self.service.read('get_copy_version_state', key), before)

    def test_multiple_images_prepare_schema_once_and_preserve_fts(self):
        other = self.source.parent / 'b.png'
        Image.new('RGB', (24, 20), 'blue').save(other)
        self.db.register_files_minimal([dict(path=str(other), filename='b.png', size=other.stat().st_size,
                                            created=1, hash_original=sha(other))])
        worker = self.worker()
        worker._images = [str(self.source), str(other)]
        with patch.object(self.service, 'submit', wraps=self.service.submit) as writes:
            errors, done, _ = self.run_worker(worker)
        self.assertFalse(errors, errors)
        self.assertEqual(len(done), 2)
        self.assertEqual(sum(call.args[0] == 'prepare_derived_copies' for call in writes.call_args_list), 1)
        self.assertTrue(self.db.check_fts()['consistent'])


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main()
