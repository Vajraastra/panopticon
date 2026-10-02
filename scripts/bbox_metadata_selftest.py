"""Private Windows fixtures: composition metadata before publication and hashing."""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, PngImagePlugin
from core.metadata.bundle import MetadataBundle
from core.metadata.extractor import MetadataExtractor
from core.metadata.stamper import MetadataStamper
from core.metadata.output_stamp import payload
from modules.dataset_tagger.logic.ideogram import editor_load, editor_save, layout
from modules.dataset_tagger.logic.ideogram.datamodel import WorkDoc


class Metadata(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / 'output'
        self.images = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for image in self.images:
            image.close()
        self.temp.cleanup()

    def source(self, suffix='.png', metadata=True):
        self.original = self.root / ('source' + suffix)
        with Image.new('RGB', (120, 80), 'red') as image:
            image.save(self.original)
        if metadata:
            bundle = MetadataBundle(
                positive_prompt='persona, luz suave', negative_prompt='borroso',
                model='fixture-model', seed='37', steps='20', cfg='7',
                sampler='Euler', vae='fixture-vae', loras=['fixture-lora'],
                tool='A1111', tags=['curado'], rating=4, quality_score=80,
                raw={'workflow': '{"nodes":[]}', 'Source': 'fixture-source'},
            )
            self.assertTrue(MetadataStamper.stamp(self.original, bundle, prepared=True))
        self.pano = layout.pano_path(self.out, self.original)
        loaded = editor_load.load(self.original, self.pano, {}, lambda: False)
        image = Image.new('RGB', (120, 80), 'blue')
        self.images.extend([image, loaded['image']])
        self.args = [self.original, self.pano, self.out, loaded['doc'].to_dict(), image, True,
                     loaded['source_identity'], loaded['draft_identity'], loaded['draft_digest'],
                     loaded['source_digest'], loaded['pinned_identity']]
        self.before = self.original.read_bytes()

    def assert_unpublished(self, result):
        self.assertIsNone(result['published'], result)
        self.assertIsNone(result['draft_committed'], result)
        self.assertFalse(self.pano.exists())
        self.assertFalse((self.out / self.original.name).exists())
        self.assertEqual(self.original.read_bytes(), self.before)
        self.assertFalse(list(self.out.rglob('.panopticon-*')))
        self.assertFalse(list(self.out.rglob('*.guard')))

    def test_formats_preserve_bundle_and_hash_final_bytes_without_reencoding(self):
        for suffix in ('.png', '.jpg', '.jpeg', '.webp'):
            with self.subTest(suffix=suffix):
                self.source(suffix)
                expected = MetadataExtractor.extract(self.original, strict=True)
                real = MetadataStamper.stamp
                staged_pixels = []
                def stamp(path, bundle, **kwargs):
                    with Image.open(path) as image:
                        staged_pixels.append(image.tobytes())
                    self.assertTrue(kwargs['prepared'])
                    return real(path, bundle, **kwargs)
                with patch.object(MetadataStamper, 'stamp', side_effect=stamp):
                    result = editor_save.save(*self.args, lambda: False)
                self.assertIsNone(result['error'], result)
                output = Path(result['published'])
                self.assertTrue(MetadataStamper.verify_prepared(output, expected))
                actual = MetadataExtractor.extract(output, strict=True)
                self.assertEqual(payload(actual), payload(expected))
                with Image.open(output) as image:
                    self.assertEqual(image.tobytes(), staged_pixels[0])
                output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
                doc = WorkDoc.load(self.pano)
                self.assertEqual(doc.output_sha256, output_hash)
                self.assertEqual(result['output_sha256'], output_hash)
                self.assertEqual(doc.source_sha256, hashlib.sha256(self.before).hexdigest())
                self.assertEqual(self.original.read_bytes(), self.before)
                output.unlink()
                self.pano.unlink()

    def test_empty_source_metadata_is_valid_and_original_unchanged(self):
        self.source(metadata=False)
        result = editor_save.save(*self.args, lambda: False)
        self.assertIsNone(result['error'], result)
        expected = MetadataExtractor.extract(self.original, strict=True)
        self.assertTrue(MetadataStamper.verify_prepared(result['published'], expected))
        self.assertEqual(self.original.read_bytes(), self.before)

    def test_stamp_failure_does_not_publish_image_or_draft(self):
        self.source()
        with patch.object(MetadataStamper, 'stamp', return_value=False):
            result = editor_save.save(*self.args, lambda: False)
        self.assertIn('metadata_stamp_failed', result['error'])
        self.assert_unpublished(result)

    def test_corrupted_stamped_metadata_is_rejected_before_publication(self):
        self.source()
        def corrupt(path, bundle, **kwargs):
            with Image.open(path) as image:
                clean = image.copy()
            try:
                clean.save(path)
            finally:
                clean.close()
            return True
        with patch.object(MetadataStamper, 'stamp', side_effect=corrupt):
            result = editor_save.save(*self.args, lambda: False)
        self.assertIsNotNone(result['error'])
        self.assert_unpublished(result)

    def test_invalid_source_metadata_is_rejected_before_encode(self):
        self.source(metadata=False)
        info = PngImagePlugin.PngInfo()
        info.add_text('panopticon_data', '{invalid-json')
        with Image.new('RGB', (120, 80), 'red') as image:
            image.save(self.original, pnginfo=info)
        # Preview can display this image; capture its current proof for Save.
        loaded = editor_load.load(self.original, self.pano, {}, lambda: False)
        self.images.append(loaded['image'])
        self.args[6] = loaded['source_identity']
        self.args[9] = loaded['source_digest']
        self.args[10] = loaded['pinned_identity']
        self.before = self.original.read_bytes()
        with patch.object(Image.Image, 'save') as encode:
            result = editor_save.save(*self.args, lambda: False)
        encode.assert_not_called()
        self.assertIsNotNone(result['error'])
        self.assert_unpublished(result)

    def test_draft_only_does_not_stamp_or_edit_existing_composition(self):
        self.source()
        first = editor_save.save(*self.args, lambda: False)
        self.assertIsNone(first['error'], first)
        output = Path(first['published'])
        before = output.read_bytes()
        loaded = editor_load.load(self.original, self.pano, {}, lambda: False)
        self.images.append(loaded['image'])
        self.args[3] = loaded['doc'].to_dict()
        self.args[3]['global']['high_level_description'] = 'revised caption'
        self.args[5] = False
        self.args[6:11] = [loaded['source_identity'], loaded['draft_identity'],
                          loaded['draft_digest'], loaded['source_digest'], loaded['pinned_identity']]
        with patch.object(MetadataStamper, 'stamp') as stamp:
            result = editor_save.save(*self.args, lambda: False)
        self.assertIsNone(result['error'], result)
        stamp.assert_not_called()
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(self.original.read_bytes(), self.before)
        self.assertEqual(WorkDoc.load(self.pano).high_level_description, 'revised caption')

    def test_cancel_during_stamping_removes_staged_output(self):
        self.source()
        cancelled = False
        real = MetadataStamper.stamp
        def stamp(*args, **kwargs):
            nonlocal cancelled
            result = real(*args, **kwargs)
            cancelled = True
            return result
        with patch.object(MetadataStamper, 'stamp', side_effect=stamp):
            result = editor_save.save(*self.args, lambda: cancelled)
        self.assertTrue(result['cancelled'], result)
        self.assertIsNone(result['error'])
        self.assert_unpublished(result)


if __name__ == '__main__':
    unittest.main()
