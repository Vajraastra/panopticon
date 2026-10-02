"""Private captions: source association, cancellation and atomic publication."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import json
import shutil
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image
from PySide6.QtWidgets import QApplication
from modules.dataset_tagger.logic import sidecar
from modules.dataset_tagger.logic.caption_worker import CaptionWorker
from modules.dataset_tagger.logic.caption_publication import CaptionPair


class CaptionPublication(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.image('source/a.png', 'red')
        self.out = sidecar.output_dir(self.root, 'mock', 'natural')
        self.provider = Mock()
        self.provider.caption.return_value = 'correct caption'

    def image(self, name, color):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (24, 20), color).save(path)
        return path

    def run_worker(self, images=None, policy=sidecar.OVERWRITE):
        worker = CaptionWorker(self.provider, 'mock', str(self.root), ['flux'],
                               images=[str(p) for p in (images or [self.source])], policy=policy)
        errors, done, terminal = [], [], []
        worker.error.connect(lambda *args: errors.append(args))
        worker.image_done.connect(lambda *args: done.append(args))
        worker.finished_all.connect(lambda *args: terminal.append(args))
        worker.run()
        return worker, errors, done, terminal

    def test_existing_other_image_never_receives_source_caption(self):
        self.image(str(self.out.relative_to(self.root) / 'a.png'), 'blue')
        old = self.out / 'a.txt'
        old.write_text('caption for blue', encoding='utf-8')
        _, errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertEqual(old.read_text(), 'caption for blue')
        self.provider.caption.assert_not_called()

    def test_same_stem_different_extension_cannot_share_caption(self):
        other = self.image('other/a.jpg', 'blue')
        _, errors, done, terminal = self.run_worker([self.source, other])
        self.assertTrue(errors)
        self.assertEqual(len(done), 1)
        self.assertEqual(terminal, [(1, 1)])
        self.assertFalse((self.out / 'a.jpg').exists())

    def test_cancellation_after_provider_returns_never_publishes(self):
        worker = CaptionWorker(self.provider, 'mock', str(self.root), ['flux'], images=[str(self.source)])
        self.provider.caption.side_effect = lambda *a, **k: (worker.cancel() or 'obsolete caption')
        done = []
        worker.image_done.connect(lambda *args: done.append(args))
        worker.run()
        self.assertFalse(done)
        self.assertFalse((self.out / 'a.png').exists())
        self.assertFalse((self.out / 'a.txt').exists())

    def existing_pair(self, text='previous caption'):
        self.out.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.source, self.out / self.source.name)
        (self.out / 'a.txt').write_text(text, encoding='utf-8')

    def test_identical_image_reuses_bytes_and_overwrites_only_caption(self):
        self.existing_pair()
        data = (self.out / 'a.png').read_bytes()
        _, errors, done, terminal = self.run_worker()
        self.assertFalse(errors)
        self.assertEqual(len(done), 1)
        self.assertEqual(terminal, [(1, 0)])
        self.assertEqual((self.out / 'a.png').read_bytes(), data)
        self.assertIn('correct caption', (self.out / 'a.txt').read_text())

    def test_append_keeps_existing_caption_and_unicode(self):
        self.existing_pair('Ã¡rbol azul')
        _, errors, done, _ = self.run_worker(policy=sidecar.APPEND)
        self.assertFalse(errors)
        self.assertTrue(done)
        self.assertTrue((self.out / 'a.txt').read_text(encoding='utf-8').startswith('Ã¡rbol azul'))

    def test_skip_validates_pair_without_spending_provider(self):
        self.existing_pair()
        _, errors, done, terminal = self.run_worker(policy=sidecar.SKIP)
        self.assertFalse(errors)
        self.assertFalse(done)
        self.assertEqual(terminal, [(0, 1)])
        self.provider.caption.assert_not_called()

    def test_orphan_caption_is_preserved_and_never_skipped_as_success(self):
        self.out.mkdir()
        (self.out / 'a.txt').write_text('unassociated')
        _, errors, done, _ = self.run_worker(policy=sidecar.SKIP)
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertEqual((self.out / 'a.txt').read_text(), 'unassociated')
        self.provider.caption.assert_not_called()

    def test_concurrent_caption_edit_is_not_overwritten(self):
        self.existing_pair()
        self.provider.caption.side_effect = lambda *a, **k: ((self.out / 'a.txt').write_text('user edit') and 'stale result')
        _, errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertEqual((self.out / 'a.txt').read_text(), 'user edit')

    def test_concurrent_different_extension_is_not_given_shared_caption(self):
        self.provider.caption.side_effect = lambda *a, **k: (self.image(str(self.out.relative_to(self.root) / 'a.jpg'), 'blue') and 'stale')
        _, errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertFalse((self.out / 'a.png').exists())
        self.assertFalse((self.out / 'a.txt').exists())

    def test_source_pinned_during_provider_call(self):
        original = self.source.read_bytes()
        def caption(*a, **k):
            with self.assertRaises(PermissionError):
                self.source.write_bytes(b'changed by another writer')
            return 'caption from unchanged image'
        self.provider.caption.side_effect = caption
        _, errors, done, _ = self.run_worker()
        self.assertFalse(errors)
        self.assertTrue(done)
        self.assertEqual(self.source.read_bytes(), original)

    def test_existing_caption_survives_failed_atomic_replace(self):
        self.existing_pair()
        original = os.replace
        def replace(src, dst):
            if Path(dst).resolve() == (self.out / 'a.txt').resolve():
                raise OSError('fixture disk failure')
            return original(src, dst)
        with patch('os.replace', side_effect=replace):
            _, errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertEqual((self.out / 'a.txt').read_text(), 'previous caption')
        self.assertFalse(list(self.out.glob('*.caption.guard')))

    def test_new_image_partial_publication_keeps_review_guard(self):
        original = os.rename
        def rename(src, dst):
            if Path(dst).resolve() == (self.out / 'a.txt').resolve():
                raise OSError('fixture caption failure')
            return original(src, dst)
        with patch('os.rename', side_effect=rename):
            _, errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertEqual((self.out / 'a.png').read_bytes(), self.source.read_bytes())
        self.assertFalse((self.out / 'a.txt').exists())
        guard = self.out / 'a.txt.caption.guard'
        self.assertEqual(json.loads(guard.read_text())['phase'], 'review_required')
        self.provider.reset_mock()
        _, errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.provider.caption.assert_not_called()

    def test_new_destination_race_never_replaces_other_image(self):
        original = os.rename
        def rename(src, dst):
            if Path(dst).resolve() == (self.out / 'a.png').resolve():
                (self.out / 'a.png').write_bytes(b'competing output')
            return original(src, dst)
        with patch('os.rename', side_effect=rename):
            _, errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertEqual((self.out / 'a.png').read_bytes(), b'competing output')
        self.assertFalse((self.out / 'a.txt').exists())

    def test_late_cancellation_finishes_started_pair(self):
        worker = CaptionWorker(self.provider, 'mock', str(self.root), ['flux'], images=[str(self.source)])
        original = os.rename
        def rename(src, dst):
            result = original(src, dst)
            if Path(dst).resolve() == (self.out / 'a.png').resolve():
                worker.cancel()
            return result
        with patch('os.rename', side_effect=rename):
            worker.run()
        self.assertTrue((self.out / 'a.png').is_file())
        self.assertTrue((self.out / 'a.txt').is_file())
        self.assertFalse(list(self.out.glob('*.caption.guard')))

    def test_one_conflict_does_not_abort_other_images(self):
        self.image(str(self.out.relative_to(self.root) / 'a.png'), 'blue')
        other = self.image('other/b.png', 'green')
        _, errors, done, terminal = self.run_worker([self.source, other])
        self.assertTrue(errors)
        self.assertEqual(len(done), 1)
        self.assertEqual(terminal, [(1, 1)])
        self.assertTrue((self.out / 'b.txt').is_file())

    def test_success_preserves_image_bytes_and_cleans_temporaries(self):
        _, errors, done, _ = self.run_worker()
        self.assertFalse(errors)
        self.assertTrue(done)
        self.assertEqual((self.out / 'a.png').read_bytes(), self.source.read_bytes())
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ['a.png', 'a.txt'])

    def test_cherry_output_rejected_before_directory_creation(self):
        (self.root / 'catalog.db').write_bytes(b'fixture marker')
        _, errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertFalse(self.out.exists())
        self.provider.caption.assert_not_called()

    def test_output_folder_substitution_rejected(self):
        def caption(*a, **k):
            self.out.rename(self.out.with_name(self.out.name + '_moved'))
            self.out.mkdir()
            return 'stale result'
        self.provider.caption.side_effect = caption
        _, errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertFalse((self.out / 'a.txt').exists())

    def test_caption_failure_is_not_advertised_as_complete(self):
        with patch.object(CaptionPair, 'publish', side_effect=OSError('fixture failure')):
            _, errors, done, terminal = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.assertEqual(terminal, [(0, 1)])

    def test_literal_brackets_in_image_name_do_not_bypass_stem_check(self):
        self.source = self.image('source/[a].png', 'red')
        self.image(str(self.out.relative_to(self.root) / '[a].jpg'), 'blue')
        _, errors, done, _ = self.run_worker()
        self.assertTrue(errors)
        self.assertFalse(done)
        self.provider.caption.assert_not_called()


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main()
