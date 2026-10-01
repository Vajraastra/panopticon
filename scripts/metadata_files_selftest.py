"""Metadata Hub con imágenes/DB privadas; sin biblioteca real, modelos ni GPU."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.database_manager import DatabaseManager
from core.library_service import LibraryService, LibraryError
from modules.metadata.logic import file_tasks
from modules.metadata.logic.file_tasks import MetadataTasks
from modules.metadata.logic.modifier import modify_metadata
from modules.metadata.logic.reader import UniversalParser


class Files(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'original ñ.png'
        info = PngImagePlugin.PngInfo()
        info.add_itxt('parameters', 'old prompt\nNegative prompt: old bad\nSteps: 20, Seed: 9, Model: old model')
        Image.new('RGB', (32, 48), (20, 40, 60)).save(self.source, pnginfo=info)
        self.service = LibraryService(self.root / 'private.db')
        self.addCleanup(self.service.close)
        self.db = DatabaseManager(service=self.service)
        self.register(self.source)
        self.key = self.db.get_file_id(str(self.source))
        self.db.edit_curation(str(self.source), add_tags=['curada'], rating=5)
        self.db.update_file_metadata(str(self.source), dict(width=32, height=48, positive='native old', tool='native tool'))
        self.tasks = MetadataTasks(self.service, self.root / 'recovery')
        self.event = threading.Event()
        self.capture = self.tasks.load(str(self.source), self.event)
        self.original = self.source.read_bytes()

    def register(self, path):
        info = path.stat()
        self.db.register_files_minimal([dict(path=str(path), filename=path.name, size=info.st_size,
            created=info.st_ctime, hash_original=hashlib.sha256(path.read_bytes()).hexdigest(), origin_url='Cherry receipt')])

    def state(self):
        return self.service.read('get_copy_version_state', self.key)

    def edit(self, **kwargs):
        return self.tasks.edit(self.capture, 'new ñ prompt', 'new bad', event=self.event, **kwargs)

    def test_original_publishes_current_and_previous_history(self):
        before = self.state()['state']
        result = self.edit()
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['native_committed'])
        after = self.state()['state']
        self.assertEqual(after['file']['hash_original'], before['file']['hash_original'])
        self.assertEqual(after['file']['hash_mod'], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(after['file']['meta_positive'], 'new ñ prompt')
        self.assertEqual(after['tags'], before['tags'])
        self.assertEqual(after['file']['rating'], 5)
        self.assertEqual(after['file']['origin_url'], 'Cherry receipt')
        record = self.service.read('get_generation_history_page', self.key)['records'][0]
        self.assertEqual(record['previous']['native']['meta_positive'], 'native old')
        self.assertEqual(record['previous']['observed']['positive'], 'old prompt')
        journal = json.loads(Path(result['journal']).read_bytes())
        self.assertEqual(journal['phase'], 'complete')
        self.assertEqual(Path(journal['backup']).read_bytes(), self.original)
        self.assertEqual(list(self.root.glob('.panopticon-metadata-*')), [])

    def test_clean_preserves_native_curation_and_previous_generation(self):
        result = self.tasks.edit(self.capture, '', '', event=self.event)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(UniversalParser.parse_image(str(self.source))['positive'], '')
        self.assertEqual(self.state()['state']['file']['meta_tool'], '')
        self.assertEqual(self.db.get_tags_for_file(str(self.source)), ['curada'])

    def test_export_fresh_leaves_original_and_native_unchanged(self):
        before = self.state()
        result = self.edit(destination=str(self.root / 'export.png'))
        self.assertEqual(result['status'], 'complete')
        self.assertFalse(result['native_committed'])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.service.read('get_generation_history_status')['prepared'])

    def test_export_original_and_existing_target_rejected(self):
        for target in (self.source, self.root / 'occupied.png'):
            if target != self.source: target.write_bytes(b'keep')
            result = self.edit(destination=str(target))
            self.assertEqual(result['status'], 'error')
            self.assertFalse(result['published'])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual((self.root / 'occupied.png').read_bytes(), b'keep')

    def test_export_offline_history_even_different_case_is_protected(self):
        target = self.root / 'ÁRBOL.png'
        self.register(target) if target.exists() else self.db.register_files_minimal([
            dict(path=str(target), filename=target.name, size=1, created=1)])
        key = self.db.get_file_id(str(target))
        state = self.service.read('get_copy_version_state', key)
        self.service.submit('archive_copy', key, expected_fingerprint=state['fingerprint']).result()
        result = self.edit(destination=str(self.root / 'árbol.png'))
        self.assertIn('path_has_history', result['error'])
        self.assertFalse(result['published'])

    def test_changed_source_is_rejected_before_effect(self):
        self.source.write_bytes(self.original+b'changed')
        changed = self.source.read_bytes()
        result = self.edit()
        self.assertIn('source_changed', result['error'])
        self.assertEqual(self.source.read_bytes(), changed)
        self.assertFalse(result['published'])

    def test_changed_native_curation_blocks_publication(self):
        self.db.edit_curation(str(self.source), add_tags=['later'])
        result = self.edit()
        self.assertIn('changed_since_review', result['error'])
        self.assertFalse(result['published'])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_cherry_original_and_export_destination_protected(self):
        (self.root / 'catalog.db').write_bytes(b'portable')
        result = self.edit()
        self.assertIn('cherry_original_protected', result['error'])
        self.assertEqual(self.source.read_bytes(), self.original)
        other = self.root / 'portable'
        other.mkdir()
        (other / 'panopticon.curation.db').write_bytes(b'portable')
        result = self.edit(destination=str(other / 'new.png'))
        self.assertFalse(result['published'])

    def test_external_original_rejected_export_allowed(self):
        source = self.root / 'external.png'
        source.write_bytes(self.original)
        captured = self.tasks.load(str(source), self.event)
        self.assertIsNone(captured['native'])
        result = self.tasks.edit(captured, 'new', '', event=self.event)
        self.assertIn('not_indexed', result['error'])
        self.assertEqual(source.read_bytes(), self.original)
        result = self.tasks.edit(captured, 'new', '', destination=str(self.root / 'external_copy.png'), event=self.event)
        self.assertEqual(result['status'], 'complete')

    def test_pre_cancel_has_no_effect(self):
        self.event.set()
        result = self.edit()
        self.assertIn('cancelled', result['error'])
        self.assertFalse((self.root / 'recovery').exists())

    def test_transform_and_verification_failure_leave_original(self):
        with patch.object(file_tasks, 'modify_metadata', return_value=(False, 'fixture transform failure')):
            result = self.edit()
        self.assertFalse(result['published'])
        self.assertEqual(self.source.read_bytes(), self.original)
        def wrong(source, target, text):
            Path(target).write_bytes(Path(source).read_bytes())
            return True, ''
        with patch.object(file_tasks, 'modify_metadata', side_effect=wrong): result = self.edit()
        self.assertIn('verification_failed', result['error'])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_commit_failure_after_publish_is_partial_and_backup_remains(self):
        submit = self.service.submit
        def fail(operation, *args, **kwargs):
            if operation == 'commit_metadata_edit':
                from concurrent.futures import Future
                future = Future()
                future.set_exception(LibraryError(operation, 'fixture owner failure'))
                return future
            return submit(operation, *args, **kwargs)
        before = self.state()
        with patch.object(self.service, 'submit', side_effect=fail): result = self.edit()
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['published'])
        self.assertFalse(result['native_committed'])
        self.assertEqual(before, self.state())
        journal = json.loads(Path(result['journal']).read_bytes())
        self.assertEqual(journal['phase'], 'review_required')
        self.assertEqual(Path(journal['backup']).read_bytes(), self.original)
        self.assertEqual(UniversalParser.parse_image(str(self.source))['positive'], 'new ñ prompt')

    def test_final_journal_failure_keeps_confirmed_commit_visible(self):
        write = file_tasks._journal
        def fail(path, data):
            if data['phase'] == 'complete': raise OSError('fixture final journal failure')
            return write(path, data)
        with patch.object(file_tasks, '_journal', side_effect=fail): result = self.edit()
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['native_committed'])
        self.assertEqual(self.state()['state']['file']['meta_positive'], 'new ñ prompt')

    def test_export_race_does_not_overwrite(self):
        target = self.root / 'new.png'
        rename = file_tasks.os.rename
        def race(source, destination):
            Path(destination).write_bytes(b'other writer')
            return rename(source, destination)
        with patch.object(file_tasks.os, 'rename', side_effect=race): result = self.edit(destination=str(target))
        self.assertFalse(result['published'])
        self.assertEqual(target.read_bytes(), b'other writer')

    def test_jpeg_unicode_and_pixels_preserved(self):
        source = self.root / 'photo.jpg'
        Image.new('RGB', (32, 48), (21, 55, 86)).save(source, quality=73)
        self.register(source)
        captured = self.tasks.load(str(source), self.event)
        with Image.open(source) as image: before = image.tobytes()
        result = self.tasks.edit(captured, 'niño ñ á', 'malo', event=self.event)
        self.assertEqual(result['status'], 'complete')
        with Image.open(source) as image: self.assertEqual(image.tobytes(), before)
        self.assertEqual(UniversalParser.parse_image(str(source))['positive'], 'niño ñ á')
        captured = self.tasks.load(str(source), self.event)
        result = self.tasks.edit(captured, '', '', event=self.event)
        self.assertEqual(result['status'], 'complete')
        with Image.open(source) as image: self.assertEqual(image.tobytes(), before)

    def test_webp_xmp_read_and_cdata_prompt(self):
        source = self.root / 'web.webp'
        Image.new('RGB', (32, 48), (21, 55, 86)).save(source, lossless=True)
        self.register(source)
        captured = self.tasks.load(str(source), self.event)
        with Image.open(source) as image: before = image.tobytes()
        result = self.tasks.edit(captured, 'niño ]]> & <texto>', 'malo', event=self.event)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(UniversalParser.parse_image(str(source))['positive'], 'niño ]]> & <texto>')
        with Image.open(source) as image: self.assertEqual(image.tobytes(), before)

    def test_png_compressed_unicode_read(self):
        source = self.root / 'compressed.png'
        info = PngImagePlugin.PngInfo()
        info.add_itxt('parameters', 'niño\nNegative prompt: malo', zip=True)
        Image.new('RGB', (16, 16)).save(source, pnginfo=info)
        self.assertEqual(UniversalParser.parse_image(str(source))['positive'], 'niño')

    def test_png_metadata_bomb_rejected(self):
        source = self.root / 'bomb.png'
        info = PngImagePlugin.PngInfo()
        info.add_itxt('parameters', 'a'*300000, zip=True)
        Image.new('RGB', (16, 16)).save(source, pnginfo=info)
        self.assertIn('error', UniversalParser.parse_image(str(source)))

    def test_cancel_after_publish_does_not_cancel_native_commit(self):
        replace = file_tasks.os.replace
        def cancel(source, destination):
            value = replace(source, destination)
            if Path(destination) == self.source: self.event.set()
            return value
        with patch.object(file_tasks.os, 'replace', side_effect=cancel): result = self.edit()
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['native_committed'])

    def test_avif_current_generation_if_codec_available(self):
        source = self.root / 'avif.avif'
        try: Image.new('RGB', (32, 48), (21, 55, 86)).save(source, 'AVIF')
        except (KeyError, OSError): self.skipTest('AVIF codec unavailable')
        self.register(source)
        captured = self.tasks.load(str(source), self.event)
        result = self.tasks.edit(captured, 'avif ñ', '', event=self.event)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(UniversalParser.parse_image(str(source))['positive'], 'avif ñ')

    def test_source_changed_during_preparation_is_not_overwritten(self):
        transform = file_tasks.modify_metadata
        def change(source, output, text):
            result = transform(source, output, text)
            self.source.write_bytes(self.original+b'external change')
            return result
        with patch.object(file_tasks, 'modify_metadata', side_effect=change): result = self.edit()
        self.assertFalse(result['published'])
        self.assertIn('source_changed', result['error'])
        self.assertTrue(self.source.read_bytes().endswith(b'external change'))

    def test_journal_failure_before_publish_leaves_original(self):
        write = file_tasks._journal
        def fail(path, data):
            if data['phase'] == 'ready_to_publish': raise OSError('fixture journal before effect')
            return write(path, data)
        with patch.object(file_tasks, '_journal', side_effect=fail): result = self.edit()
        self.assertFalse(result['published'])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_changed_native_after_approval_reports_partial_keeps_new_curation(self):
        submit = self.service.submit
        def change(operation, *args, **kwargs):
            future = submit(operation, *args, **kwargs)
            if operation == 'approve_metadata_edit':
                future.result()
                self.db.edit_curation(str(self.source), add_tags=['concurrent'])
            return future
        with patch.object(self.service, 'submit', side_effect=change): result = self.edit()
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['native_committed'])
        self.assertIn('concurrent', self.db.get_tags_for_file(str(self.source)))
        journal = json.loads(Path(result['journal']).read_bytes())
        self.assertEqual(journal['native_previous']['meta_positive'], 'native old')
        self.assertEqual(Path(journal['backup']).read_bytes(), self.original)

    def test_jpeg_segment_capacity_failure_keeps_original(self):
        source = self.root / 'large_prompt.jpg'
        Image.new('RGB', (16, 16)).save(source)
        self.register(source)
        captured = self.tasks.load(str(source), self.event)
        original = source.read_bytes()
        result = self.tasks.edit(captured, 'x'*40000, 'y'*40000, event=self.event)
        self.assertFalse(result['published'])
        self.assertIn('segment capacity', result['error'])
        self.assertEqual(source.read_bytes(), original)

    def test_hardlinked_source_requires_review(self):
        alias = self.root / 'alias.png'
        import os
        os.link(self.source, alias)
        with self.assertRaisesRegex(ValueError, 'unsupported_file'):
            self.tasks.load(str(self.source), self.event)

    def test_avif_xmp_payload_limit_is_checked(self):
        with self.assertRaisesRegex(ValueError, 'review limit'):
            UniversalParser._xmp_prompts(b'x'*300000)

    def test_backup_under_watched_root_is_rejected(self):
        self.db.add_watched_folder(str(self.root))
        result = self.edit()
        self.assertIn('recovery_inside_library', result['error'])
        self.assertFalse(result['published'])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_backup_under_portable_catalog_is_rejected(self):
        folder = self.root/'portable-backups'
        folder.mkdir()
        (folder/'catalog.db').write_bytes(b'portable')
        self.tasks.journal_root = folder/'recovery'
        result = self.edit()
        self.assertIn('cherry_original_protected', result['error'])
        self.assertFalse(result['published'])


if __name__ == '__main__': unittest.main(verbosity=2)
