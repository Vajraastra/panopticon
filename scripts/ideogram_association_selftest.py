"""Private Ideogram association/export; no model, network or real collection."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image
from PySide6.QtWidgets import QApplication
from modules.dataset_tagger.logic.ideogram.datamodel import WorkDoc
from modules.dataset_tagger.logic.ideogram.ig4_worker import Ig4Worker
from modules.dataset_tagger.logic.ideogram import palette, layout
from modules.dataset_tagger.logic.ideogram import autobox, editor_load, editor_save
from modules.dataset_tagger.logic.ideogram.draft_publication import draft_guard


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class IdeogramAssociation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'a.png'
        Image.new('RGB', (24, 20), 'red').save(self.source)
        self.out = self.root / 'output'
        self.out.mkdir()
        self.pano = layout.pano_path(self.out, self.source)
        self.pano.parent.mkdir()
        self.provider = Mock()
        self.provider.caption.return_value = 'captured scene'
        doc = WorkDoc(image='a.png', image_w=24, image_h=20, background='background')
        doc.style.art_style = 'digital illustration'
        doc.style.color_palette = ['#ff0000']
        doc.save(self.pano)
        self.data = json.loads(self.pano.read_text())
        self.data['source_sha256'] = sha(self.source)
        self.pano.write_text(json.dumps(self.data))
        self.pal = patch.object(palette, 'extract_palette', return_value=['#ff0000'])
        self.pal.start()
        self.addCleanup(self.pal.stop)

    def worker(self, **options):
        return Ig4Worker(self.provider, 'mock', [(str(self.source), str(self.pano))],
                         prompt_hld='scene', prompt_background='background', prompt_obj='object', **options)

    def run_worker(self, worker=None):
        worker = worker or self.worker()
        errors, done, terminal = [], [], []
        worker.error.connect(lambda *args: errors.append(args))
        worker.image_done.connect(lambda *args: done.append(args))
        worker.finished_all.connect(lambda *args: terminal.append(args))
        worker.run()
        return errors, done, terminal

    def test_unowned_existing_composition_is_never_used(self):
        Image.new('RGB', (24, 20), 'blue').save(self.out / 'a.png')
        before = (self.out / 'a.png').read_bytes()
        errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.provider.caption.assert_not_called()
        self.assertEqual((self.out / 'a.png').read_bytes(), before)

    def test_changed_source_same_name_and_dimensions_rejected(self):
        Image.new('RGB', (24, 20), 'blue').save(self.source)
        errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.provider.caption.assert_not_called()
        self.assertFalse((self.out / 'a.json').exists())

    def test_cancelled_provider_result_does_not_export(self):
        worker = self.worker()
        self.provider.caption.side_effect = lambda *a, **k: (worker.cancel() or 'stale')
        _, done, _ = self.run_worker(worker)
        self.assertFalse(done)
        self.assertFalse((self.out / 'a.json').exists())

    def test_shared_style_checks_ownership_before_first_provider_call(self):
        Image.new('RGB', (24, 20), 'blue').save(self.out / 'a.png')
        errors, done, _ = self.run_worker(self.worker(infer_style=True))
        self.assertTrue(errors)
        self.assertFalse(done)
        self.provider.caption.assert_not_called()

    def test_concurrent_json_edit_is_preserved(self):
        shutil.copy2(self.source, self.out / 'a.png')
        target = self.out / 'a.json'
        target.write_text('previous export')
        self.provider.caption.side_effect = lambda *a, **k: (target.write_text('user edit') and 'stale')
        errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertEqual(target.read_text(), 'user edit')

    def update_doc(self, **changes):
        self.data.update(changes)
        self.pano.write_text(json.dumps(self.data))

    def test_plain_export_preserves_source_and_records_pair(self):
        before = self.source.read_bytes()
        errors, done, terminal = self.run_worker()
        self.assertFalse(errors)
        self.assertEqual(len(done), 1)
        self.assertEqual(terminal, [(1, 0)])
        self.assertEqual((self.out / 'a.png').read_bytes(), before)
        self.assertEqual(self.source.read_bytes(), before)
        doc = WorkDoc.load(self.pano)
        self.assertEqual(doc.status, 'exported')
        self.assertEqual(doc.source_sha256, sha(self.source))
        self.assertEqual(doc.output_sha256, sha(self.out / 'a.png'))
        self.assertIsInstance(json.loads((self.out / 'a.json').read_text()), dict)
        self.assertFalse(list(self.out.rglob('*.guard')))

    def test_verified_composition_is_provider_reference(self):
        output = self.out / 'a.png'
        Image.new('RGB', (24, 20), 'blue').save(output)
        self.update_doc(output_sha256=sha(output))
        before = output.read_bytes()
        errors, done, _ = self.run_worker()
        self.assertFalse(errors)
        self.assertEqual(len(done), 1)
        self.assertEqual(Path(self.provider.caption.call_args.args[0]).resolve(), output.resolve())
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(WorkDoc.load(self.pano).output_sha256, sha(output))

    def test_missing_claimed_composition_requires_review(self):
        self.update_doc(output_sha256='a' * 64)
        errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.provider.caption.assert_not_called()
        self.assertFalse((self.out / 'a.png').exists())

    def test_legacy_draft_is_preserved_until_review(self):
        self.update_doc(source_sha256=None)
        before = self.pano.read_bytes()
        errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.provider.caption.assert_not_called()
        self.assertEqual(self.pano.read_bytes(), before)

    def test_shared_style_rejects_inconsistent_dimensions(self):
        self.update_doc(image_w=25)
        errors, done, _ = self.run_worker(self.worker(infer_style=True))
        self.assertTrue(errors)
        self.assertFalse(done)
        self.provider.caption.assert_not_called()

    def test_concurrent_draft_edit_is_preserved(self):
        changed = json.dumps(dict(self.data, high_level_description='user edit'))
        self.provider.caption.side_effect = lambda *a, **k: (self.pano.write_text(changed) and 'stale')
        errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertEqual(self.pano.read_text(), changed)
        self.assertFalse((self.out / 'a.json').exists())

    def test_editor_rejects_changed_proven_source(self):
        Image.new('RGB', (24, 20), 'blue').save(self.source)
        with self.assertRaises(ValueError):
            editor_load.load(self.source, self.pano, {}, lambda: False)

    def test_bbox_save_records_composition_proof_and_preserves_it(self):
        value = editor_load.load(self.source, self.pano, {}, lambda: False)
        self.addCleanup(value['image'].close)
        composition = Image.new('RGB', (24, 20), 'blue')
        self.addCleanup(composition.close)
        args = [self.source, self.pano, self.out, value['doc'].to_dict(), composition, True,
                value['source_identity'], value['draft_identity'], value['draft_digest'],
                value['source_digest'], value['pinned_identity']]
        result = editor_save.save(*args, lambda: False)
        self.assertIsNone(result['error'], result)
        self.assertEqual(result['source_sha256'], sha(self.source))
        self.assertEqual(result['output_sha256'], sha(self.out / 'a.png'))
        value2 = editor_load.load(self.source, self.pano, {}, lambda: False)
        self.addCleanup(value2['image'].close)
        args2 = [self.source, self.pano, self.out, value2['doc'].to_dict(), value2['image'], False,
                 value2['source_identity'], value2['draft_identity'], value2['draft_digest'],
                 value2['source_digest'], value2['pinned_identity']]
        result2 = editor_save.save(*args2, lambda: False)
        self.assertIsNone(result2['error'], result2)
        self.assertEqual(result2['output_sha256'], result['output_sha256'])

    def run_autobox(self, detect=None):
        worker = autobox.AutoBoxBatchWorker([self.source], self.out,
                                           {'art_style': 'digital illustration'}, skip_existing=False)
        errors, done = [], []
        worker.failed.connect(errors.append)
        worker.finished_ok.connect(lambda *args: done.append(args))
        boxer = Mock()
        boxer.detect.return_value = []
        if detect is not None:
            boxer.detect.side_effect = lambda *a: detect(worker)
        with patch.object(autobox, 'AutoBoxer', return_value=boxer):
            worker.run()
        return errors, done, boxer

    def test_autobox_new_draft_has_proof_and_can_export(self):
        self.pano.unlink()
        errors, done, boxer = self.run_autobox()
        self.assertFalse(errors)
        self.assertEqual(done, [(1, 0)])
        boxer.detect.assert_called_once()
        self.assertEqual(WorkDoc.load(self.pano).source_sha256, sha(self.source))
        errors, done, _ = self.run_worker()
        self.assertFalse(errors)
        self.assertEqual(len(done), 1)

    def test_autobox_rejects_unowned_output_before_detect(self):
        Image.new('RGB', (24, 20), 'blue').save(self.out / 'a.png')
        before = self.pano.read_bytes()
        errors, done, boxer = self.run_autobox()
        self.assertTrue(errors)
        self.assertFalse(done)
        boxer.detect.assert_not_called()
        self.assertEqual(self.pano.read_bytes(), before)

    def test_autobox_cancel_after_detect_preserves_draft(self):
        before = self.pano.read_bytes()
        errors, done, _ = self.run_autobox(lambda worker: (worker.cancel() or []))
        self.assertFalse(errors)
        self.assertEqual(done, [(0, 0)])
        self.assertEqual(self.pano.read_bytes(), before)

    def test_active_editor_guard_blocks_export_without_removing_guard(self):
        with draft_guard(self.pano):
            errors, done, _ = self.run_worker()
            self.assertTrue(errors)
            self.assertFalse(done)
            self.assertTrue(self.pano.with_name(self.pano.name + '.bbox.guard').exists())
        self.provider.caption.assert_not_called()

    def test_invalid_provenance_cannot_load(self):
        self.update_doc(source_sha256='invalid')
        with self.assertRaises(ValueError):
            WorkDoc.load(self.pano)

    def test_json_write_failure_preserves_existing_export(self):
        shutil.copy2(self.source, self.out / 'a.png')
        target = self.out / 'a.json'
        target.write_text('previous export')
        before = self.pano.read_bytes()
        real = os.replace
        def fail(source, destination):
            if Path(destination).resolve() == target.resolve():
                raise OSError('private disk failure')
            return real(source, destination)
        with patch.object(os, 'replace', side_effect=fail):
            errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertEqual(target.read_text(), 'previous export')
        self.assertEqual(self.pano.read_bytes(), before)
        self.assertFalse(list(self.out.rglob('*.guard')))

    def test_draft_write_failure_keeps_published_pair_for_review(self):
        before = self.pano.read_bytes()
        real = os.replace
        def fail(source, destination):
            if Path(destination).resolve() == self.pano.resolve():
                raise OSError('private draft disk failure')
            return real(source, destination)
        with patch.object(os, 'replace', side_effect=fail):
            errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertEqual(self.pano.read_bytes(), before)
        self.assertTrue((self.out / 'a.png').exists())
        self.assertTrue((self.out / 'a.json').exists())
        guard = self.out / 'a.json.caption.guard'
        self.assertEqual(json.loads(guard.read_text())['phase'], 'review_required')
        self.assertFalse(list(self.out.rglob('*.tmp')))
        self.assertFalse(self.pano.with_name(self.pano.name + '.bbox.guard').exists())

    def test_autobox_concurrent_draft_edit_is_preserved(self):
        changed = json.dumps(dict(self.data, high_level_description='user edit'))
        errors, done, _ = self.run_autobox(lambda worker: (self.pano.write_text(changed) and []))
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertEqual(self.pano.read_text(), changed)


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main()
