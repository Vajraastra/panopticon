"""Real private files/codecs/Native history: no overwrite or privacy leaks."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, PngImagePlugin
from PySide6.QtWidgets import QApplication
from core.paths import CachePaths
from core.library_service import LibraryService
from modules.watermarker.logic import watermarker as logic


class Publication(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(setattr, CachePaths, '_cache_root', CachePaths._cache_root)
        CachePaths.set_cache_root(self.root/'cache')
        self.source, self.asset, self.output = (self.root/name for name in ('source.png','asset.png','output.png'))
        text = PngImagePlugin.PngInfo()
        text.add_itxt('parameters', 'secret prompt; tags=curada, rating=5')
        text.add_text('arbitrary-private-field', 'private artist')
        exif = Image.Exif()
        exif[270] = 'private prompt'
        with Image.new('RGBA', (80, 64), 'green') as image:
            image.save(self.source, pnginfo=text, exif=exif)
        with Image.new('RGBA', (8, 8), 'red') as image:
            image.save(self.asset)
        self.original = self.source.read_bytes()
        self.asset_original = self.asset.read_bytes()

    def publish(self, **kwargs):
        return logic.process_image(self.source, self.output, watermark_path=self.asset, **kwargs)

    def assert_untouched(self):
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.asset.read_bytes(), self.asset_original)
        self.assertEqual(list(self.root.glob('.panopticon-watermark-*')), [])

    def assert_clean(self, path, fmt):
        with Image.open(path) as image:
            self.assertEqual(image.format, fmt)
            self.assertEqual(image.size, (80, 64))
            image.load()
            self.assertFalse(image.getexif())
            self.assertFalse(getattr(image, 'text', {}))
            self.assertFalse(set(image.info)-{'jfif','jfif_version','jfif_unit','jfif_density','loop','background','timestamp','duration'})

    def test_png_removes_private_text_exif_and_keeps_original(self):
        self.assertTrue(self.publish()[0])
        self.assert_clean(self.output, 'PNG')
        with Image.open(self.output) as image:
            self.assertNotEqual(image.getpixel((1,1)), (0,128,0,255))
        self.assert_untouched()

    def test_jpeg_encoded_once_without_private_exif(self):
        self.output = self.root/'output.jpg'
        original = Image.Image.save
        saves = []
        def save(image, *args, **kwargs):
            saves.append(args)
            return original(image, *args, **kwargs)
        with patch.object(Image.Image, 'save', save):
            self.assertTrue(self.publish()[0])
        self.assertEqual(len(saves), 1)
        self.assert_clean(self.output, 'JPEG')
        self.assert_untouched()

    def test_webp_verified_private_copy(self):
        self.output = self.root/'output.webp'
        self.assertTrue(self.publish()[0])
        self.assert_clean(self.output, 'WEBP')
        self.assert_untouched()

    def test_avif_verified_private_copy_when_codec_available(self):
        Image.init()
        if 'AVIF' not in Image.SAVE:
            self.skipTest('AVIF codec unavailable in this project environment')
        self.output = self.root/'output.avif'
        self.assertTrue(self.publish()[0])
        self.assert_clean(self.output, 'AVIF')
        self.assert_untouched()

    def test_source_destination_rejected_without_edit(self):
        self.output = self.source
        ok, error = self.publish()
        self.assertFalse(ok)
        self.assertIn('new_output', error)
        self.assert_untouched()

    def test_destination_asset_rejected(self):
        self.output = self.asset
        self.assertFalse(self.publish()[0])
        self.assert_untouched()

    def test_hardlinked_source_destination_rejected(self):
        os.link(self.source, self.output)
        self.assertFalse(self.publish()[0])
        self.assertEqual(self.output.read_bytes(), self.original)
        self.assert_untouched()

    def test_existing_unrelated_destination_preserved(self):
        self.output.write_bytes(b'existing other copy')
        self.assertFalse(self.publish()[0])
        self.assertEqual(self.output.read_bytes(), b'existing other copy')
        self.assert_untouched()

    def test_missing_watermark_never_silently_publishes(self):
        self.asset.unlink()
        self.assertFalse(self.publish()[0])
        self.assertFalse(self.output.exists())

    def test_missing_logo_never_silently_publishes(self):
        self.assertFalse(self.publish(logo_path=self.root/'missing.png')[0])
        self.assertFalse(self.output.exists())
        self.assert_untouched()

    def test_source_changed_while_rendering_cannot_publish(self):
        render = logic.render_image
        def change(*args, **kwargs):
            result = render(*args, **kwargs)
            self.source.write_bytes(self.original+b'changed')
            return result
        with patch.object(logic, 'render_image', change):
            ok, error = self.publish()
        self.assertFalse(ok)
        self.assertEqual(error, 'watermarker_input_changed')
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.panopticon-watermark-*')), [])

    def test_asset_changed_while_rendering_cannot_publish(self):
        render = logic.render_image
        def change(*args, **kwargs):
            result = render(*args, **kwargs)
            self.asset.write_bytes(self.asset_original+b'changed')
            return result
        with patch.object(logic, 'render_image', change):
            self.assertFalse(self.publish()[0])
        self.assertFalse(self.output.exists())

    def test_concurrent_destination_wins_atomic_publication(self):
        operation = 'rename' if os.name == 'nt' else 'link'
        original = getattr(os, operation)
        def raced(source, destination):
            Path(destination).write_bytes(b'concurrent winner')
            return original(source, destination)
        with patch.object(logic.os, operation, raced):
            self.assertFalse(self.publish()[0])
        self.assertEqual(self.output.read_bytes(), b'concurrent winner')
        self.assert_untouched()

    def test_cancellation_before_admission_has_no_output(self):
        event = threading.Event()
        event.set()
        self.assertEqual(self.publish(cancel_event=event), (False,'watermarker_cancelled'))
        self.assertFalse(self.output.exists())
        self.assert_untouched()

    def test_cancellation_during_pattern_removes_only_temporary(self):
        event = threading.Event()
        original = Image.Image.paste
        calls = []
        def cancel(image, *args, **kwargs):
            calls.append(True)
            result = original(image, *args, **kwargs)
            if len(calls) == 3:
                event.set()
            return result
        with patch.object(Image.Image, 'paste', cancel):
            self.assertEqual(self.publish(cancel_event=event), (False,'watermarker_cancelled'))
        self.assertFalse(self.output.exists())
        self.assert_untouched()

    def test_cancellation_after_publication_keeps_success_and_file(self):
        event = threading.Event()
        operation = 'rename' if os.name == 'nt' else 'link'
        original = getattr(os, operation)
        def cancel(source, destination):
            original(source, destination)
            event.set()
        with patch.object(logic.os, operation, cancel):
            self.assertTrue(self.publish(cancel_event=event)[0])
        self.assert_clean(self.output,'PNG')
        self.assert_untouched()

    def test_failed_privacy_verification_never_publishes(self):
        original = Image.Image.save
        def leak(image, *args, **kwargs):
            info = PngImagePlugin.PngInfo()
            info.add_text('private', 'must not publish')
            kwargs['pnginfo'] = info
            return original(image, *args, **kwargs)
        with patch.object(Image.Image, 'save', leak):
            self.assertEqual(self.publish(), (False,'watermarker_privacy_verification_failed'))
        self.assertFalse(self.output.exists())
        self.assert_untouched()

    def test_encoder_failure_has_no_final_file(self):
        with patch.object(Image.Image, 'save', side_effect=OSError('encoder failed')):
            self.assertEqual(self.publish(), (False,'encoder failed'))
        self.assertFalse(self.output.exists())
        self.assert_untouched()

    def test_fsync_failure_has_no_final_file(self):
        with patch.object(logic.os, 'fsync', side_effect=OSError('fsync failed')):
            self.assertEqual(self.publish(), (False,'fsync failed'))
        self.assertFalse(self.output.exists())
        self.assert_untouched()

    def test_unsupported_format_has_no_final_file(self):
        self.output = self.root/'output.tiff'
        self.assertEqual(self.publish(), (False,'watermarker_unsupported_format'))
        self.assertFalse(self.output.exists())
        self.assert_untouched()

    def test_pixel_limit_rejects_before_render_allocation(self):
        with patch.object(logic, 'MAX_PIXELS', 100):
            self.assertEqual(self.publish(), (False,'watermarker_image_limit'))
        self.assertFalse(self.output.exists())
        self.assert_untouched()

    def test_file_limit_rejects_before_render(self):
        with patch.object(logic, 'MAX_FILE', 10):
            self.assertEqual(self.publish(), (False,'watermarker_file_limit'))
        self.assertFalse(self.output.exists())
        self.assert_untouched()

    def test_multiframe_source_rejected(self):
        animated = self.root/'animated.gif'
        with Image.new('RGB',(16,16),'red') as first, Image.new('RGB',(16,16),'blue') as second:
            first.save(animated,save_all=True,append_images=[second],duration=100,loop=0)
        ok, error = logic.process_image(animated,self.output,watermark_path=self.asset)
        self.assertFalse(ok)
        self.assertEqual(error,'watermarker_single_frame_required')
        self.assertFalse(self.output.exists())

    def test_tiny_asset_scaling_never_has_zero_stride(self):
        with Image.new('RGBA',(1,1),'red') as image:
            image.save(self.asset)
        self.assertTrue(self.publish(wm_scale=0.1)[0])
        self.assert_clean(self.output,'PNG')

    def test_pattern_work_limit_rejects_without_output(self):
        with patch.object(logic,'MAX_TILES',1):
            self.assertEqual(self.publish(),(False,'watermarker_tile_limit'))
        self.assertFalse(self.output.exists())
        self.assert_untouched()

    def test_invalid_parameters_do_not_publish(self):
        for parameters in ({'wm_scale':float('nan')},{'wm_opacity':1.5},{'wm_angle':float('inf')},
                           {'logo_path':self.asset,'logo_position':'elsewhere'},
                           {'logo_path':self.asset,'logo_size':0}):
            self.assertEqual(self.publish(**parameters),(False,'watermarker_invalid_parameters'))
        self.assertFalse(self.output.exists())
        self.assert_untouched()

    def test_svg_asset_render_and_invalid_svg_reject(self):
        svg = self.root/'mark.svg'
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8" fill="red"/></svg>')
        self.assertTrue(logic.process_image(self.source,self.output,watermark_path=svg)[0])
        self.assert_clean(self.output,'PNG')
        svg.write_text('invalid svg')
        self.assertEqual(logic.process_image(self.source,self.root/'other.png',watermark_path=svg),
                         (False,'watermarker_invalid_svg'))

    def test_historical_native_destination_is_protected_without_sql_writes(self):
        service = LibraryService(self.root/'private.db')
        self.addCleanup(service.close)
        service.submit('register_files_minimal',[dict(path=str(self.output),filename=self.output.name,
            size=10,created=1,hash_original='a'*64)]).result()
        service.submit('edit_curation',str(self.output),add_tags=['curada'],rating=5).result()
        before = service.read('get_curation_snapshot',[str(self.output)],include_revision=True)
        self.assertEqual(self.publish(native_service=service),(False,'watermarker_output_has_native_history'))
        self.assertEqual(service.read('get_curation_snapshot',[str(self.output)],include_revision=True),before)
        self.assertFalse(self.output.exists())
        self.assert_untouched()

    def test_native_destination_registered_during_render_is_rechecked(self):
        service = LibraryService(self.root/'private.db')
        self.addCleanup(service.close)
        render = logic.render_image
        def register(*args,**kwargs):
            result = render(*args,**kwargs)
            service.submit('register_files_minimal',[dict(path=str(self.output),filename=self.output.name,
                size=10,created=1,hash_original='b'*64)]).result()
            return result
        with patch.object(logic,'render_image',register):
            self.assertEqual(self.publish(native_service=service),(False,'watermarker_output_has_native_history'))
        self.assertFalse(self.output.exists())
        self.assert_untouched()

    def test_output_under_cherry_catalog_is_protected(self):
        (self.root/'catalog.db').write_bytes(b'private catalog marker')
        self.assertEqual(self.publish(),(False,'metadata_cherry_original_protected'))
        self.assertFalse(self.output.exists())
        self.assert_untouched()

    def test_export_paths_are_unique_for_repeated_and_equal_basename_sources(self):
        first = logic.get_export_path(self.source)
        second = logic.get_export_path(self.source)
        third = logic.get_export_path(self.root/'other'/self.source.name)
        self.assertEqual(len({first,second,third}),3)
        self.assertTrue(all(path.parent == CachePaths.get_tool_cache('watermarker') for path in (first,second,third)))

    def test_preview_is_bounded_clean_and_never_encodes_to_disk(self):
        before = set(self.root.iterdir())
        with patch.object(Image.Image,'save',side_effect=AssertionError('preview encoded')):
            with logic.render_preview(self.source,watermark_path=self.asset,preview_size=(40,32)) as image:
                self.assertEqual(image.size,(40,32))
                self.assertEqual(image.info,{})
        self.assertEqual(set(self.root.iterdir()),before)
        self.assert_untouched()

    def test_batch_cancellation_prevents_next_publication(self):
        event = threading.Event()
        results = logic.batch_process([self.source,self.source],watermark_path=self.asset,cancel_event=event)
        self.assertTrue(next(results)[2]['success'])
        event.set()
        with self.assertRaises(logic.WatermarkCancelled):
            next(results)
        self.assertEqual(len(list(CachePaths.get_tool_cache('watermarker').iterdir())),1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
