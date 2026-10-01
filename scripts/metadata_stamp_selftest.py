"""Codecs reales y fallos de estampado en archivos temporales; sin biblioteca real."""
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, PngImagePlugin
from core.metadata import MetadataBundle, MetadataExtractor, MetadataStamper, StampLib
from core.metadata import output_stamp as codec
from modules.metadata.logic.reader import UniversalParser


def webp_chunks(path):
    raw = path.read_bytes()
    result, pos = [], 12
    while pos < len(raw):
        kind, size = struct.unpack('<4sI', raw[pos:pos+8])
        result.append((kind, raw[pos+8:pos+8+size]))
        pos += 8+size+(size & 1)
    return result


class Stamp(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = MetadataBundle(positive_prompt='cabello ñ <lora:x:1> & ]]> 你好',
            negative_prompt='extra fingers', tags=['curada', 'ñ & <x> ]]>'], rating=5,
            quality_score=80, model='model', seed='42', steps='20', cfg='7', sampler='Euler',
            vae='vae', loras=['a:1'], tool='A1111')

    def image(self, name='input.png', **options):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        with Image.new('RGBA' if options.pop('alpha', False) else 'RGB', (19, 23)) as image:
            image.putdata([(i*19 % 255, i*11 % 255, i*7 % 255) if image.mode == 'RGB'
                else (i*19 % 255, i*11 % 255, i*7 % 255, i*23 % 255) for i in range(19*23)])
            image.save(path, **options)
        return path

    def stamp(self, path):
        self.assertIs(MetadataStamper.stamp(path, self.bundle, prepared=True), True)
        self.assertEqual(codec.read_payload(path), codec.payload(self.bundle))
        after = MetadataExtractor.extract(path, strict=True)
        self.assertEqual(after.compare(self.bundle), {})
        return after

    def test_png_preserves_idat_profile_and_unknown_chunks(self):
        path = self.image(icc_profile=b'ICC fixture')
        before = path.read_bytes()
        with open(path, 'rb') as file:
            original = [(kind, before[pos:pos+size]) for kind, size, pos in codec.png_chunks(file)
                        if kind not in (b'tEXt', b'iTXt', b'zTXt')]
        self.stamp(path)
        after = path.read_bytes()
        with open(path, 'rb') as file:
            result = [(kind, after[pos:pos+size]) for kind, size, pos in codec.png_chunks(file)
                      if kind not in (b'tEXt', b'iTXt', b'zTXt')]
        self.assertEqual(original, result)
        self.assertEqual(UniversalParser.parse_image(str(path))['positive'], self.bundle.positive_prompt)

    def test_jpeg_preserves_scan_pixels_icc_and_unicode(self):
        exif = Image.Exif()
        exif[0x9286] = b'ASCII\0\0\0old physical prompt'
        path = self.image('input.jpg', quality=73, subsampling=2, exif=exif, icc_profile=b'ICC fixture')
        before = path.read_bytes()
        with Image.open(path) as image: pixels = image.convert('RGB').tobytes()
        self.stamp(path)
        after = path.read_bytes()
        self.assertEqual(before[before.index(b'\xff\xda'):], after[after.index(b'\xff\xda'):])
        with Image.open(path) as image:
            self.assertEqual(image.convert('RGB').tobytes(), pixels)
            self.assertEqual(image.info['icc_profile'], b'ICC fixture')
        self.assertEqual(UniversalParser.parse_image(str(path))['positive'], self.bundle.positive_prompt)

    def test_webp_lossy_and_lossless_streams_preserved_with_alpha(self):
        for lossless in (False, True):
            with self.subTest(lossless=lossless):
                path = self.image(f'input-{lossless}.webp', lossless=lossless, alpha=True, exact=True,
                                  icc_profile=b'ICC fixture')
                before = dict(webp_chunks(path))
                self.stamp(path)
                after = dict(webp_chunks(path))
                for kind in (b'VP8 ', b'VP8L', b'ALPH', b'ICCP'):
                    if kind in before: self.assertEqual(before[kind], after[kind])
                self.assertEqual(UniversalParser.parse_image(str(path))['positive'], self.bundle.positive_prompt)

    def test_simple_webp_gets_extended_header_without_reencoding(self):
        path = self.image('simple.webp', lossless=False)
        self.assertNotIn(b'VP8X', dict(webp_chunks(path)))
        encoded = dict(webp_chunks(path))[b'VP8 ']
        self.stamp(path)
        chunks = dict(webp_chunks(path))
        self.assertEqual(chunks[b'VP8 '], encoded)
        self.assertTrue(chunks[b'VP8X'][0] & 4)

    def test_existing_xmp_property_survives_and_json_special_chars_round_trip(self):
        existing = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:a="urn:fixture" a:license="private"/></rdf:RDF></x:xmpmeta>'
        path = self.image('input.webp', xmp=existing)
        self.stamp(path)
        with Image.open(path) as image:
            description = ET.fromstring(image.info['xmp']).find('.//'+codec.RDF+'Description')
            self.assertEqual(description.attrib['{urn:fixture}license'], 'private')

    def test_raw_workflow_and_compressed_png_text_round_trip(self):
        info = PngImagePlugin.PngInfo()
        prompt = json.dumps({'7': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'forest ñ'}}})
        info.add_itxt('prompt', prompt, zip=True)
        info.add_text('workflow', '{"nodes":[]}', zip=True)
        path = self.image(pnginfo=info)
        bundle = MetadataExtractor.extract(path, strict=True)
        bundle.tags = ['workflow']
        self.assertEqual(bundle.raw['prompt'], prompt)
        self.assertIs(MetadataStamper.stamp(path, bundle, prepared=True), True)
        self.assertEqual(MetadataExtractor.extract(path, strict=True).raw['prompt'], prompt)
        self.assertEqual(codec.read_png(path)['workflow'], '{"nodes":[]}')

    def test_repeated_stamp_replaces_panopticon_data_and_can_clear_tags(self):
        path = self.image()
        self.stamp(path)
        self.bundle.tags, self.bundle.rating = [], 0
        self.stamp(path)
        self.assertEqual(codec.read_png(path)['panopticon_data'], codec._json(codec.payload(self.bundle)))

    def test_rating_only_negative_only_and_model_only_are_valid(self):
        for bundle in (MetadataBundle(rating=4), MetadataBundle(negative_prompt='bad'), MetadataBundle(model='M')):
            self.assertTrue(bundle.is_valid())

    def test_legacy_in_place_stamp_is_refused_without_byte_changes(self):
        path = self.image()
        before = path.read_bytes()
        self.assertIs(MetadataStamper.stamp(path, self.bundle), False)
        self.assertIs(StampLib.stamp_file(path, ['tag'], 4), False)
        self.assertEqual(path.read_bytes(), before)

    def test_curating_prepared_jpeg_does_not_replace_generation(self):
        exif = Image.Exif()
        exif[0x9286] = b'ASCII\0\0\0original\nNegative prompt: bad\nSteps: 20, Seed: 42'
        path = self.image('input.jpg', exif=exif)
        before = path.read_bytes()
        self.assertIs(StampLib.stamp_file(path, ['documented'], 4, prepared=True), True)
        result = MetadataExtractor.extract(path, strict=True)
        self.assertEqual(result.positive_prompt, 'original')
        self.assertEqual(result.tags, ['documented'])
        after = path.read_bytes()
        self.assertEqual(before[before.index(b'\xff\xda'):], after[after.index(b'\xff\xda'):])

    def test_transfer_published_output_requires_review_and_is_read_only(self):
        source, output = self.image(), self.image('output.webp')
        self.stamp(source)
        before = output.read_bytes()
        result = MetadataStamper.transfer(source, output)
        self.assertFalse(result.success)
        self.assertIn('Metadata Hub', result.error)
        self.assertEqual(output.read_bytes(), before)

    def test_transfer_prepared_is_verified_even_when_verify_false(self):
        source, output = self.image(), self.image('output.webp')
        self.stamp(source)
        result = MetadataStamper.transfer(source, output, verify=False, prepared=True)
        self.assertTrue(result.success, result.error)
        self.assertEqual(MetadataExtractor.extract(source).compare(MetadataExtractor.extract(output)), {})
        for key, value in codec.read_payload(source)['raw_generation'].items():
            self.assertEqual(codec.read_payload(output)['raw_generation'][key], value)

    def test_hardlink_and_cherry_folder_are_refused(self):
        path = self.image()
        os.link(path, self.root/'alias.png')
        before = path.read_bytes()
        self.assertFalse(MetadataStamper.stamp(path, self.bundle, prepared=True))
        self.assertEqual(path.read_bytes(), before)
        (self.root/'alias.png').unlink()
        (self.root/'catalog.db').write_bytes(b'foreign catalog')
        self.assertFalse(MetadataStamper.stamp(path, self.bundle, prepared=True))
        self.assertEqual(path.read_bytes(), before)

    def test_oversized_metadata_or_exif_leaves_original_and_no_temp(self):
        path = self.image('input.jpg')
        before = path.read_bytes()
        self.bundle.positive_prompt = 'x'*65536
        self.assertFalse(MetadataStamper.stamp(path, self.bundle, prepared=True))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(self.root.glob('.panopticon-stamp-*')))

    def test_verification_failure_and_replace_failure_preserve_prepared_bytes(self):
        path = self.image()
        before = path.read_bytes()
        for name in ('verify', 'os.replace'):
            with self.subTest(name=name), patch('core.metadata.output_stamp.'+name, side_effect=OSError('fixture')):
                self.assertFalse(MetadataStamper.stamp(path, self.bundle, prepared=True))
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(list(self.root.glob('.panopticon-stamp-*')))

    def test_stale_prepared_file_is_not_replaced(self):
        path = self.image()
        verify = codec.verify
        def change(temp, data):
            result = verify(temp, data)
            path.write_bytes(b'external changed file')
            return result
        with patch.object(codec, 'verify', side_effect=change):
            self.assertFalse(MetadataStamper.stamp(path, self.bundle, prepared=True))
        self.assertEqual(path.read_bytes(), b'external changed file')

    def test_duplicate_png_text_refused(self):
        info = PngImagePlugin.PngInfo()
        info.add_text('parameters', 'first')
        info.add_text('parameters', 'second')
        path = self.image(pnginfo=info)
        with self.assertRaisesRegex(ValueError, 'duplicate'): MetadataExtractor.extract(path, strict=True)
        before = path.read_bytes()
        self.assertFalse(MetadataStamper.stamp(path, self.bundle, prepared=True))
        self.assertEqual(path.read_bytes(), before)

    def test_bad_png_text_crc_refused(self):
        info = PngImagePlugin.PngInfo()
        info.add_text('parameters', 'text')
        path = self.image(pnginfo=info)
        value = bytearray(path.read_bytes())
        with open(path, 'rb') as source:
            for kind, size, position in codec.png_chunks(source):
                if kind == b'tEXt':
                    value[position+size] ^= 1
                    break
        path.write_bytes(value)
        with self.assertRaisesRegex(ValueError, 'text_crc'): MetadataExtractor.extract(path, strict=True)

    def test_compressed_text_bomb_is_rejected_before_decode(self):
        info = PngImagePlugin.PngInfo()
        info.add_text('parameters', 'x'*(codec.MAX_METADATA+1), zip=True)
        path = self.image(pnginfo=info)
        with self.assertRaisesRegex(ValueError, 'text_limit'): MetadataExtractor.extract(path, strict=True)

    def test_invalid_xmp_refuses_overwrite(self):
        path = self.image('invalid.webp', xmp=b'<invalid')
        before = path.read_bytes()
        self.assertFalse(MetadataStamper.stamp(path, self.bundle, prepared=True))
        self.assertEqual(path.read_bytes(), before)

    def test_avif_requires_options_before_first_encode(self):
        path = self.image('input.avif')
        before = path.read_bytes()
        self.assertFalse(MetadataStamper.stamp(path, self.bundle, prepared=True))
        self.assertEqual(path.read_bytes(), before)
        with Image.new('RGB', (19, 23)) as image:
            image.save(path, **MetadataStamper.encoder_options(self.bundle))
        self.assertTrue(MetadataStamper.verify_prepared(path, self.bundle))
        self.assertEqual(MetadataExtractor.extract(path).compare(self.bundle), {})

    def test_shared_xmp_output_can_be_edited_in_metadata_hub(self):
        from modules.metadata.logic.modifier import modify_metadata
        source = self.image('source.webp')
        self.stamp(source)
        output = self.root/'edited.webp'
        success, error = modify_metadata(str(source), str(output), 'new ñ <x>\nNegative prompt: bad')
        self.assertTrue(success, error)
        result = UniversalParser.parse_image(str(output))
        self.assertEqual(result['positive'], 'new ñ <x>')
        self.assertEqual(codec.read_payload(output)['tags'], self.bundle.tags)

    def test_malformed_generation_payload_not_silently_empty_in_strict_mode(self):
        info = PngImagePlugin.PngInfo()
        info.add_text('panopticon_data', '{broken')
        path = self.image(pnginfo=info)
        with self.assertRaises(json.JSONDecodeError): MetadataExtractor.extract(path, strict=True)
        self.assertFalse(StampLib.stamp_file(path, ['tag'], 3, prepared=True))

    def test_same_file_transfer_never_modifies_source(self):
        path = self.image()
        self.stamp(path)
        before = path.read_bytes()
        result = MetadataStamper.transfer(path, path, prepared=True)
        self.assertFalse(result.success)
        self.assertFalse(result.metadata_preserved)
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__': unittest.main(verbosity=2)
