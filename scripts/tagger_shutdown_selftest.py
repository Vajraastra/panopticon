"""Private Windows/Qt Tagger shutdown, stale results and terminal failures."""
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
from shiboken6 import isValid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image
from PySide6.QtCore import QThread, Signal, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QDialog, QWidget
from core.paths import CachePaths, ProjectPaths
from core.theme_manager import ThemeManager
from modules.dataset_tagger.module import DatasetTaggerModule
from modules.dataset_tagger.ui import tagger_view as tagger, source_grid, review_view, bbox_editor
from modules.dataset_tagger.ui.worker_lifecycle import _LIVE_SCOPES
from modules.dataset_tagger.logic import tag_db, refine, caption_worker
from modules.dataset_tagger.logic.ideogram.ig4_worker import Ig4Worker


def pump(predicate, seconds=3):
    deadline = time.monotonic()+seconds
    while time.monotonic()<deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(.003)
    return False


class HeldWorker(QThread):
    result = Signal(str)

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def run(self):
        self.result.emit('result before native cleanup')
        self.entered.set()
        self.release.wait(4)


class TaggerShutdown(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(ProjectPaths, 'root', return_value=self.root))
        self.stack.enter_context(patch.object(CachePaths, 'get_models_root', return_value=self.root/'models'))
        self.stack.enter_context(patch.object(tag_db, 'is_ready', return_value=True))
        self.open_folder = self.stack.enter_context(patch.object(CachePaths, 'open_folder'))
        self.context = dict(theme_manager=ThemeManager())
        self.module = DatasetTaggerModule()
        self.module.on_load(self.context)
        self.view = self.module.get_view()
        self.widgets = [self.view]
        self.threads = []
        self.releases = []
        self.image = self.root/'a.png'
        Image.new('RGB', (24,24)).save(self.image)
        self.addCleanup(self.cleanup)

    def test_partial_capture_error_remains_in_final_summary(self):
        self.view._batch_failure=None
        self.view._on_error(str(self.image),'private Native failure; caption_pair_requires_review')
        self.view._on_finished(0,1)
        self.assertIn('private Native failure',self.view.status.text())
        self.assertIn('caption_pair_requires_review',self.view.status.toolTip())

    def cleanup(self):
        for event in self.releases:
            event.set()
        for worker in self.threads:
            if isValid(worker) and worker.isRunning():
                self.assertTrue(worker.wait(3000))
        self.module.on_unload()
        for widget in self.widgets:
            widget.close()
            self.assertTrue(pump(widget.shutdown_ready))
            widget.deleteLater()
        self.assertTrue(pump(lambda: not _LIVE_SCOPES))
        QApplication.processEvents()

    def held(self, widget=None, attribute='_caption', callback=None):
        widget = widget or self.view
        worker = HeldWorker()
        setattr(widget, attribute, worker)
        if callback is not None:
            widget._workers.connect(worker, worker.result, callback)
        self.releases.append(worker.release)
        self.threads.append(worker)
        widget._workers.start(worker, attribute)
        self.assertTrue(worker.entered.wait(1))
        return worker

    def review(self):
        widget = review_view.ReviewView(self.context, as_tag=False,
                                        endpoint='private://mock', vlm_model='mock')
        widget.setParent(self.view)
        self.view._editors.append(widget)
        self.widgets.append(widget)
        return widget

    def editor(self):
        widget = bbox_editor.BBoxEditor(self.image, self.root/'drafts'/'a.pano.json', parent=self.view)
        self.view._editors.append(widget)
        self.widgets.append(widget)
        self.assertTrue(pump(lambda: not widget._loading))
        return widget

    def test_result_is_not_native_completion_or_permission_to_restart(self):
        results=[]
        worker=self.held(callback=results.append)
        self.assertTrue(pump(lambda: bool(results)))
        self.assertIs(self.view._caption, worker)
        self.assertFalse(self.module.shutdown_ready())
        with patch.object(tagger, 'CaptionWorker') as factory:
            self.view._run()
        factory.assert_not_called()
        worker.release.set()
        self.assertTrue(pump(lambda: self.view._caption is None))

    def test_delivery_runs_on_gui_thread(self):
        threads=[]
        self.held(callback=lambda *args: threads.append(threading.get_ident()))
        self.assertTrue(pump(lambda: bool(threads)))
        self.assertEqual(threads, [threading.get_ident()])

    def test_close_does_not_wait_and_keeps_view_alive(self):
        worker=self.held()
        self.view.show()
        start=time.monotonic()
        self.assertFalse(self.view.close())
        self.assertLess(time.monotonic()-start,.1)
        self.assertTrue(self.view.isVisible())
        self.assertTrue(worker.cancelled)
        ticks=[]
        QTimer.singleShot(0, lambda: ticks.append(True))
        self.assertTrue(pump(lambda: bool(ticks)))
        self.assertFalse(self.module.shutdown_ready())
        worker.release.set()
        self.assertTrue(pump(lambda: not self.view.isVisible()))
        self.assertTrue(self.module.shutdown_ready())

    def test_shutdown_suppresses_queued_results_and_new_inputs(self):
        results=[]
        worker=self.held(callback=results.append)
        self.module.on_unload()
        self.module.load_image_set([str(self.image)])
        self.view._discover()
        self.view._test_connection()
        QApplication.processEvents()
        self.assertEqual(results, [])
        self.assertIsNone(self.view._images)
        self.assertIsNone(self.view._discovery)
        self.assertIsNone(self.view._conn)
        self.assertFalse(self.module.shutdown_ready())
        worker.release.set()
        self.assertTrue(pump(self.module.shutdown_ready))

    def test_worker_admission_stays_closed(self):
        self.module.on_unload()
        worker=HeldWorker()
        with self.assertRaisesRegex(RuntimeError,'closed'):
            self.view._workers.start(worker,'_caption')
        worker.deleteLater()

    def test_all_tagger_worker_kinds_participate(self):
        attrs=('_discovery','_conn','_wd_dl','_caption','_ig4','_autobatch')
        workers=[self.held(attribute=attr) for attr in attrs]
        self.module.on_unload()
        self.assertFalse(self.module.shutdown_ready())
        self.assertTrue(all(w.cancelled for w in workers))
        for worker in workers:
            worker.release.set()
        self.assertTrue(pump(self.module.shutdown_ready))
        self.assertTrue(all(getattr(self.view,attr) is None for attr in attrs))

    def test_review_saves_once_while_both_workers_drain(self):
        review=self.review()
        caption=self.root/'a.txt'
        caption.write_text('old caption',encoding='utf-8')
        review._select_caption(str(caption))
        review.editor.setPlainText('edited caption')
        workers=[self.held(review, attr) for attr in ('_tagdb_worker','_refine_worker')]
        with patch.object(review_view.tag_tools,'write',wraps=review_view.tag_tools.write) as writes:
            self.module.on_unload()
            review.close()
            self.assertFalse(self.module.shutdown_ready())
            for worker in workers:
                worker.release.set()
            self.assertTrue(pump(self.module.shutdown_ready))
            review.close()
        self.assertEqual(writes.call_count,1)
        self.assertEqual(caption.read_text(encoding='utf-8'),'edited caption')

    def test_multiple_review_windows_are_retained(self):
        first=self.review(); second=self.review()
        workers=[self.held(window,'_refine_worker') for window in (first,second)]
        self.module.on_unload()
        workers[0].release.set()
        self.assertTrue(pump(first.shutdown_ready))
        self.assertFalse(self.module.shutdown_ready())
        workers[1].release.set()
        self.assertTrue(pump(self.module.shutdown_ready))

    def test_dialog_accept_reject_escape_defer_until_native_finish(self):
        for intent in ('accept','reject','close'):
            with self.subTest(intent=intent):
                editor=self.editor()
                worker=self.held(editor,'_autodet')
                editor.show()
                start=time.monotonic()
                getattr(editor,intent)()
                self.assertLess(time.monotonic()-start,.1)
                self.assertTrue(editor.isVisible())
                self.assertFalse(editor.shutdown_ready())
                worker.release.set()
                self.assertTrue(pump(lambda: not editor.isVisible()))
                self.assertEqual(editor.result(), QDialog.Accepted if intent=='accept' else QDialog.Rejected)

    def test_refinement_cannot_write_after_close_or_reopen_output(self):
        review=self.review()
        review.editor.setPlainText('original')
        worker=self.held(review,'_refine_worker',review._on_refine_ready)
        self.module.on_unload()
        with patch.object(review_view,'RefineProposalDialog') as proposal:
            QApplication.processEvents()
        proposal.assert_not_called()
        self.assertEqual(review.editor.toPlainText(),'original')
        self.open_folder.assert_not_called()
        worker.release.set()

    def test_changed_review_context_discards_proposal(self):
        review=self.review()
        review.editor.setPlainText('original')
        review._refine_snapshot=review._suggestion_state()
        review.editor.setPlainText('new user text')
        with patch.object(review_view,'RefineProposalDialog') as proposal:
            review._on_refine_ready('obsolete suggestion')
        proposal.assert_not_called()
        self.assertEqual(review.editor.toPlainText(),'new user text')

    def test_changed_context_inside_proposal_is_revalidated(self):
        review=self.review()
        review.editor.setPlainText('original')
        review._refine_snapshot=review._suggestion_state()
        with patch.object(review_view,'RefineProposalDialog') as proposal:
            proposal.return_value.exec.side_effect=lambda: (review.editor.setPlainText('new edit') or True)
            proposal.return_value.result_text.return_value='obsolete suggestion'
            review._on_refine_ready('suggestion')
        self.assertEqual(review.editor.toPlainText(),'new edit')

    def test_connection_failure_emits_terminal_result(self):
        with patch.object(tagger,'OpenAICompatProvider') as provider:
            provider.return_value.test_connection.side_effect=OSError('fixture failure')
            worker=tagger.ConnectionWorker('private://mock','')
            results=[]
            worker.result.connect(lambda *args: results.append(args))
            worker.start()
            self.assertTrue(worker.wait(3000))
            self.assertTrue(pump(lambda: bool(results)))
        self.assertEqual(results,[(False,'fixture failure',[])])
        worker.deleteLater()

    def test_double_connection_and_discovery_do_not_replace_thread(self):
        for attr,method in (('_discovery','_discover'),('_conn','_test_connection')):
            worker=self.held(attribute=attr)
            getattr(self.view,method)()
            self.assertIs(getattr(self.view,attr),worker)
            worker.release.set()

    def test_grid_replacement_is_bounded_and_discards_same_path_stale_result(self):
        grid=self.view.grid
        gate=threading.Event(); entered=threading.Event()
        self.releases.append(gate)
        created=[]
        class Thumb(QThread):
            ready=Signal(str,object)
            def __init__(self,paths):
                super().__init__();self.paths=paths;self.cancelled=False;created.append(self)
            def cancel(self): self.cancelled=True
            def run(self):
                if len(created)==1:
                    entered.set();gate.wait(4)
                image=QImage(2,2,QImage.Format_RGB32)
                image.fill(0xFF0000 if self is created[0] else 0x00FF00)
                self.ready.emit(self.paths[0],image)
        with patch.object(source_grid,'ThumbnailWorker',Thumb):
            grid.show_images([str(self.image)])
            self.threads.append(created[0])
            self.assertTrue(entered.wait(1))
            for _ in range(20):
                start=time.monotonic();grid.show_images([str(self.image)])
                self.assertLess(time.monotonic()-start,.1)
            self.assertEqual(len(created),1)
            self.assertEqual(len(grid._workers.workers),1)
            gate.set()
            self.assertTrue(pump(lambda: len(created)==2 and grid._worker is None))
            self.assertEqual(grid._thumb_by_path[str(self.image)].pixelColor(0,0).green(),255)
            self.assertEqual(grid._thumb_by_path[str(self.image)].pixelColor(0,0).red(),0)

    def test_grid_stop_alone_does_not_claim_thread_is_done(self):
        worker=self.held(self.view.grid,'_worker')
        start=time.monotonic();self.view.grid.stop()
        self.assertLess(time.monotonic()-start,.1)
        self.assertIs(self.view.grid._worker,worker)
        self.module.on_unload()
        self.assertFalse(self.module.shutdown_ready())
        worker.release.set()
        self.assertTrue(pump(self.module.shutdown_ready))

    def test_ui_signals_still_work_without_worker_admission(self):
        changes=[]
        with patch.object(self.view,'_refresh_engine_ui',side_effect=lambda:changes.append(True)):
            self.view.combo_tags_engine.setCurrentIndex(1)
        self.assertTrue(changes)

    def test_caption_publication_failure_has_one_terminal_result(self):
        provider=MagicMock();provider.caption.return_value='caption'
        worker=caption_worker.CaptionWorker(provider,'mock',str(self.root),['flux'],images=[str(self.image)])
        errors=[];terminals=[]
        worker.error.connect(lambda *args: errors.append(args))
        worker.finished_all.connect(lambda *args:terminals.append(args))
        with patch.object(caption_worker.CaptionPair,'publish',side_effect=OSError('private publish failure')):
            worker.start();self.assertTrue(worker.wait(3000))
            self.assertTrue(pump(lambda:bool(terminals)))
        self.assertEqual(terminals,[(0,1)])
        self.assertTrue(any('private publish failure' in message for _,message in errors))
        self.assertTrue(self.image.exists())
        worker.deleteLater()

    def test_shared_ideogram_style_failure_has_terminal_result(self):
        worker=Ig4Worker(MagicMock(),'mock',[(str(self.image),'private.json')],infer_style=True,
                         prompt_hld='mock',prompt_background='mock',prompt_obj='mock')
        results=[];errors=[]
        worker.finished_all.connect(lambda *args:results.append(args))
        worker.error.connect(lambda *args:errors.append(args))
        with patch.object(worker,'_infer_shared_style',side_effect=OSError('private style failure')):
            worker.start();self.assertTrue(worker.wait(3000))
            self.assertTrue(pump(lambda:bool(results)))
        self.assertEqual(results,[(0,0)])
        self.assertEqual(errors,[('','private style failure')])
        worker.deleteLater()

    def test_fatal_caption_failure_is_not_shown_as_success(self):
        self.view._on_batch_failed('private failure')
        status=self.view.status.text()
        self.view._on_finished(0,0)
        self.assertEqual(self.view.status.text(),status)
        self.assertIn('private failure',status)
        self.open_folder.assert_not_called()

    def test_closed_ready_editors_are_released_on_next_open(self):
        previous=self.review()
        previous.close()
        current=review_view.ReviewView(self.context,as_tag=False)
        self.view._retain_editor(current)
        self.assertNotIn(previous,self.view._editors)
        self.assertEqual(self.view._editors,[current])
        self.widgets.remove(previous)
        self.widgets.append(current)

    def test_actual_box_detection_finishes_without_applying_after_reject(self):
        from modules.dataset_tagger.logic.ideogram import autobox
        editor=self.editor()
        entered=threading.Event();release=threading.Event();self.releases.append(release)
        boxer=MagicMock()
        def detect(*args):
            entered.set();release.wait(4);return [dict(bbox_px=[0,0,10,10])]
        boxer.detect.side_effect=detect
        with patch.object(autobox,'get_shared_boxer',return_value=boxer):
            editor._auto_detect()
            self.threads.append(editor._autodet)
            self.assertTrue(entered.wait(1))
            editor.reject()
            self.assertFalse(editor.shutdown_ready())
            release.set()
            self.assertTrue(pump(editor.shutdown_ready))
        self.assertEqual(editor._doc.elements,[])
        self.assertFalse(editor.pano_path.exists())

    def test_successful_refine_remains_explicit_proposal(self):
        review=self.review()
        review.editor.setPlainText('original')
        review._refine_snapshot=review._suggestion_state()
        with patch.object(review_view,'RefineProposalDialog') as proposal:
            proposal.return_value.exec.return_value=True
            proposal.return_value.result_text.return_value='accepted suggestion'
            review._on_refine_ready('suggestion')
        proposal.assert_called_once()
        self.assertEqual(review.editor.toPlainText(),'accepted suggestion')

    def test_actual_model_download_stays_owned_until_return(self):
        entered=threading.Event();release=threading.Event();self.releases.append(release)
        def download(*args,**kwargs):
            entered.set();release.wait(4)
        with patch.object(tagger.wd_download,'download',side_effect=download):
            self.view._download_wd()
            worker=self.view._wd_dl
            self.threads.append(worker)
            self.assertTrue(entered.wait(1))
            self.module.on_unload()
            self.assertFalse(self.module.shutdown_ready())
            release.set()
            self.assertTrue(pump(self.module.shutdown_ready))

    def test_actual_caption_discards_unpublished_result_without_late_ui(self):
        entered=threading.Event();release=threading.Event();self.releases.append(release)
        provider=MagicMock()
        def caption(*args,**kwargs):
            entered.set();release.wait(4);return 'private caption'
        provider.caption.side_effect=caption
        worker=caption_worker.CaptionWorker(provider,'mock',str(self.root),['flux'],images=[str(self.image)])
        self.threads.append(worker)
        self.view._caption=worker
        callbacks=[]
        self.view._workers.connect(worker,worker.finished_all,lambda *args:callbacks.append(args))
        self.view._workers.start(worker,'_caption')
        self.assertTrue(entered.wait(1))
        original=self.image.read_bytes()
        self.module.on_unload()
        self.assertFalse(self.module.shutdown_ready())
        release.set()
        self.assertTrue(pump(self.module.shutdown_ready))
        captions=[path for path in self.root.rglob('a.txt')]
        self.assertEqual(len(captions),0)
        self.assertFalse(list(self.root.rglob('*.caption.guard')))
        self.assertEqual(self.image.read_bytes(),original)
        self.assertEqual(callbacks,[])
        self.open_folder.assert_not_called()

    def test_actual_refinement_drain_does_not_offer_closed_window_result(self):
        review=self.review()
        review.editor.setPlainText('original')
        entered=threading.Event();release=threading.Event();self.releases.append(release)
        def complete(*args,**kwargs):
            entered.set();release.wait(4);return 'suggested'
        with patch.object(review_view,'OpenAICompatProvider') as provider, patch.object(review_view,'RefineProposalDialog') as proposal:
            provider.return_value.complete.side_effect=complete
            review._on_suggest()
            self.threads.append(review._refine_worker)
            self.assertTrue(entered.wait(1))
            review.close()
            self.assertFalse(self.module.shutdown_ready())
            release.set()
            self.assertTrue(pump(self.module.shutdown_ready))
        proposal.assert_not_called()
        self.assertEqual(review.editor.toPlainText(),'original')

    def test_tag_database_architecture_change_does_not_apply_old_csv(self):
        review=self.review()
        choices=[]
        for index in range(review.combo_arch.count()):
            review.combo_arch.setCurrentIndex(index)
            tmpl=review._current_template()
            if tmpl and tmpl.tag_csv and tmpl.tag_csv not in [csv for _,csv in choices]:
                choices.append((index,tmpl.tag_csv))
        self.assertGreaterEqual(len(choices),2)
        entered=threading.Event();release=threading.Event();self.releases.append(release)
        calls=[]
        def load(csv,url):
            calls.append(csv)
            if len(calls)==1:
                entered.set();release.wait(4)
            return 1
        with patch.object(tag_db,'is_ready',return_value=False), patch.object(tag_db,'is_present',return_value=True), patch.object(tag_db,'ensure_loaded',side_effect=load), patch.object(review.completer,'set_csv',wraps=review.completer.set_csv) as applied:
            review.combo_arch.setCurrentIndex(choices[0][0])
            review.chk_as_tag.setChecked(True)
            self.assertTrue(entered.wait(1))
            self.threads.append(review._tagdb_worker)
            review.combo_arch.setCurrentIndex(choices[1][0])
            release.set()
            self.assertTrue(pump(lambda:len(calls)==2 and review._tagdb_worker is None))
        self.assertEqual(calls,[choices[0][1],choices[1][1]])
        self.assertEqual([call.args[0] for call in applied.call_args_list],[choices[1][1]])

    def test_main_window_waits_for_tagger_auxiliary_worker(self):
        # main configures a persistent log at import; keep it out of this fixture.
        with patch('core.logging_config.setup_logging'):
            import main as product
        from core.library_service import close_library_services,request_close_library_services
        worker=self.held(self.review(),'_refine_worker')
        with ExitStack() as stack:
            stack.enter_context(patch.object(product.MainWindow,'load_available_modules'))
            stack.enter_context(patch.object(product,'close_library_services',side_effect=lambda **kwargs:close_library_services()))
            stack.enter_context(patch.object(product,'request_close_library_services',side_effect=lambda **kwargs:request_close_library_services()))
            window=product.MainWindow();window.show()
            window.loaded_modules[self.module.name]=self.module
            try:
                start=time.monotonic();window.close()
                self.assertLess(time.monotonic()-start,.1)
                self.assertFalse(window._shutdown_finished)
                self.assertTrue(window.isVisible())
                worker.release.set()
                self.assertTrue(pump(lambda:window._shutdown_finished))
                self.assertTrue(self.module.shutdown_ready())
            finally:
                worker.release.set();window.close();pump(lambda:window._shutdown_finished)
                window.deleteLater();QApplication.processEvents();close_library_services()


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    unittest.main()
