"""Archivos/DB/journal privados: altas independientes y publicaciones parciales."""
from concurrent.futures import Future
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, PngImagePlugin
from core.derived_publication import DerivedPublication, write
from core.library_repository import LibraryRepository
from core.library_service import LibraryService
from core.metadata import MetadataExtractor
from modules.image_optimizer.logic import optimizer as logic


class Native(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source, self.output = self.root/'source.png', self.root/'output.png'
        self.journals = self.root/'journals'
        info = PngImagePlugin.PngInfo()
        info.add_itxt('parameters', 'forest ñ\nNegative prompt: blur\nSteps: 20, Seed: 42, Model: toy')
        with Image.new('RGB', (24, 16), 'green') as image: image.save(self.source, pnginfo=info)
        self.original = self.source.read_bytes()
        self.service = LibraryService(self.root/'private.db')
        self.addCleanup(self.service.close)
        self.service.submit('register_files_minimal', [dict(path=str(self.source), filename=self.source.name,
            size=len(self.original), created=1, hash_original=hashlib.sha256(self.original).hexdigest(),
            origin_url='Cherry receipt', artist='Artist')]).result()
        self.service.submit('edit_curation', str(self.source), add_tags=['curada'], rating=5).result()
        self.row = self.service.read('get_curation_snapshot', [str(self.source)], include_revision=True)[0]
        self.before = self.service.read('get_copy_version_state', self.row['id'])
        self.service.submit('prepare_derived_copies').result()

    def sql(self, statement):
        with closing(sqlite3.connect(self.service.db_path, isolation_level=None)) as conn:
            return conn.execute(statement).fetchall()

    def optimize(self, **kwargs):
        return logic.optimize_image(self.source, self.output, source_snapshot=self.row,
            native_service=self.service, journal_root=self.journals, tags=list(self.row['tags']), rating=5,
            max_side=12, **kwargs)

    def journal(self, result):
        return json.loads(Path(result['journal']).read_text(encoding='utf-8'))

    def unchanged(self):
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.service.read('get_copy_version_state', self.row['id']), self.before)

    def partial(self, result):
        self.assertFalse(result['success'], result)
        self.assertTrue(result['published'], result)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(self.output.exists())
        self.assertTrue(result['journal'])
        self.assertFalse(list(self.root.glob('.panopticon-*')))

    def test_verified_copy_has_own_sha_generation_identity_and_clean_curation(self):
        save, calls = Image.Image.save, []
        def once(image, *args, **kwargs):
            calls.append(1)
            return save(image, *args, **kwargs)
        with patch.object(Image.Image, 'save', autospec=True, side_effect=once):
            result = self.optimize()
        self.assertTrue(result['success'], result)
        self.assertTrue(result['native_registered'])
        self.assertNotEqual(result['copy_id'], self.row['id'])
        self.assertEqual(len(calls), 1)
        native = self.service.read('get_copy_version_state', result['copy_id'])['state']
        self.assertEqual(native['file']['hash_original'], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertIsNone(native['file']['hash_mod'])
        self.assertIsNone(native['file']['origin_url'])
        self.assertEqual(native['file']['rating'], 0)
        self.assertEqual(native['tags'], [])
        self.assertEqual((native['file']['width'], native['file']['height']), (12, 8))
        self.assertEqual(native['file']['meta_positive'], 'forest ñ')
        self.assertEqual(MetadataExtractor.extract(self.output).tags, ['curada'])
        journal = self.journal(result)
        self.assertEqual((journal['phase'], journal['native_confirmation']), ('complete', 'confirmed'))
        self.assertEqual(journal['receipt']['copy_id'], result['copy_id'])
        self.assertTrue(self.service.read('check_fts')['consistent'])
        self.unchanged()

    def test_cleared_embedded_generation_is_not_inherited_from_native_source(self):
        self.service.submit('update_file_metadata', str(self.source), dict(positive='central only', model='old')).result()
        self.row = self.service.read('get_curation_snapshot', [str(self.source)], include_revision=True)[0]
        result = self.optimize(preserve_metadata=False)
        self.assertTrue(result['success'], result)
        file = self.service.read('get_copy_version_state', result['copy_id'])['state']['file']
        self.assertEqual(file['meta_positive'], '')
        self.assertEqual(file['meta_model'], '')

    def test_jpeg_webp_avif_indexed_outputs_encode_once_and_store_their_verified_bytes(self):
        save = Image.Image.save
        for format_name in ('JPEG', 'WEBP', 'AVIF'):
            with self.subTest(format=format_name):
                calls = []
                def once(image, *args, **kwargs):
                    calls.append(1)
                    return save(image, *args, **kwargs)
                with patch.object(Image.Image, 'save', autospec=True, side_effect=once):
                    result = self.optimize(format_override=format_name, preserve_metadata=False)
                self.assertTrue(result['success'], result)
                self.assertEqual(len(calls), 1)
                path = Path(result['output_path'])
                file = self.service.read('get_copy_version_state', result['copy_id'])['state']['file']
                self.assertEqual(file['hash_original'], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(file['meta_positive'], '')
                self.assertEqual(MetadataExtractor.extract(path, strict=True).tags, ['curada'])
        self.unchanged()

    def test_external_source_exports_without_native_schema_identity_or_journal(self):
        service = LibraryService(self.root/'external.db')
        self.addCleanup(service.close)
        row = service.read('get_curation_snapshot', [str(self.source)], include_revision=True)[0]
        with service.metadata_destination_guard() as guard:
            result = logic.optimize_image(self.source, self.output, source_snapshot=row,
                native_service=service, service=guard, journal_root=self.journals)
        self.assertTrue(result['success'], result)
        self.assertFalse(result['native_registered'])
        self.assertEqual(result['journal'], '')
        self.assertIsNone(result['copy_id'])
        self.assertFalse(service.read('get_derived_copies_status')['prepared'])
        self.assertEqual(service.read('count_files'), 0)
        self.assertFalse(self.journals.exists())
        self.unchanged()

    def test_unprepared_native_contract_is_rejected_before_publication(self):
        self.sql('DROP TABLE panopticon_derived_copies_v1')
        result = self.optimize()
        self.assertFalse(result['published'], result)
        self.assertIn('not_prepared', result['error'])
        self.assertFalse(self.output.exists())
        self.unchanged()

    def test_stale_full_source_revision_never_publishes(self):
        self.service.submit('edit_curation', str(self.source), add_tags=['new']).result()
        result = self.optimize()
        self.assertFalse(result['published'], result)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.service.read('count_files'), 1)

    def test_known_source_sha_mismatch_never_publishes(self):
        self.sql("UPDATE files SET hash_original='"+'a'*64+"'")
        self.row = self.service.read('get_curation_snapshot', [str(self.source)], include_revision=True)[0]
        result = self.optimize()
        self.assertIn('source_hash_changed', result['error'])
        self.assertFalse(self.output.exists())

    def test_source_bytes_changed_after_approval_never_publish(self):
        ready = DerivedPublication.ready
        def changed(task, *args):
            ready(task, *args)
            self.source.write_bytes(b'external modification')
        with patch.object(DerivedPublication, 'ready', changed): result = self.optimize()
        self.assertFalse(result['published'], result)
        self.assertIn('source_changed', result['error'])
        self.assertFalse(self.output.exists())
        self.assertEqual(self.journal(result)['phase'], 'not_published')

    def test_destination_created_after_approval_is_never_replaced(self):
        ready = DerivedPublication.ready
        def occupied(task, *args):
            ready(task, *args)
            self.output.write_bytes(b'external destination')
        with patch.object(DerivedPublication, 'ready', occupied): result = self.optimize()
        self.assertFalse(result['published'], result)
        self.assertEqual(self.output.read_bytes(), b'external destination')
        self.unchanged()

    def test_prepared_bytes_changed_after_journal_never_publish(self):
        ready = DerivedPublication.ready
        def changed(task, source_identity, result_identity, prepared_output, event):
            ready(task, source_identity, result_identity, prepared_output, event)
            prepared_output.write_bytes(b'tampered')
        with patch.object(DerivedPublication, 'ready', changed): result = self.optimize()
        self.assertFalse(result['published'], result)
        self.assertIn('prepared_file_changed', result['error'])
        self.assertFalse(self.output.exists())
        self.unchanged()

    def test_native_destination_race_after_approve_reports_partial_preserving_occupant(self):
        rename = os.rename
        def publish(source, output):
            self.service.submit('register_files_minimal', [dict(path=str(output), filename=Path(output).name,
                size=123, created=1)]).result()
            rename(source, output)
        with patch.object(logic.os, 'rename', side_effect=publish): result = self.optimize()
        self.partial(result)
        self.assertIn('destination_occupied', result['error'])
        self.assertFalse(result['native_registered'])
        self.assertEqual(self.sql('SELECT file_size FROM files WHERE id != '+str(self.row['id'])), [(123,)])
        self.assertEqual(self.sql('SELECT count(*) FROM panopticon_derived_copies_v1'), [(0,)])
        self.unchanged()

    def test_native_source_revision_race_after_publish_reports_partial(self):
        rename = os.rename
        def publish(source, output):
            rename(source, output)
            self.service.submit('edit_curation', str(self.source), rating=1).result()
        with patch.object(logic.os, 'rename', side_effect=publish): result = self.optimize()
        self.partial(result)
        self.assertEqual(self.service.read('count_files'), 1)
        self.assertFalse(result['native_registered'])

    def test_fts_failure_rolls_back_native_without_deleting_published_file(self):
        with patch.object(LibraryRepository, '_fts_sync_file', side_effect=RuntimeError('FTS fixture')):
            result = self.optimize()
        self.partial(result)
        self.assertIn('FTS fixture', result['error'])
        self.assertEqual(self.service.read('count_files'), 1)
        self.assertEqual(self.sql('SELECT count(*) FROM panopticon_derived_copies_v1'), [(0,)])
        self.assertEqual(self.journal(result)['native_confirmation'], 'unconfirmed')
        self.unchanged()

    def test_lost_commit_ack_resolves_via_receipt_without_replaying(self):
        submit, operations = self.service.submit, []
        def lost(operation, *args, **kwargs):
            operations.append(operation)
            future = submit(operation, *args, **kwargs)
            if operation == 'register_derived_copy':
                future.result()
                future = Future()
                future.set_exception(RuntimeError('lost ack fixture'))
            return future
        with patch.object(self.service, 'submit', side_effect=lost): result = self.optimize()
        self.assertTrue(result['success'], result)
        self.assertEqual(operations.count('register_derived_copy'), 1)
        self.assertEqual(self.service.read('count_files'), 2)

    def test_receipt_read_failure_is_partial_even_if_native_commit_succeeded(self):
        read = self.service.read
        def missing(operation, *args, **kwargs):
            if operation == 'get_derived_copy_receipt': raise RuntimeError('receipt fixture')
            return read(operation, *args, **kwargs)
        with patch.object(self.service, 'read', side_effect=missing): result = self.optimize()
        self.partial(result)
        self.assertFalse(result['native_registered'])
        self.assertEqual(self.service.read('count_files'), 2)
        journal = self.journal(result)
        self.assertIsNotNone(self.service.read('get_derived_copy_receipt', journal['operation_id'])['receipt'])
        self.assertEqual(journal['native_confirmation'], 'unconfirmed')

    def test_cancel_at_publication_finishes_receipt_confirmation(self):
        rename, event = os.rename, threading.Event()
        def publish(source, output):
            rename(source, output)
            event.set()
        with patch.object(logic.os, 'rename', side_effect=publish): result = self.optimize(cancel_event=event)
        self.assertTrue(event.is_set())
        self.assertTrue(result['success'], result)
        self.assertTrue(result['native_registered'])
        self.unchanged()

    def test_journal_failure_before_publish_has_no_output_or_native_insert(self):
        with patch.object(DerivedPublication, 'save', side_effect=OSError('journal fixture')):
            result = self.optimize()
        self.assertFalse(result['published'], result)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.service.read('count_files'), 1)
        self.unchanged()

    def test_journal_failure_after_publish_retains_ready_evidence_and_skips_native_insert(self):
        save = DerivedPublication.save
        def broken(task):
            if task.data['published']: raise OSError('published journal fixture')
            save(task)
        with patch.object(DerivedPublication, 'save', broken): result = self.optimize()
        self.partial(result)
        self.assertFalse(result['native_registered'])
        self.assertEqual(self.service.read('count_files'), 1)
        self.assertEqual(self.journal(result)['phase'], 'ready_to_publish')

    def test_complete_journal_failure_reports_partial_with_confirmed_native_receipt(self):
        save = DerivedPublication.save
        def broken(task):
            if task.data['phase'] == 'complete': raise OSError('complete journal fixture')
            save(task)
        with patch.object(DerivedPublication, 'save', broken): result = self.optimize()
        self.partial(result)
        self.assertTrue(result['native_registered'])
        self.assertEqual(self.journal(result)['native_confirmation'], 'confirmed')
        self.assertEqual(self.service.read('count_files'), 2)

    def test_published_bytes_changed_do_not_register(self):
        rename = os.rename
        def changed(source, output):
            rename(source, output)
            Path(output).write_bytes(b'external bytes')
        with patch.object(logic.os, 'rename', side_effect=changed): result = self.optimize()
        self.partial(result)
        self.assertIn('published_file_changed', result['error'])
        self.assertEqual(self.service.read('count_files'), 1)
        self.assertEqual(self.output.read_bytes(), b'external bytes')

    def test_output_changed_after_native_ack_reports_partial_with_historical_receipt(self):
        read = self.service.read
        def changed(operation, *args, **kwargs):
            value = read(operation, *args, **kwargs)
            if operation == 'get_derived_copy_receipt': self.output.write_bytes(b'external after ack')
            return value
        with patch.object(self.service, 'read', side_effect=changed): result = self.optimize()
        self.partial(result)
        self.assertTrue(result['native_registered'])
        self.assertEqual(self.service.read('count_files'), 2)
        self.assertEqual(self.output.read_bytes(), b'external after ack')
        self.assertEqual(self.journal(result)['native_confirmation'], 'confirmed')

    def test_journal_inside_watched_or_cherry_folder_is_rejected_before_writes(self):
        for mode in ('watched', 'cherry'):
            with self.subTest(mode=mode):
                if mode == 'watched':
                    self.service.submit('add_watched_folder', str(self.root)).result()
                else:
                    self.sql('DELETE FROM watched_folders')
                    self.journals.mkdir()
                    (self.journals/'catalog.db').write_bytes(b'foreign')
                result = self.optimize()
                self.assertFalse(result['published'], result)
                self.assertFalse(self.output.exists())
                self.assertFalse(list(self.journals.glob('*/journal.json')))
        self.assertEqual((self.journals/'catalog.db').read_bytes(), b'foreign')
        self.unchanged()

    def test_pending_write_can_cancel_but_running_write_drains(self):
        event, pending = threading.Event(), Future()
        class Service:
            def submit(self, *args, **kwargs):
                event.set()
                return pending
        with self.assertRaisesRegex(ValueError, 'cancelled'): write(Service(), 'fixture', event=event)
        self.assertTrue(pending.cancelled())
        event.clear()
        running = Future()
        running.set_running_or_notify_cancel()
        thread = threading.Thread(target=lambda: (event.set(), running.set_result(True)))
        class Running:
            def submit(self, *args, **kwargs):
                thread.start()
                return running
        try: write(Running(), 'fixture', event=event)
        finally: thread.join()
        self.assertFalse(running.cancelled())


if __name__ == '__main__': unittest.main(verbosity=2)
