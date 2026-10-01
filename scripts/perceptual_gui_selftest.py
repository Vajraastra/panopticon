"""Fase pHash Qt explícita, curación conservada y cierre coordinado."""
from concurrent.futures import Future
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication,QPushButton,QPlainTextEdit,QSpinBox
from shiboken6 import delete
from curation_snapshot_selftest import pump
from core.library_repository import LibraryRepository
from modules.duplicate_finder.logic.perceptual_backfill import calculate
import duplicate_gui_selftest as prior
import perceptual_backfill_selftest as images


class PhaseGUI(prior.GUI):
    image=images.Backfill.image
    def enter(self,folders=None):
        self.module.combo_mode.setCurrentIndex(4)
        self.module.phash_panel.folders.setPlainText('\n'.join(folders or [self.root.as_posix()]))
    def run_phase(self):
        self.module.run_scan()
        self.assertTrue(pump(lambda:self.module.phash_worker is None))
        self.assertFalse(self.module._phash_busy)
        return self.module.phash_panel.result

    def test_saving_is_explicit_and_curated_copy_still_wins_exact_review(self):
        raw=self.image('raw.png')
        curated=self.image('curated.png',rating=5,tags=['documented'])
        self.enter()
        self.assertFalse(self.service.read('get_phash_registry_status')['prepared'])
        self.assertIsNone(self.module.phash_worker)
        value=self.run_phase()
        self.assertTrue(value['complete'])
        self.assertEqual(value['persisted'],2)
        self.assertIn('Guardadas: 2',self.module.status.text())
        self.assertIn('Guardadas (confirmadas): 2',self.module.phash_panel.details.toPlainText())
        self.assertTrue(self.module.btn_delete.isHidden())
        self.assertFalse(self.module.btn_delete.isEnabled())
        self.module.combo_mode.setCurrentIndex(0)
        self.scan_gui()
        self.module.auto_select_duplicates()
        self.assertEqual(self.module._checked_paths,{raw.as_posix()})
        self.assertTrue(curated.exists() and raw.exists())

    def test_visual_mode_reuses_verified_cache_without_decode_and_never_enables_retirement(self):
        self.image('a.png')
        self.image('b.png')
        self.enter()
        self.run_phase()
        self.module.combo_mode.setCurrentIndex(1)
        with patch.object(Image,'open',side_effect=AssertionError('cached visual decoded')):
            value=self.scan_gui()
        self.assertTrue(value['complete'])
        self.assertFalse(self.module.btn_delete.isEnabled())
        self.assertFalse(self.module.btn_auto_select.isEnabled())

    def test_unknown_sha_and_pixel_budget_are_reported_without_ambiguous_writes(self):
        path=self.image('unknown.png',register=False)
        self.register(path.as_posix())
        self.enter()
        value=self.run_phase()
        self.assertEqual(value['unknown_images'],1)
        self.assertFalse(value['complete'])
        self.assertIn('Calcula SHA-256',self.module.phash_panel.details.toPlainText())
        self.assertFalse(self.service.read('get_phash_registry_status')['prepared'])
        self.image('known.png')
        self.module.phash_panel.pixels.setValue(10)
        value=self.run_phase()
        self.assertEqual(value['persisted'],0)
        self.assertGreater(value['issue_count'],1)

    def test_scope_or_budget_change_consumes_result_and_does_not_auto_restart(self):
        self.image()
        self.enter()
        self.run_phase()
        self.module.phash_panel.images.setValue(9)
        self.assertIsNone(self.module.phash_panel.result)
        self.assertIsNone(self.module.phash_worker)
        self.assertIn('Elige el alcance explícito',self.module.status.text())
        self.module.phash_panel.folders.clear()
        self.assertFalse(self.module.btn_run.isEnabled())

    def test_double_start_guard_nonblocking_cancel_and_terminal_release_controls(self):
        self.image()
        self.enter()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        threads=[]
        def blocked(record,event,**kwargs):
            threads.append(threading.get_ident())
            entered.set()
            self.assertTrue(release.wait(3))
            return calculate(record,event,**kwargs)
        with patch('modules.duplicate_finder.logic.perceptual_backfill.calculate',blocked):
            self.module.run_scan()
            self.assertTrue(entered.wait(1))
            worker=self.module.phash_worker
            self.module.run_scan()
            self.assertIs(self.module.phash_worker,worker)
            self.assertFalse(self.module.combo_mode.isEnabled())
            self.assertFalse(self.module.phash_panel.folders.isEnabled())
            self.assertFalse(self.module.shutdown_ready())
            ticks=[]
            QTimer.singleShot(0,lambda:ticks.append(True))
            start=time.monotonic()
            self.module.cancel()
            self.assertLess(time.monotonic()-start,.1)
            self.assertTrue(pump(lambda:ticks))
            release.set()
            self.assertTrue(pump(lambda:self.module.phash_worker is None))
        self.assertTrue(self.module.phash_panel.result['cancelled'])
        self.assertTrue(self.module.btn_run.isEnabled())
        self.assertNotIn(threading.get_ident(),threads)

    def test_dashboard_waits_for_accepted_phash_commit_before_closing_owner(self):
        self.image()
        self.enter()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        store=LibraryRepository.store_phash_batch
        def accepted(repo,*args,**kwargs):
            value=store(repo,*args,**kwargs)
            entered.set()
            self.assertTrue(release.wait(3))
            return value
        host=self.dashboard()
        with (patch.object(LibraryRepository,'store_phash_batch',accepted),patch('main.close_library_services') as close_owner):
            self.module.run_scan()
            self.assertTrue(entered.wait(1))
            event=QCloseEvent()
            start=time.monotonic()
            host.closeEvent(event)
            self.assertLess(time.monotonic()-start,.1)
            self.assertFalse(event.isAccepted())
            self.assertFalse(close_owner.called)
            self.assertFalse(self.module.shutdown_ready())
            ticks=[]
            QTimer.singleShot(0,lambda:ticks.append(True))
            self.assertTrue(pump(lambda:ticks))
            release.set()
            self.assertTrue(pump(lambda:host._shutdown_finished))
            close_owner.assert_called_once_with(permanent=True)
        values=self.service.read('get_phash_page',[self.root.as_posix()],images.algorithm_id())
        self.assertIsNotNone(values['records'][0]['phash'])
        self.assertIsNone(self.module.phash_panel.result)  # terminalUI descartado al cerrar.

    def test_destroyed_view_retains_worker_until_finished_without_late_widget_access(self):
        self.image()
        self.enter()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        def blocked(record,event,**kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return calculate(record,event,**kwargs)
        with patch('modules.duplicate_finder.logic.perceptual_backfill.calculate',blocked):
            self.module.run_scan()
            self.assertTrue(entered.wait(1))
            delete(self.view)
            self.assertTrue(self.module._closed)
            release.set()
            self.assertTrue(pump(lambda:self.module.phash_worker is None))
        self.assertTrue(self.module.shutdown_ready())

    def test_programmatic_option_change_drops_stale_terminal_but_keeps_accepted_data(self):
        self.image()
        self.enter()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        store=LibraryRepository.store_phash_batch
        def accepted(repo,*args,**kwargs):
            value=store(repo,*args,**kwargs)
            entered.set()
            self.assertTrue(release.wait(3))
            return value
        with patch.object(LibraryRepository,'store_phash_batch',accepted):
            self.module.run_scan()
            self.assertTrue(entered.wait(1))
            self.module.phash_panel.images.setValue(9)
            release.set()
            self.assertTrue(pump(lambda:self.module.phash_worker is None))
        self.assertIsNone(self.module.phash_panel.result)
        self.assertIsNotNone(self.service.read('get_phash_page',[self.root.as_posix()],images.algorithm_id())['records'][0]['phash'])

    def test_unconfirmed_future_has_human_error_and_zero_confirmed_count(self):
        self.image()
        self.enter()
        submit=self.service.submit
        def unconfirmed(operation,*args,**kwargs):
            if operation=='store_phash_batch':
                future=Future()
                future.set_result(False)
                return future
            return submit(operation,*args,**kwargs)
        with patch.object(self.service,'submit',unconfirmed):value=self.run_phase()
        self.assertEqual(value['persisted'],0)
        self.assertIn('no se confirmó',self.module.status.text())
        self.assertTrue(self.module.btn_run.isEnabled())

    def test_controls_and_result_display_are_localized_and_do_no_gui_sql_or_stat(self):
        self.image()
        self.enter()
        self.run_phase()
        with (patch.object(self.service,'read',side_effect=AssertionError('GUI SQL')),
              patch.object(Path,'stat',side_effect=AssertionError('GUI stat'))):
            self.module.phash_panel.show_result(self.module.phash_panel.result,self.module.reason)
            self.module._controls()
        self.assertEqual(self.module.btn_run.text(),'Calcular y guardar pHash')
        for kind in (QPushButton,QPlainTextEdit,QSpinBox):
            for widget in self.module.phash_panel.findChildren(kind):
                self.assertTrue(widget.toolTip())
                if isinstance(widget,QPushButton):self.assertNotEqual(widget.text(),widget.toolTip())
        self.module.context['data_service']=None
        self.module.run_scan()
        self.assertIsNone(self.module.phash_worker)
        self.assertIn('servicio de biblioteca nativa',self.module.status.text())


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('PhaseGUI.'+name,sys.modules[__name__])
        for name in PhaseGUI.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
