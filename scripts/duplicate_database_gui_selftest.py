"""Modo DB Qt real: candidatos, verificación completa y admisión explícita."""
import hashlib
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QPushButton, QComboBox, QLineEdit, QSlider, QSpinBox
from core.paths import ProjectPaths
from core.library_repository import LibraryRepository
from PySide6.QtGui import QCloseEvent
from shiboken6 import delete
from curation_snapshot_selftest import pump
import duplicate_gui_selftest as prior


class DatabaseGUI(prior.GUI):
    def enter(self):
        self.module.combo_mode.setCurrentIndex(2)
        self.assertTrue(pump(lambda:self.module.native_worker is None))

    def find(self):
        self.module.run_scan()
        self.assertTrue(pump(lambda:self.module.native_worker is None))

    def verify(self,index=0):
        self.module.db_tree.setCurrentItem(self.module.db_tree.topLevelItem(index))
        self.module.verify_database_group()
        self.assertTrue(pump(lambda:self.module.worker is None))
        return self.module.review

    def prepare(self):
        self.module.prepare_database()
        self.assertTrue(pump(lambda:self.module.native_worker is None))
        self.assertTrue(self.module._db_prepared)

    def test_candidates_are_not_proof_then_explicit_preparation_allows_verified_retirement(self):
        raw,curated=self.pair()
        receipt=raw.parent/'catalog.db'
        receipt.write_bytes(b'Cherry URL/hash receipts')
        self.enter()
        self.assertFalse(self.module._db_prepared)
        self.find()
        self.assertEqual(self.module.db_tree.topLevelItemCount(),1)
        self.assertIsNone(self.module.review)
        self.assertFalse(self.module.btn_delete.isEnabled())
        value=self.verify()
        self.assertEqual(value['scope'],'database')
        self.assertTrue(value['complete'])
        report=next(iter(value['reports'].values()))
        self.assertEqual(report['keep'],(curated.as_posix(),))
        self.module.auto_select_duplicates()
        self.assertFalse(self.module.btn_delete.isEnabled())
        self.prepare()
        self.assertIsNone(self.module.review)
        self.assertEqual(self.module._checked_paths,set())
        self.verify()
        self.module.auto_select_duplicates()
        self.assertTrue(self.module.btn_delete.isEnabled())
        self.accepted_retirement()
        self.assertFalse(raw.exists())
        self.assertTrue(curated.exists())
        self.assertEqual(receipt.read_bytes(),b'Cherry URL/hash receipts')
        self.assertEqual(self.events,[raw.parent.as_posix()])
        self.assertEqual(self.module.db_tree.topLevelItemCount(),0)
        self.assertFalse(self.module.btn_delete.isEnabled())

    def test_all_zero_weights_still_prefer_documented_copy(self):
        raw,curated=self.pair()
        self.enter()
        for control in self.module.weights.values():
            control.setValue(0)
        self.find()
        report=next(iter(self.verify()['reports'].values()))
        self.assertEqual(report['keep'],(curated.as_posix(),))
        self.assertEqual(report['remove'],(raw.as_posix(),))

    def test_complementary_curation_never_enables_retirement(self):
        self.file('a.png',tags=['alpha'])
        self.file('b.png',tags=['beta'])
        self.enter()
        self.prepare()
        self.find()
        self.verify()
        self.module.auto_select_duplicates()
        self.assertFalse(self.module.btn_auto_select.isEnabled())
        self.assertFalse(self.module.btn_delete.isEnabled())
        self.assertEqual(self.module._checked_paths,set())

    def test_changed_bytes_disable_retirement_despite_stored_hash(self):
        raw,curated=self.pair()
        self.enter()
        self.find()
        raw.write_bytes(b'changed since indexing')
        value=self.verify()
        self.assertFalse(value['complete'])
        self.assertFalse(self.module.btn_auto_select.isEnabled())
        self.assertFalse(self.module.btn_delete.isEnabled())
        self.assertTrue(curated.exists())

    def test_large_group_is_visible_but_no_prefix_can_be_verified(self):
        rows=[dict(path=(self.root/f'{i}.png').as_posix(),filename=f'{i}.png',size=1,created=1,
            hash_original='a'*64) for i in range(257)]
        self.service.submit('register_files_minimal',rows).result(3)
        self.enter()
        self.find()
        item=self.module.db_tree.topLevelItem(0)
        self.assertEqual(item.data(0,Qt.UserRole)['copies'],257)
        self.module.db_tree.setCurrentItem(item)
        self.assertFalse(self.module.btn_db_verify.isEnabled())
        with patch.object(self.service,'read',side_effect=AssertionError('prefix queried')):
            self.module.verify_database_group()
        self.assertIsNone(self.module.worker)
        self.assertIsNone(self.module.review)

    def test_keyset_group_pages_are_complete_and_previous_requeries_current_catalog(self):
        rows=[]
        for i in range(27):
            digest=hashlib.sha256(str(i).encode()).hexdigest()
            for copy in range(2):
                name=f'{i}-{copy}.png'
                rows.append(dict(path=(self.root/name).as_posix(),filename=name,size=1,created=1,hash_original=digest))
        self.service.submit('register_files_minimal',rows).result(3)
        self.enter()
        self.find()
        first=[self.module.db_tree.topLevelItem(i).data(0,Qt.UserRole)['digest'] for i in range(25)]
        self.assertTrue(self.module.btn_db_next.isEnabled())
        self.module._db_page_request(1)
        self.assertTrue(pump(lambda:self.module.native_worker is None))
        self.assertEqual(self.module.db_tree.topLevelItemCount(),2)
        self.assertEqual(self.module._db_page,1)
        self.assertFalse(self.module.btn_db_next.isEnabled())
        second=[self.module.db_tree.topLevelItem(i).data(0,Qt.UserRole)['digest'] for i in range(2)]
        self.assertFalse(set(first)&set(second))
        self.module._db_page_request(-1)
        self.assertTrue(pump(lambda:self.module.native_worker is None))
        self.assertEqual([self.module.db_tree.topLevelItem(i).data(0,Qt.UserRole)['digest'] for i in range(25)],first)

    def test_native_query_is_off_gui_guarded_responsive_and_stale_results_are_dropped(self):
        self.pair()
        self.enter()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        native=self.service.read
        main=threading.get_ident()
        threads=[]
        def blocked(operation,*args,**kwargs):
            threads.append(threading.get_ident())
            entered.set()
            self.assertTrue(release.wait(3))
            return native(operation,*args,**kwargs)
        with patch.object(self.service,'read',blocked):
            self.module.run_scan()
            self.assertTrue(entered.wait(1))
            worker=self.module.native_worker
            self.module.run_scan()
            self.assertIs(self.module.native_worker,worker)
            self.assertFalse(self.module.btn_run.isEnabled())
            self.assertFalse(self.module.shutdown_ready())
            ticks=[]
            QTimer.singleShot(0,lambda:ticks.append(True))
            self.assertTrue(pump(lambda:ticks))
            self.module._configuration_changed()
            release.set()
            self.assertTrue(pump(lambda:self.module.native_worker is None))
        self.assertTrue(all(thread!=main for thread in threads))
        self.assertEqual(self.module.db_tree.topLevelItemCount(),0)
        self.assertTrue(self.module.shutdown_ready())

    def test_closed_view_drops_late_query_without_blocking_or_destroying_worker(self):
        self.pair()
        self.enter()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        native=self.service.read
        def blocked(operation,*args,**kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return native(operation,*args,**kwargs)
        with patch.object(self.service,'read',blocked):
            self.module.run_scan()
            self.assertTrue(entered.wait(1))
            started=time.monotonic()
            self.module.on_unload()
            self.assertLess(time.monotonic()-started,.1)
            self.assertFalse(self.module.shutdown_ready())
            release.set()
            self.assertTrue(pump(lambda:self.module.native_worker is None))
        self.assertEqual(self.module.db_tree.topLevelItemCount(),0)
        self.assertTrue(self.module.shutdown_ready())

    def test_cancelled_read_has_terminal_and_releases_controls(self):
        self.pair()
        self.enter()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        native=self.service.read
        def blocked(operation,*args,**kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return native(operation,*args,**kwargs)
        with patch.object(self.service,'read',blocked):
            self.module.run_scan()
            self.assertTrue(entered.wait(1))
            self.module.cancel()
            release.set()
            self.assertTrue(pump(lambda:self.module.native_worker is None))
        self.assertTrue(self.module.btn_run.isEnabled())
        self.assertIsNone(self.module.review)
        self.assertFalse(self.module._native_busy)

    def test_preparation_is_explicit_and_idempotent_and_options_consume_approval(self):
        self.pair()
        self.enter()
        self.assertFalse(self.service.read('get_duplicate_search_status')['prepared'])
        self.prepare()
        self.assertTrue(self.module.btn_db_prepare.isEnabled())
        self.prepare()
        self.find()
        self.verify()
        self.module.auto_select_duplicates()
        self.assertTrue(self.module.btn_delete.isEnabled())
        self.module.weights['manual_tag_weight'].setValue(91)
        self.assertFalse(self.module.btn_delete.isEnabled())
        self.assertEqual(self.module._checked_paths,set())

    def test_candidate_selection_and_details_do_no_sql_or_filesystem_on_gui(self):
        raw,curated=self.pair()
        self.enter()
        self.find()
        with (patch.object(self.service,'read',side_effect=AssertionError('GUI SQL')),
              patch.object(Path,'stat',side_effect=AssertionError('GUI filesystem'))):
            self.module.db_tree.setCurrentItem(self.module.db_tree.topLevelItem(0))
        self.verify()
        with (patch.object(self.service,'read',side_effect=AssertionError('GUI SQL')),
              patch.object(Path,'stat',side_effect=AssertionError('GUI filesystem'))):
            self.module.tree.setCurrentItem(self.child(curated))
            self.assertIn('documented',self.module.details.toPlainText())

    def test_database_controls_have_meaningful_localized_tooltips(self):
        self.enter()
        for kind in (QPushButton,QComboBox,QLineEdit,QSlider,QSpinBox):
            for widget in self.view.findChildren(kind):
                self.assertTrue(widget.toolTip(),type(widget).__name__)
                if isinstance(widget,QPushButton):
                    self.assertNotEqual(widget.text(),widget.toolTip())
        self.assertEqual(self.module.btn_db_prepare.text(),'Preparar índice')
        self.assertIn('30 segundos',self.module.btn_db_prepare.toolTip())
        self.assertIn('todas las copias',self.module.lbl_folder.text())

    def test_accepted_index_build_commits_after_cancel_and_keeps_qt_responsive(self):
        self.pair()
        self.enter()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        prepare=LibraryRepository.prepare_duplicate_search
        def blocked(repo):
            value=prepare(repo)
            entered.set()
            self.assertTrue(release.wait(3))
            return value
        with patch.object(LibraryRepository,'prepare_duplicate_search',blocked):
            self.module.prepare_database()
            self.assertTrue(entered.wait(1))
            self.module.cancel()
            ticks=[]
            QTimer.singleShot(0,lambda:ticks.append(True))
            self.assertTrue(pump(lambda:ticks))
            self.assertFalse(self.module.shutdown_ready())
            release.set()
            self.assertTrue(pump(lambda:self.module.native_worker is None))
        self.assertTrue(self.module._db_prepared)
        self.assertTrue(self.service.read('get_duplicate_search_status')['prepared'])

    def test_dashboard_waits_for_accepted_index_commit_before_closing_owner(self):
        self.pair()
        self.enter()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        prepare=LibraryRepository.prepare_duplicate_search
        def blocked(repo):
            value=prepare(repo)
            entered.set()
            self.assertTrue(release.wait(3))
            return value
        host=self.dashboard()
        with (patch.object(LibraryRepository,'prepare_duplicate_search',blocked),
              patch('main.close_library_services') as close_owner):
            self.module.prepare_database()
            self.assertTrue(entered.wait(1))
            event=QCloseEvent()
            started=time.monotonic()
            host.closeEvent(event)
            self.assertLess(time.monotonic()-started,.1)
            self.assertFalse(event.isAccepted())
            self.assertFalse(close_owner.called)
            ticks=[]
            QTimer.singleShot(0,lambda:ticks.append(True))
            self.assertTrue(pump(lambda:ticks))
            release.set()
            self.assertTrue(pump(lambda:host._shutdown_finished))
            close_owner.assert_called_once_with(permanent=True)
        self.assertTrue(self.service.read('get_duplicate_search_status')['prepared'])
        self.assertFalse(self.module._db_prepared)  # Terminal tardío descartado tras cerrar.

    def test_destroyed_view_keeps_running_query_alive_and_drops_late_ui_work(self):
        self.pair()
        self.enter()
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        native=self.service.read
        def blocked(operation,*args,**kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return native(operation,*args,**kwargs)
        with patch.object(self.service,'read',blocked):
            self.module.run_scan()
            self.assertTrue(entered.wait(1))
            delete(self.view)
            self.assertTrue(self.module._closed)
            self.assertFalse(self.module.shutdown_ready())
            release.set()
            self.assertTrue(pump(lambda:self.module.native_worker is None))
        self.assertTrue(self.module.shutdown_ready())

    def test_query_error_clears_candidates_and_reenables_retry(self):
        self.pair()
        self.enter()
        self.find()
        with patch.object(self.service,'read',side_effect=ValueError('duplicate_discovery_budget_exceeded')):
            self.find()
        self.assertEqual(self.module.db_tree.topLevelItemCount(),0)
        self.assertFalse(self.module.btn_db_verify.isEnabled())
        self.assertFalse(self.module.btn_db_next.isEnabled())
        self.assertTrue(self.module.btn_run.isEnabled())
        self.assertIn('plazo de lectura',self.module.db_status.text())


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('DatabaseGUI.'+name,sys.modules[__name__])
        for name in DatabaseGUI.__dict__ if name.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
