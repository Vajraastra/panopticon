"""Real codecs/process pool, disposable inputs, and injected write failures."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from modules.format_converter.logic import converter as c


class ConverterSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def image(self, relative, color):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with Image.new('RGB', (12, 12), color) as img:
            img.save(path)
        return path

    def test_same_names_distinct_images_real_processes_and_retry(self):
        paths = [self.image('a/01.png', 'red'), self.image('b/01.jpg', 'blue'),
                 self.image('c/01.PNG', 'green')]
        before = {p: p.read_bytes() for p in paths}
        result = c.convert_batch(paths, self.root / 'out', preserve_metadata=False)
        self.assertEqual(result.converted_count, 3, result.failed_files)
        self.assertEqual(len({r.output_path.casefold() for r in result.results}), 3)
        for r in result.results:
            with Image.open(r.source_path) as original, Image.open(r.output_path) as output:
                self.assertEqual(original.convert('RGB').tobytes(), output.convert('RGB').tobytes())
        self.assertEqual({p: p.read_bytes() for p in paths}, before)
        retry = c.convert_batch(paths[::-1], self.root / 'out', preserve_metadata=False)
        self.assertEqual(retry.skipped_count, 3)
        self.assertEqual(retry.failed_count, 0)

    def test_case_only_stems_are_unique(self):
        a = self.image('a/Name.png', 'red')
        b = self.image('b/name.jpg', 'blue')
        self.assertNotEqual(c._output_name(a, '.webp').casefold(), c._output_name(b, '.webp').casefold())

    def test_failed_save_keeps_previous_output(self):
        src = self.image('input.png', 'red')
        out = self.image('out.webp', 'blue')
        before = out.read_bytes()
        def partial(img, path, **kwargs):
            Path(path).write_bytes(b'broken')
            raise OSError('injected disk failure')
        with patch.object(Image.Image, 'save', partial):
            result = c.convert_single(src, out, preserve_metadata=False, overwrite=True)
        self.assertFalse(result.success)
        self.assertEqual(out.read_bytes(), before)
        self.assertFalse(list(self.root.glob('.convert-*')))

    def test_failed_metadata_does_not_publish(self):
        src = self.image('input.png', 'red')
        out = self.root / 'out.webp'
        class Bundle:
            def is_valid(self):
                return True
        with patch.object(c.MetadataExtractor, 'extract', return_value=Bundle()), \
             patch.object(c.MetadataStamper, 'stamp', return_value=False):
            result = c.convert_single(src, out)
        self.assertFalse(result.success)
        self.assertFalse(result.metadata_preserved)
        self.assertFalse(out.exists())
        self.assertFalse(list(self.root.glob('.convert-*')))

    def test_real_metadata_survives_temporary_publication(self):
        src = self.root / 'input.png'
        metadata = PngInfo()
        metadata.add_text('parameters', 'a green forest\nNegative prompt: blurry\nSteps: 20, Sampler: Euler, CFG scale: 7, Seed: 123')
        with Image.new('RGB', (12, 12), 'green') as img:
            img.save(src, pnginfo=metadata)
        before = c.MetadataExtractor.extract(src)
        self.assertTrue(before.is_valid())
        result = c.convert_single(src, self.root / 'out.webp')
        self.assertTrue(result.success, result.error)
        after = c.MetadataExtractor.extract(result.output_path)
        self.assertEqual(after.positive_prompt, before.positive_prompt)
        self.assertEqual(after.negative_prompt, before.negative_prompt)
        self.assertEqual(after.seed, before.seed)

    def test_source_cannot_be_overwritten_even_via_hardlink(self):
        src = self.image('input.png', 'red')
        before = src.read_bytes()
        alias = self.root / 'alias.webp'
        os.link(src, alias)
        for out in (src, alias):
            result = c.convert_single(src, out, preserve_metadata=False, overwrite=True)
            self.assertFalse(result.success)
        self.assertEqual(src.read_bytes(), before)

    def test_destination_appearing_during_conversion_survives(self):
        src = self.image('input.png', 'red')
        out = self.root / 'out.webp'
        save = Image.Image.save
        def race(img, path, **kwargs):
            out.write_bytes(b'other conversion')
            save(img, path, **kwargs)
        with patch.object(Image.Image, 'save', race):
            result = c.convert_single(src, out, preserve_metadata=False)
        self.assertFalse(result.success)
        self.assertEqual(out.read_bytes(), b'other conversion')


if __name__ == '__main__':
    unittest.main()
