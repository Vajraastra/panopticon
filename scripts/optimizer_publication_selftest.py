"""Salidas físicas/Native ocupadas, codec único y fallos sin publicar; fixtures privados."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, PngImagePlugin
from PySide6.QtCore import QRectF
from core.library_service import LibraryService
from core.metadata import MetadataExtractor, MetadataStamper
from modules.image_optimizer.logic import optimizer as logic
from modules.format_converter.logic.converter import convert_single
from modules.cropper.logic.cropper_logic import crop_image


class Publication(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.image('source.png')
        self.output = self.root/'output.png'

    def image(self, name, prompts=False):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        info = PngImagePlugin.PngInfo()
        if prompts: info.add_itxt('parameters', 'forest ñ\nNegative prompt: bad\nSteps: 20, Seed: 42')
        with Image.new('RGB', (23, 19), 'green') as image: image.save(path, pnginfo=info)
        return path

    def optimize(self, **kwargs):
        return logic.optimize_image(self.source, self.output, **kwargs)

    def no_temps(self):
        self.assertFalse(list(self.root.glob('.panopticon-*')))

    def test_false_stamp_and_codec_failure_never_publish(self):
        original = self.source.read_bytes()
        with patch.object(MetadataStamper, 'stamp', return_value=False):
            result = self.optimize(tags=['tag'])
        self.assertFalse(result['success'])
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), original)
        self.no_temps()
        with patch.object(Image.Image, 'save', side_effect=OSError('codec fixture')):
            self.assertFalse(self.optimize()['success'])
        self.assertFalse(self.output.exists())
        self.no_temps()

    def test_existing_destination_and_source_are_protected(self):
        self.output.write_bytes(b'keep existing')
        self.assertFalse(self.optimize()['success'])
        self.assertEqual(self.output.read_bytes(), b'keep existing')
        before = self.source.read_bytes()
        result = logic.optimize_image(self.source, self.source)
        self.assertFalse(result['success'])
        self.assertEqual(self.source.read_bytes(), before)

    def test_extension_change_cannot_replace_source(self):
        jpg = self.root/'source.jpg'
        with Image.new('RGB', (12, 12)) as image: image.save(jpg)
        before = self.source.read_bytes()
        result = logic.optimize_image(jpg, self.source.with_suffix('.jpg'), format_override='PNG')
        self.assertFalse(result['success'])
        self.assertEqual(self.source.read_bytes(), before)
        self.no_temps()

    def test_destination_appearing_during_stamp_survives(self):
        stamp = MetadataStamper.stamp
        def occupied(path, bundle, **kwargs):
            result = stamp(path, bundle, **kwargs)
            self.output.write_bytes(b'external destination')
            return result
        with patch.object(MetadataStamper, 'stamp', side_effect=occupied):
            self.assertFalse(self.optimize(tags=['tag'])['success'])
        self.assertEqual(self.output.read_bytes(), b'external destination')
        self.no_temps()

    def test_source_changed_during_stamp_is_not_replaced_or_published(self):
        stamp = MetadataStamper.stamp
        def changed(path, bundle, **kwargs):
            result = stamp(path, bundle, **kwargs)
            self.source.write_bytes(b'external original')
            return result
        with patch.object(MetadataStamper, 'stamp', side_effect=changed):
            result = self.optimize(tags=['tag'])
        self.assertFalse(result['success'])
        self.assertIn('source_changed', result['error'])
        self.assertEqual(self.source.read_bytes(), b'external original')
        self.assertFalse(self.output.exists())
        self.no_temps()

    def test_cancel_before_publication_has_no_output(self):
        event = threading.Event()
        stamp = MetadataStamper.stamp
        def cancel(path, bundle, **kwargs):
            result = stamp(path, bundle, **kwargs)
            event.set()
            return result
        with patch.object(MetadataStamper, 'stamp', side_effect=cancel):
            self.assertFalse(self.optimize(tags=['tag'], cancel_event=event)['success'])
        self.assertFalse(self.output.exists())
        self.no_temps()

    def test_pre_cancel_does_not_load_codec(self):
        event = threading.Event()
        event.set()
        with patch.object(Image, 'open') as image:
            self.assertFalse(self.optimize(cancel_event=event)['success'])
        image.assert_not_called()
        self.no_temps()

    def test_native_offline_path_history_protected_without_database_change(self):
        service = LibraryService(self.root/'private.db')
        self.addCleanup(service.close)
        service.submit('register_files_minimal', [dict(path=str(self.output), filename=self.output.name,
            size=1, created=1)]).result()
        key = service.read('get_file_id', str(self.output))
        before = service.read('get_copy_version_state', key)
        service.submit('archive_copy', key, expected_fingerprint=before['fingerprint']).result()
        before = service.read('get_copy_version_state', key)
        result = self.optimize(service=service)
        self.assertFalse(result['success'])
        self.assertIn('native_history', result['error'])
        self.assertFalse(self.output.exists())
        self.assertEqual(service.read('get_copy_version_state', key), before)

    def test_native_read_error_does_not_publish(self):
        class Service:
            def read(self, *args, **kwargs): raise RuntimeError('locked fixture')
        result = self.optimize(service=Service())
        self.assertFalse(result['success'])
        self.assertFalse(self.output.exists())
        self.no_temps()

    def test_cherry_source_is_readable_but_cherry_output_protected(self):
        folder = self.root/'cherry'
        source = self.image('cherry/source.png')
        (folder/'catalog.db').write_bytes(b'foreign catalog')
        before = source.read_bytes()
        result = logic.optimize_image(source, self.output)
        self.assertTrue(result['success'], result)
        result = logic.optimize_image(source, folder/'output.png')
        self.assertFalse(result['success'])
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual((folder/'catalog.db').read_bytes(), b'foreign catalog')

    def test_same_basename_outputs_have_stable_distinct_names(self):
        first, second = self.image('a/source.png'), self.image('b/source.png')
        one = logic.get_export_path(first, self.root)
        two = logic.get_export_path(second, self.root)
        self.assertNotEqual(one.name.casefold(), two.name.casefold())
        self.assertEqual(one, logic.get_export_path(first, self.root))
        results = list(logic.batch_optimize([first, second], output_dir=self.root/'out'))
        self.assertTrue(all(result['success'] for _, _, result in results))
        self.assertEqual(len({result['output_path'] for _, _, result in results}), 2)

    def test_optimizer_formats_encode_once_with_verified_curation(self):
        save = Image.Image.save
        for format_name, suffix in (('JPEG', '.jpg'), ('WEBP', '.webp'), ('AVIF', '.avif')):
            with self.subTest(format=format_name):
                calls = []
                def once(image, *args, **kwargs):
                    calls.append(args)
                    return save(image, *args, **kwargs)
                output = self.root/('once'+suffix)
                with patch.object(Image.Image, 'save', autospec=True, side_effect=once):
                    result = logic.optimize_image(self.source, output, format_override=format_name,
                        tags=['curada ñ'], rating=4, quality=71)
                self.assertTrue(result['success'], result)
                self.assertEqual(len(calls), 1)
                extracted = MetadataExtractor.extract(output, strict=True)
                self.assertEqual((extracted.tags, extracted.rating), (['curada ñ'], 4))
                self.assertEqual(result['new_size'], output.stat().st_size)

    def test_avif_converter_and_crop_include_metadata_in_first_encode(self):
        source = self.image('prompts.png', prompts=True)
        save = Image.Image.save
        for action in ('convert', 'crop'):
            with self.subTest(action=action):
                calls = []
                def once(image, *args, **kwargs):
                    calls.append(args)
                    return save(image, *args, **kwargs)
                output = self.root/(action+'.avif')
                with patch.object(Image.Image, 'save', autospec=True, side_effect=once):
                    if action == 'convert':
                        result = convert_single(source, output, target_format='AVIF')
                        self.assertTrue(result.success, result.error)
                    else: crop_image(source, QRectF(0, 0, .5, .5), output)
                self.assertEqual(len(calls), 1)
                self.assertEqual(MetadataExtractor.extract(output).positive_prompt, 'forest ñ')

    def test_resize_budget_checked_before_allocating(self):
        with Image.open(self.source) as image, patch.object(image, 'resize') as resize:
            with self.assertRaisesRegex(ValueError, 'resize_limit'):
                logic.resize_image(image, width=32768, height=32768, lock_aspect=False)
            resize.assert_not_called()
        self.assertFalse(self.optimize(resize_width=32768, resize_height=32768, lock_aspect=False)['success'])
        self.assertFalse(self.output.exists())


if __name__ == '__main__': unittest.main(verbosity=2)
