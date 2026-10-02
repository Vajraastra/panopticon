"""Ideogram publication against private Native; provider mocked, no real models."""
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
from core.metadata.bundle import MetadataBundle
from core.metadata.stamper import MetadataStamper
from modules.dataset_tagger.logic.ideogram import editor_load, editor_save, layout, palette
from modules.dataset_tagger.logic.ideogram.datamodel import WorkDoc
from modules.dataset_tagger.logic.ideogram.ig4_worker import Ig4Worker


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class IdeogramNative(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source' / 'a.png'
        self.source.parent.mkdir()
        with Image.new('RGB', (24, 20), 'red') as image:
            image.save(self.source)
        self.bundle = MetadataBundle(positive_prompt='original prompt', negative_prompt='bad quality',
                                     tool='A1111', model='fixture-model', seed='123')
        self.assertTrue(MetadataStamper.stamp(self.source, self.bundle, prepared=True))
        self.service = LibraryService(self.root / 'private.db')
        self.addCleanup(self.service.close)
        self.db = DatabaseManager(service=self.service)
        self.register(self.source)
        self.db.edit_curation(str(self.source), add_tags=['curated'], rating=5)
        self.source_id = self.db.get_file_id(str(self.source))
        self.out = self.root / 'output'
        self.out.mkdir()
        self.image = self.out / self.source.name
        self.caption = self.image.with_suffix('.json')
        self.pano = self.draft(self.source)
        self.journals = self.root / 'recovery'
        self.provider = Mock()
        self.provider.caption.return_value = 'captured scene'
        self.addCleanup(patch.stopall)
        patch.object(palette, 'extract_palette', return_value=['#ff0000']).start()

    def register(self, path):
        self.db.register_files_minimal([dict(path=str(path), filename=path.name,
            size=path.stat().st_size, created=path.stat().st_ctime,
            hash_original=sha(path), origin_url='portable receipt', artist='Artist')])

    def draft(self, source):
        pano = layout.pano_path(self.out, source)
        pano.parent.mkdir(exist_ok=True)
        doc = WorkDoc(image=source.name, image_w=24, image_h=20, background='background')
        doc.style.art_style = 'digital illustration'
        doc.style.color_palette = ['#ff0000']
        doc.source_sha256 = sha(source)
        doc.save(pano)
        return pano

    def compose(self):
        value = editor_load.load(self.source, self.pano, {}, lambda: False)
        try:
            with Image.new('RGB', (24, 20), 'blue') as image:
                result = editor_save.save(self.source, self.pano, self.out,
                    value['doc'].to_dict(), image, True, value['source_identity'],
                    value['draft_identity'], value['draft_digest'], value['source_digest'],
                    value['pinned_identity'], lambda: False)
        finally:
            value['image'].close()
        self.assertIsNone(result['error'], result)
        return self.image.read_bytes()

    def worker(self, **kwargs):
        return Ig4Worker(self.provider, 'mock', [(str(self.source), str(self.pano))],
            prompt_hld='scene', prompt_background='background', prompt_obj='object',
            native_service=self.service, journal_root=self.journals, **kwargs)

    def run_worker(self, worker=None):
        worker = worker or self.worker()
        errors, done, terminal = [], [], []
        worker.error.connect(lambda *a: errors.append(a))
        worker.image_done.connect(lambda *a: done.append(a))
        worker.finished_all.connect(lambda *a: terminal.append(a))
        worker.run()
        return errors, done, terminal

    def journal(self):
        return json.loads(next(self.journals.glob('*/journal.json')).read_text())

    def test_modified_composition_registers_verified_generation_with_empty_curation(self):
        image_before = self.compose()
        source_before = self.source.read_bytes()
        before = self.service.read('get_copy_version_state', self.source_id)
        errors, done, terminal = self.run_worker()
        self.assertFalse(errors, errors)
        self.assertEqual(len(done), 1)
        self.assertEqual(terminal, [(1, 0)])
        copy = self.db.get_file_id(str(self.image))
        self.assertNotEqual(copy, self.source_id)
        state = self.service.read('get_copy_version_state', copy)['state']
        self.assertEqual(state['file']['rating'], 0)
        self.assertFalse(state['tags'])
        self.assertEqual(state['file']['hash_original'], sha(self.image))
        self.assertEqual(state['file']['meta_model'], 'fixture-model')
        self.assertEqual(state['file']['meta_positive'], 'original prompt')
        self.assertEqual(self.image.read_bytes(), image_before)
        self.assertEqual(self.source.read_bytes(), source_before)
        self.assertEqual(self.service.read('get_copy_version_state', self.source_id), before)
        journal = self.journal()
        self.assertEqual(journal['phase'], 'complete')
        self.assertEqual(journal['native_confirmation'], 'confirmed')
        self.assertEqual(journal['receipt']['producer'], 'ideogram4')
        self.assertEqual(journal['receipt']['result_sha256'], sha(self.image))
        self.assertEqual(journal['receipt']['source_sha256'], sha(self.source))
        self.assertEqual(WorkDoc.load(self.pano).status, 'exported')
        self.assertFalse(list(self.out.rglob('*.guard')))
        self.assertTrue(self.db.check_fts()['consistent'])

    def test_plain_copy_keeps_independent_initial_curation(self):
        errors, done, _ = self.run_worker()
        self.assertFalse(errors, errors)
        self.assertEqual(len(done), 1)
        copy = self.db.get_file_id(str(self.image))
        state = self.service.read('get_copy_version_state', copy)['state']
        before = self.service.read('get_copy_version_state', self.source_id)['state']
        self.assertEqual(state['file']['rating'], 5)
        self.assertEqual(state['tags'], before['tags'])
        self.assertEqual(state['file']['origin_url'], 'portable receipt')
        self.assertEqual(sha(self.image), sha(self.source))
        self.assertEqual(self.journal()['receipt']['producer'], 'dataset_tagger')
        self.db.edit_curation(str(self.image), rating=1)
        self.assertEqual(self.service.read('get_copy_version_state', self.source_id)['state']['file']['rating'], 5)

    def test_existing_output_keeps_id_and_later_curation(self):
        self.compose()
        self.assertFalse(self.run_worker()[0])
        copy = self.db.get_file_id(str(self.image))
        self.db.edit_curation(str(self.image), add_tags=['independent'], rating=3)
        before = self.service.read('get_copy_version_state', copy)
        errors, done, _ = self.run_worker(self.worker(recapture=True))
        self.assertFalse(errors, errors)
        self.assertEqual(len(done), 1)
        self.assertEqual(self.db.get_file_id(str(self.image)), copy)
        self.assertEqual(self.service.read('get_copy_version_state', copy), before)
        self.assertEqual(len(list(self.journals.glob('*/journal.json'))), 1)

    def test_source_revision_changed_during_provider_stops_before_export(self):
        image_before = self.compose()
        draft_before = self.pano.read_bytes()
        def changed(*a, **k):
            self.db.edit_curation(str(self.source), rating=1)
            return 'scene'
        self.provider.caption.side_effect = changed
        errors, done, _ = self.run_worker()
        self.assertTrue(any('changed_since_review' in a[1] for a in errors), errors)
        self.assertFalse(done)
        self.assertFalse(self.caption.exists())
        self.assertEqual(self.image.read_bytes(), image_before)
        self.assertEqual(self.pano.read_bytes(), draft_before)
        self.assertIsNone(self.db.get_file_id(str(self.image)))

    def test_shared_style_captures_source_revision_before_provider(self):
        def changed(*a, **k):
            self.db.edit_curation(str(self.source), rating=1)
            return 'art_style: painting'
        self.provider.caption.side_effect = changed
        errors, done, _ = self.run_worker(self.worker(infer_style=True))
        self.assertTrue(any('changed_since_style_review' in a[1] for a in errors), errors)
        self.assertEqual(self.provider.caption.call_count, 1)
        self.assertFalse(done)
        self.assertFalse(self.image.exists())
        self.assertFalse(self.caption.exists())

    def test_registration_failure_retains_caption_guard_and_journal(self):
        before = self.compose()
        real = self.service.submit
        def failed(operation, *args, **kwargs):
            if operation == 'register_derived_copy':
                future = Future()
                future.set_exception(OSError('private registration failed'))
                return future
            return real(operation, *args, **kwargs)
        with patch.object(self.service, 'submit', side_effect=failed):
            errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertTrue(self.caption.exists())
        self.assertTrue(self.caption.with_name(self.caption.name + '.caption.guard').exists())
        self.assertEqual(self.image.read_bytes(), before)
        self.assertIsNone(self.db.get_file_id(str(self.image)))
        self.assertEqual(self.journal()['phase'], 'review_required')
        self.assertTrue(self.journal()['published'])

    def test_lost_ack_confirms_receipt_without_resubmission(self):
        self.compose()
        real = self.service.submit
        calls = []
        def lost(operation, *args, **kwargs):
            value = real(operation, *args, **kwargs)
            if operation == 'register_derived_copy':
                self.assertTrue(value.result(3))
                calls.append(operation)
                value = Future()
                value.set_exception(OSError('private lost ack'))
            return value
        with patch.object(self.service, 'submit', side_effect=lost):
            errors, done, _ = self.run_worker()
        self.assertFalse(errors, errors)
        self.assertEqual(len(done), 1)
        self.assertEqual(calls, ['register_derived_copy'])
        self.assertIsNotNone(self.db.get_file_id(str(self.image)))

    def test_failed_draft_commit_keeps_pair_and_prepared_native_journal(self):
        self.compose()
        before = self.pano.read_bytes()
        real = WorkDoc.save
        def failed(doc, path):
            if Path(path).resolve() == self.pano.resolve():
                raise OSError('private locked draft')
            return real(doc, path)
        with patch.object(WorkDoc, 'save', autospec=True, side_effect=failed):
            errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertEqual(self.pano.read_bytes(), before)
        self.assertTrue(self.caption.exists())
        self.assertTrue(self.caption.with_name(self.caption.name + '.caption.guard').exists())
        self.assertEqual(self.journal()['phase'], 'review_required')
        self.assertIsNone(self.db.get_file_id(str(self.image)))

    def test_cancel_before_publication_preserves_existing_composition(self):
        before = self.compose()
        worker = self.worker()
        self.provider.caption.side_effect = lambda *a, **k: (worker.cancel() or 'stale')
        errors, done, _ = self.run_worker(worker)
        self.assertFalse(errors, errors)
        self.assertFalse(done)
        self.assertFalse(self.caption.exists())
        self.assertEqual(self.image.read_bytes(), before)
        self.assertIsNone(self.db.get_file_id(str(self.image)))

    def test_late_cancel_finishes_draft_and_registration(self):
        self.compose()
        worker = self.worker()
        real = os.rename
        def cancel(source, destination):
            result = real(source, destination)
            if Path(destination).resolve() == self.caption.resolve():
                worker.cancel()
            return result
        with patch.object(os, 'rename', side_effect=cancel):
            errors, done, terminal = self.run_worker(worker)
        self.assertFalse(errors, errors)
        self.assertEqual(len(done), 1)
        self.assertEqual(terminal, [(1, 0)])
        self.assertIsNotNone(self.db.get_file_id(str(self.image)))
        self.assertEqual(WorkDoc.load(self.pano).status, 'exported')
        self.assertEqual(self.journal()['phase'], 'complete')

    def test_late_cancel_does_not_hide_registration_conflict(self):
        self.compose()
        worker = self.worker()
        real = os.rename
        def change(source, destination):
            result = real(source, destination)
            if Path(destination).resolve() == self.caption.resolve():
                worker.cancel()
                self.db.edit_curation(str(self.source), rating=1)
            return result
        with patch.object(os, 'rename', side_effect=change):
            errors, done, _ = self.run_worker(worker)
        self.assertTrue(any('changed_since_review' in a[1] for a in errors), errors)
        self.assertFalse(done)
        self.assertTrue(self.caption.exists())
        self.assertIsNone(self.db.get_file_id(str(self.image)))
        self.assertEqual(self.journal()['phase'], 'review_required')

    def test_external_source_exports_without_native_schema_or_id(self):
        self.source = self.source.parent / 'external.png'
        with Image.new('RGB', (24, 20), 'red') as image:
            image.save(self.source)
        self.pano = self.draft(self.source)
        self.image = self.out / self.source.name
        self.caption = self.image.with_suffix('.json')
        self.compose()
        errors, done, _ = self.run_worker()
        self.assertFalse(errors, errors)
        self.assertEqual(len(done), 1)
        self.assertIsNone(self.db.get_file_id(str(self.image)))
        self.assertEqual(self.service.read('get_derived_copies_status'), {'prepared': False})
        self.assertFalse(self.journals.exists())

    def test_occupied_native_destination_is_rejected_without_overwriting_history(self):
        self.db.register_files_minimal([dict(path=str(self.image), filename=self.image.name,
            hash_original=sha(self.source), size=1, created=1)])
        copy = self.db.get_file_id(str(self.image))
        before = self.service.read('get_copy_version_state', copy)
        errors, done, _ = self.run_worker()
        self.assertTrue(any('destination_occupied' in a[1] for a in errors), errors)
        self.assertFalse(done)
        self.assertFalse(self.image.exists())
        self.assertFalse(self.caption.exists())
        self.assertEqual(self.service.read('get_copy_version_state', copy), before)

    def test_multiple_images_prepare_schema_once(self):
        other = self.source.parent / 'b.png'
        with Image.new('RGB', (24, 20), 'blue') as image:
            image.save(other)
        self.register(other)
        other_draft = self.draft(other)
        worker = self.worker()
        worker.jobs.append((str(other), str(other_draft)))
        with patch.object(self.service, 'submit', wraps=self.service.submit) as writes:
            errors, done, _ = self.run_worker(worker)
        self.assertFalse(errors, errors)
        self.assertEqual(len(done), 2)
        self.assertEqual(sum(a.args[0] == 'prepare_derived_copies' for a in writes.call_args_list), 1)
        self.assertTrue(self.db.check_fts()['consistent'])


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main()
