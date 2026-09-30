"""Snapshots del optimizador con SQLite temporal y Qt; sin biblioteca real."""
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton, QComboBox, QSpinBox, QCheckBox
from core.library_repository import LibraryRepository
from core.theme_manager import ThemeManager
from modules.image_optimizer import module as product
import curation_snapshot_selftest as helpers


def optimized(*args, **kwargs):
    return dict(success=True, saved_bytes=1)


class OptimizerData(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.workers = []
        self.modules = []
        self.releases = []
        self.addCleanup(self.shutdown)

    def shutdown(self):
        for event in self.releases:
            event.set()
        for module in self.modules:
            module.on_unload()
            if module.worker is not None:
                self.workers.append(module.worker)
        for worker in self.workers:
            worker.stop()
            self.assertTrue(worker.wait(3000), 'worker sin cerrar')
        QApplication.processEvents()
        for module in self.modules:
            module.view.close()
            module.view.deleteLater()
        QApplication.processEvents()

    def settings(self):
        return dict(format=None, quality=90, max_side=None, preserve_meta=True,
                    export_path=str(Path(self.temp.name) / 'output'))

    def worker(self, paths, settings=None):
        worker = product.OptimizerWorker(paths, settings or self.settings(), self.service)
        self.workers.append(worker)
        return worker

    def run_worker(self, worker):
        results = []
        worker.finished_signal.connect(results.append)
        worker.start()
        self.assertTrue(helpers.pump(lambda: bool(results)))
        self.assertTrue(worker.wait(3000))
        return results[0]

    def view(self, paths):
        module = product.ImageOptimizerModule()
        module.on_load(dict(data_service=self.service, theme_manager=ThemeManager()))
        module.get_view()
        module.load_image_set(paths)
        self.modules.append(module)
        return module

    def start(self, module):
        with patch.object(QFileDialog, 'getExistingDirectory', return_value=self.settings()['export_path']):
            module._run_all()

    def block_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        real = LibraryRepository.get_curation_snapshot
        def blocked(repo, paths):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('fixture release')
            return real(repo, paths)
        return entered, release, blocked

    def test_thousand_paths_use_four_reads_eight_selects_off_gui(self):
        paths = [f'C:/fixture/{i}.png' for i in range(1000)]
        self.register(*paths)
        from core import library_service
        real_open, real_read = library_service.open_reader, self.service.read
        sql, batches, threads = [], [], []
        def reader(path):
            conn = real_open(path)
            conn.set_trace_callback(sql.append)
            return conn
        def read(operation, values, **kwargs):
            self.assertEqual(operation, 'get_curation_snapshot')
            batches.append(len(values))
            threads.append(threading.get_ident())
            return real_read(operation, values, **kwargs)
        worker = self.worker(paths)
        with patch.object(library_service, 'open_reader', reader), patch.object(self.service, 'read', read), \
                patch.object(product, 'optimize_image', side_effect=optimized) as export:
            result = self.run_worker(worker)
        self.assertEqual(batches, [256, 256, 256, 232])
        self.assertEqual(sum(s.lstrip().upper().startswith('SELECT') for s in sql), 8)
        self.assertTrue(all(t != threading.get_ident() for t in threads))
        self.assertEqual((export.call_count, result['processed']), (1000, 1000))

    def test_tags_and_rating_share_transaction_during_commit(self):
        path = 'C:/fixture/a.png'
        self.register(path)
        self.service.submit('edit_curation', path, add_tags=['old'], rating=2).result(3)
        from core import library_service
        real_open = library_service.open_reader
        def reader(db_path):
            conn = real_open(db_path)
            def trace(sql):
                if sql.startswith('SELECT ft.file_id'):
                    self.service.submit('edit_curation', path, add_tags=['new'], rating=5).result(3)
            conn.set_trace_callback(trace)
            return conn
        with patch.object(library_service, 'open_reader', reader), \
                patch.object(product, 'optimize_image', side_effect=optimized) as export:
            self.run_worker(self.worker([path]))
        self.assertEqual(export.call_args.kwargs['tags'], ['old'])
        self.assertEqual(export.call_args.kwargs['rating'], 2)
        self.assertEqual(self.service.read('get_curation_snapshot', [path])[0]['rating'], 5)

    def test_duplicates_normalization_empty_and_external_do_not_register(self):
        self.register('C:/fixture/a.png', 'C:/fixture/b.png')
        self.service.submit('edit_curation', 'C:/fixture/b.png', add_tags=['alpha'], rating=4).result(3)
        paths = ['C:\\fixture\\b.png', 'C:/external.png', 'C:/fixture/a.png', 'C:/fixture/b.png']
        with patch.object(product, 'optimize_image', side_effect=optimized) as export:
            self.run_worker(self.worker(paths))
        self.assertEqual([c.args[0] for c in export.call_args_list], paths)
        self.assertEqual([(c.kwargs['tags'], c.kwargs['rating']) for c in export.call_args_list],
                         [(['alpha'], 4), ([], 0), ([], 0), (['alpha'], 4)])
        self.assertEqual(self.service.read('get_library_overview')['count'], 2)

    def test_worker_captures_paths_and_settings(self):
        paths, settings = ['C:/first.png'], self.settings()
        worker = self.worker(paths, settings)
        paths.append('C:/later.png')
        settings['quality'] = 1
        with patch.object(product, 'optimize_image', side_effect=optimized) as export:
            self.run_worker(worker)
        self.assertEqual(export.call_count, 1)
        self.assertEqual(export.call_args.kwargs['quality'], 90)

    def test_first_read_error_creates_no_output_and_keeps_queue_for_retry(self):
        module = self.view(['C:/a.png', 'C:/b.png'])
        with patch.object(LibraryRepository, 'get_curation_snapshot', side_effect=RuntimeError('locked fixture')), \
                patch.object(product, 'optimize_image', side_effect=optimized) as export, \
                patch.object(QMessageBox, 'warning') as warning:
            self.start(module)
            self.assertTrue(helpers.pump(lambda: warning.called))
            module.worker.wait(3000)
        self.assertEqual(export.call_count, 0)
        self.assertEqual(module.queue, ['C:/a.png', 'C:/b.png'])
        self.assertTrue(module.btn_run.isEnabled())
        self.assertIn('locked fixture', warning.call_args.args[2])
        with patch.object(product, 'optimize_image', side_effect=optimized) as export, \
                patch.object(QMessageBox, 'information') as info:
            self.start(module)
            self.assertTrue(helpers.pump(lambda: info.called))
            module.worker.wait(3000)
        self.assertEqual(export.call_count, 2)
        self.assertEqual(module.queue, [])

    def test_second_read_error_preserves_only_unprocessed_tail(self):
        paths = [f'C:/fixture/{i}.png' for i in range(300)]
        module = self.view(paths)
        real = LibraryRepository.get_curation_snapshot
        calls = []
        def read(repo, batch):
            calls.append(batch)
            if len(calls) == 2:
                raise RuntimeError('second block fixture')
            return real(repo, batch)
        with patch.object(LibraryRepository, 'get_curation_snapshot', read), \
                patch.object(product, 'optimize_image', side_effect=optimized) as export, \
                patch.object(QMessageBox, 'warning') as warning:
            self.start(module)
            self.assertTrue(helpers.pump(lambda: warning.called))
            module.worker.wait(3000)
        self.assertEqual(export.call_count, 256)
        self.assertEqual(module.queue, paths[256:])
        self.assertIn('256', warning.call_args.args[2])

    def test_qt_responds_to_blocked_read_and_duplicate_start_is_ignored(self):
        module = self.view(['C:/a.png'])
        entered, release, blocked = self.block_snapshot()
        beats = []
        with patch.object(LibraryRepository, 'get_curation_snapshot', blocked), \
                patch.object(product, 'optimize_image', side_effect=optimized), \
                patch.object(QMessageBox, 'information') as info:
            started = time.monotonic()
            self.start(module)
            self.assertLess(time.monotonic() - started, .3)
            self.assertTrue(entered.wait(1))
            self.assertFalse(module.btn_run.isEnabled())
            worker = module.worker
            with patch.object(QFileDialog, 'getExistingDirectory') as dialog:
                module._run_all()
                dialog.assert_not_called()
            self.assertIs(module.worker, worker)
            QTimer.singleShot(0, lambda: beats.append(True))
            self.assertTrue(helpers.pump(lambda: bool(beats)))
            release.set()
            self.assertTrue(helpers.pump(lambda: info.called))

    def test_incoming_replacement_is_not_cleared_by_old_completion(self):
        module = self.view(['C:/old.png'])
        entered, release, blocked = self.block_snapshot()
        with patch.object(LibraryRepository, 'get_curation_snapshot', blocked), \
                patch.object(product, 'optimize_image', side_effect=optimized) as export, \
                patch.object(QMessageBox, 'information') as info:
            self.start(module)
            self.assertTrue(entered.wait(1))
            module.load_image_set(['C:/new.png'])
            release.set()
            self.assertTrue(helpers.pump(lambda: info.called))
        self.assertEqual(module.queue, ['C:/new.png'])
        self.assertEqual(export.call_args.args[0], 'C:/old.png')

    def test_appended_images_survive_captured_batch(self):
        module = self.view(['C:/old.png'])
        entered, release, blocked = self.block_snapshot()
        with patch.object(LibraryRepository, 'get_curation_snapshot', blocked), \
                patch.object(product, 'optimize_image', side_effect=optimized), \
                patch.object(QMessageBox, 'information') as info:
            self.start(module)
            self.assertTrue(entered.wait(1))
            module._on_files_dropped(['C:/new.png'])
            release.set()
            self.assertTrue(helpers.pump(lambda: info.called))
        self.assertEqual(module.queue, ['C:/new.png'])

    def test_read_error_keeps_pending_from_old_and_incoming_sets(self):
        module = self.view(['C:/old.png'])
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def fail(repo, paths):
            entered.set()
            self.assertTrue(release.wait(3))
            raise RuntimeError('fixture')
        with patch.object(LibraryRepository, 'get_curation_snapshot', fail), \
                patch.object(product, 'optimize_image', side_effect=optimized) as export, \
                patch.object(QMessageBox, 'warning') as warning:
            self.start(module)
            self.assertTrue(entered.wait(1))
            module.load_image_set(['C:/new.png'])
            release.set()
            self.assertTrue(helpers.pump(lambda: warning.called))
        self.assertFalse(export.called)
        self.assertEqual(module.queue, ['C:/old.png', 'C:/new.png'])

    def test_unload_cancels_read_without_qt_wait_or_late_export(self):
        module = self.view(['C:/a.png'])
        entered, release, blocked = self.block_snapshot()
        with patch.object(LibraryRepository, 'get_curation_snapshot', blocked), \
                patch.object(product, 'optimize_image', side_effect=optimized) as export, \
                patch.object(QMessageBox, 'information') as info, patch.object(QMessageBox, 'warning') as warning:
            self.start(module)
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            module.on_unload()
            self.assertLess(time.monotonic() - started, .1)
            release.set()
            self.assertTrue(module.worker.wait(3000))
            QApplication.processEvents()
        self.assertFalse(info.called or warning.called or export.called)
        self.assertEqual(module.queue, ['C:/a.png'])

    def test_cancel_during_export_stops_before_next_file(self):
        worker = self.worker(['C:/a.png', 'C:/b.png'])
        def export(*args, **kwargs):
            worker.stop()
            return optimized()
        with patch.object(product, 'optimize_image', side_effect=export) as exported:
            result = self.run_worker(worker)
        self.assertEqual(exported.call_count, 1)
        self.assertEqual(result['processed'], 1)
        self.assertTrue(result['cancelled'])

    def test_dialog_cancel_and_unload_during_dialog_start_no_worker(self):
        module = self.view(['C:/a.png'])
        with patch.object(QFileDialog, 'getExistingDirectory', return_value=''):
            module._run_all()
        self.assertIsNone(module.worker)
        def close(*args):
            module.on_unload()
            return self.settings()['export_path']
        with patch.object(QFileDialog, 'getExistingDirectory', side_effect=close):
            module._run_all()
        self.assertIsNone(module.worker)

    def test_short_provider_response_is_error_not_silent_truncation(self):
        with patch.object(self.service, 'read', return_value=[]), \
                patch.object(product, 'optimize_image', side_effect=optimized) as export:
            result = self.run_worker(self.worker(['C:/a.png']))
        self.assertEqual(result['data_error'], 'Incomplete curation snapshot')
        self.assertFalse(export.called)

    def test_error_without_message_still_warns_and_keeps_pending(self):
        module = self.view(['C:/a.png'])
        with patch.object(LibraryRepository, 'get_curation_snapshot', side_effect=RuntimeError()), \
                patch.object(product, 'optimize_image', side_effect=optimized) as export, \
                patch.object(QMessageBox, 'warning') as warning, patch.object(QMessageBox, 'information') as info:
            self.start(module)
            self.assertTrue(helpers.pump(lambda: warning.called))
        self.assertFalse(info.called or export.called)
        self.assertEqual(module.queue, ['C:/a.png'])

    def test_terminal_signal_pending_cannot_be_overwritten_by_second_start(self):
        module = self.view(['C:/a.png'])
        with patch.object(product, 'optimize_image', side_effect=optimized), \
                patch.object(QMessageBox, 'information') as info:
            self.start(module)
            self.assertTrue(module.worker.wait(3000))
            # Qt aún no recibió la señal terminal del primer worker.
            with patch.object(QFileDialog, 'getExistingDirectory') as dialog:
                module._run_all()
                dialog.assert_not_called()
            self.assertTrue(helpers.pump(lambda: info.called))

    def test_real_png_output_preserves_original_bytes_and_database(self):
        from PIL import Image
        source = Path(self.temp.name) / 'source.png'
        Image.new('RGB', (16, 16), 'green').save(source)
        original = source.read_bytes()
        self.register(str(source))
        self.service.submit('edit_curation', str(source), add_tags=['alpha'], rating=3).result(3)
        result = self.run_worker(self.worker([str(source)]))
        self.assertEqual(result['success'], 1)
        self.assertEqual(source.read_bytes(), original)
        output = Path(self.settings()['export_path']) / 'source.png'
        self.assertTrue(output.exists())
        from core.metadata import MetadataExtractor
        bundle = MetadataExtractor.extract(output)
        self.assertIn('alpha', bundle.tags)
        self.assertEqual(bundle.rating, 3)
        self.assertEqual(self.service.read('get_library_overview')['count'], 1)


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main()
