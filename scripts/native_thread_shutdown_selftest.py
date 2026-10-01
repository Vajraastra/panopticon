"""Real Qt deferred cleanup after finished, with nonblocking native joins."""
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from shiboken6 import isValid
from PySide6.QtCore import QObject,QThread,QTimer,Qt,QCoreApplication,QEvent,QEventLoop
from PySide6.QtWidgets import QApplication
from core.native_threads import (start_worker,native_done,owner_threads_ready,
                                native_threads_ready,_NATIVE_THREADS)
from core.paths import ProjectPaths,CachePaths
from core.theme_manager import ThemeManager
from core.library_service import get_library_service,close_library_services,request_close_library_services


def pump(predicate,seconds=3):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        if predicate():return True
        time.sleep(.003)
    return False


class CleanupThread(QThread):
    """Qt deletes this QObject after native finished, before OS-thread exit."""
    def __init__(self):
        super().__init__()
        self.entered=threading.Event()
        self.release=threading.Event()
        self.cleanup_done=threading.Event()

    def cleanup(self,*args):
        self.entered.set()
        self.release.wait(4)
        self.cleanup_done.set()

    def run(self):
        self.resource=QObject()
        self.resource.destroyed.connect(self.cleanup,Qt.DirectConnection)
        self.resource.deleteLater()


class NativeCleanup(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(ProjectPaths,'root',return_value=self.root))
        self.stack.enter_context(patch.object(CachePaths,'get_models_root',return_value=self.root/'models'))
        self.service=get_library_service();self.addCleanup(close_library_services)
        self.context=dict(data_service=self.service,theme_manager=ThemeManager())
        self.threads=[];self.modules=[];self.windows=[];self.runtimes=[]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for worker in self.threads:
            if hasattr(worker,'release'):worker.release.set()
        for runtime in self.runtimes:runtime.stop()
        for worker in self.threads:
            if isValid(worker):self.assertTrue(worker.wait(3000))
        self.assertTrue(pump(native_threads_ready))
        for module in self.modules:module.on_unload()
        for module in self.modules:self.assertTrue(pump(module.shutdown_ready))
        for window in self.windows:
            window.close();self.assertTrue(pump(lambda:window._shutdown_finished));window.deleteLater()
        for module in self.modules:
            view=getattr(module,'view',None)
            if view is not None:view.close();view.deleteLater()
        from modules.character_recognizer.logic.profile_service import close_profile_services
        close_profile_services()
        QApplication.processEvents()

    def worker(self,owner=None):
        owner=owner or QObject()
        worker=CleanupThread();self.threads.append(worker)
        start_worker(worker,owner)
        self.assertTrue(worker.entered.wait(1))
        return worker,owner

    def test_finished_signal_precedes_real_qt_cleanup_and_join(self):
        worker,owner=self.worker()
        self.assertTrue(worker.isFinished())
        self.assertFalse(worker.cleanup_done.is_set())
        self.assertFalse(native_done(worker))
        start=time.monotonic();self.assertFalse(owner_threads_ready(owner))
        self.assertLess(time.monotonic()-start,.1)
        ticks=[];QTimer.singleShot(0,lambda:ticks.append(True))
        self.assertTrue(pump(lambda:bool(ticks)))
        self.assertIn(worker,_NATIVE_THREADS)
        worker.release.set()
        self.assertTrue(pump(lambda:owner_threads_ready(owner)))
        self.assertTrue(worker.cleanup_done.is_set())
        self.assertTrue(native_done(worker))

    def test_delete_later_is_deferred_without_blocking_gui(self):
        worker,owner=self.worker()
        worker.deleteLater()
        ticks=[];QTimer.singleShot(0,lambda:ticks.append(True))
        start=time.monotonic();self.assertTrue(pump(lambda:bool(ticks)))
        self.assertLess(time.monotonic()-start,.1)
        self.assertTrue(isValid(worker));self.assertIn(worker,_NATIVE_THREADS)
        self.assertFalse(worker.cleanup_done.is_set())
        worker.release.set()
        self.assertTrue(pump(lambda:not isValid(worker)))
        self.assertTrue(owner_threads_ready(owner))

    def test_owner_readiness_isolated_and_global_gate_keeps_other_worker(self):
        first,owner1=self.worker();second,owner2=self.worker()
        second.release.set()
        self.assertTrue(pump(lambda:owner_threads_ready(owner2)))
        self.assertFalse(owner_threads_ready(owner1))
        self.assertFalse(native_threads_ready())
        first.release.set();self.assertTrue(pump(native_threads_ready))

    def test_unstarted_wait_is_ready_and_failed_start_releases_retainer(self):
        worker=CleanupThread();self.threads.append(worker)
        self.assertTrue(native_done(worker))
        owner=QObject()
        with patch.object(worker,'start',side_effect=OSError('private failed start')):
            with self.assertRaisesRegex(OSError,'private failed start'):start_worker(worker,owner)
        self.assertNotIn(worker,_NATIVE_THREADS)
        self.assertTrue(owner_threads_ready(owner))

    def test_double_start_cannot_reuse_thread_during_final_cleanup(self):
        worker,owner=self.worker()
        with self.assertRaisesRegex(RuntimeError,'still_retained'):start_worker(worker,owner)
        self.assertIn(worker,_NATIVE_THREADS)

    def test_start_raises_after_native_launch_but_owner_stays_retained(self):
        class FailedAfterLaunch(CleanupThread):
            def start(self):
                super().start()
                raise OSError('private post-start failure')
        worker=FailedAfterLaunch();self.threads.append(worker);owner=QObject()
        with self.assertRaisesRegex(OSError,'private post-start failure'):start_worker(worker,owner)
        self.assertTrue(worker.entered.wait(1))
        self.assertFalse(owner_threads_ready(owner))
        self.assertIn(worker,_NATIVE_THREADS)
        worker.release.set();self.assertTrue(pump(lambda:owner_threads_ready(owner)))

    def test_wrong_creator_thread_rejects_before_admission(self):
        worker=CleanupThread();self.threads.append(worker);owner=QObject();errors=[]
        def wrong():
            try:start_worker(worker,owner)
            except Exception as exc:errors.append(str(exc))
        thread=threading.Thread(target=wrong);thread.start();thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,['native_worker_wrong_owner_thread'])
        self.assertNotIn(worker,_NATIVE_THREADS)

    def test_actual_qevent_loop_defers_delete_and_keeps_timers_responsive(self):
        worker,owner=self.worker()
        loop=QEventLoop();ticks=[]
        worker.deleteLater()
        QTimer.singleShot(0,lambda:ticks.append(True))
        QTimer.singleShot(35,worker.release.set)
        QTimer.singleShot(160,loop.quit)
        loop.exec()
        self.assertTrue(ticks)
        self.assertTrue(worker.cleanup_done.is_set())
        self.assertFalse(isValid(worker))
        self.assertTrue(owner_threads_ready(owner))

    def test_completed_delete_removes_registry_before_wrapper_is_invalidated(self):
        worker,owner=self.worker()
        worker.release.set();self.assertTrue(worker.wait(3000))
        worker.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        self.assertFalse(isValid(worker))
        self.assertTrue(owner_threads_ready(owner))
        self.assertTrue(native_threads_ready())

    def test_real_thread_can_restart_after_join_without_stale_monitor(self):
        worker,owner=self.worker()
        old=_NATIVE_THREADS[worker]
        worker.release.set();self.assertTrue(worker.wait(3000))
        worker.release.clear();worker.entered.clear();worker.cleanup_done.clear()
        start_worker(worker,owner)
        self.assertTrue(worker.entered.wait(1))
        self.assertTrue(old.poll())
        self.assertIn(worker,_NATIVE_THREADS)
        self.assertIsNot(_NATIVE_THREADS[worker],old)
        self.assertFalse(owner_threads_ready(owner))
        worker.release.set();self.assertTrue(pump(lambda:owner_threads_ready(owner)))

    def test_producer_readiness_keeps_cleanup_even_without_worker_field(self):
        import importlib
        from core.base_module import BaseModule
        for name in ('cropper','image_optimizer','format_scanner','metadata',
                     'duplicate_finder','librarian','character_recognizer'):
            with self.subTest(module=name):
                imported=importlib.import_module(f'modules.{name}.module')
                cls=next(cls for cls in vars(imported).values() if isinstance(cls,type)
                         and issubclass(cls,BaseModule) and cls is not BaseModule)
                module=cls();module.on_load(self.context);module.get_view();self.modules.append(module)
                if hasattr(module,'_lazy_load_timer'):module._lazy_load_timer.stop()
                owner=module.content_view if name=='character_recognizer' else module
                worker,_=self.worker(owner)
                module.on_unload()
                self.assertFalse(module.shutdown_ready())
                worker.release.set();self.assertTrue(pump(module.shutdown_ready))

    def test_tagger_retains_field_after_qt_finished_until_join(self):
        from modules.dataset_tagger.module import DatasetTaggerModule
        from modules.dataset_tagger.logic import tag_db
        with patch.object(tag_db,'is_ready',return_value=True):
            module=DatasetTaggerModule();module.on_load(self.context);module.get_view();self.modules.append(module)
        worker=CleanupThread();self.threads.append(worker)
        module.view._caption=worker
        module.view._workers.start(worker,'_caption')
        self.assertTrue(worker.entered.wait(1));QApplication.processEvents()
        self.assertIs(module.view._caption,worker)
        module.on_unload();self.assertFalse(module.shutdown_ready())
        worker.release.set();self.assertTrue(pump(module.shutdown_ready))
        self.assertIsNone(module.view._caption)

    def test_cherry_stopped_signal_waits_for_deferred_native_cleanup(self):
        from modules.cherry.logic import runtime as cherry
        original=cherry._LoopThread
        release=threading.Event();entered=threading.Event()
        class Loop(original):
            def run(self):
                super().run()
                self.resource=QObject()
                def cleanup(*args):entered.set();release.wait(4)
                self.resource.destroyed.connect(cleanup,Qt.DirectConnection)
                self.resource.deleteLater()
        runtime=cherry.CherryRuntime();self.runtimes.append(runtime)
        stopped=[];runtime.stopped_signal.connect(lambda:stopped.append(True))
        with patch.object(cherry,'_LoopThread',Loop):self.assertTrue(runtime.start())
        worker=runtime._thread;worker.release=release;self.threads.append(worker)
        runtime.request_stop();self.assertTrue(entered.wait(1));QApplication.processEvents()
        self.assertIs(runtime._thread,worker)
        self.assertFalse(runtime.shutdown_ready());self.assertEqual(stopped,[])
        release.set();self.assertTrue(pump(runtime.shutdown_ready))
        self.assertTrue(pump(lambda:bool(stopped)))
        self.assertEqual(stopped,[True])

    def test_dashboard_keeps_native_open_after_module_drops_worker_field(self):
        with patch('core.logging_config.setup_logging'):
            import main as product
        self.stack.enter_context(patch.object(product.MainWindow,'load_available_modules'))
        self.stack.enter_context(patch.object(product,'close_library_services',side_effect=lambda **kw:close_library_services()))
        self.stack.enter_context(patch.object(product,'request_close_library_services',side_effect=lambda **kw:request_close_library_services()))
        window=product.MainWindow();self.windows.append(window);window.show()
        worker,owner=self.worker()
        # Stand-in for a legacy module that cleared its field on Qt finished.
        self.assertTrue(worker.isFinished())
        start=time.monotonic();window.close()
        self.assertLess(time.monotonic()-start,.1)
        self.assertFalse(window._shutdown_finished);self.assertTrue(window.isVisible())
        self.assertFalse(self.service._closed)
        worker.deleteLater()
        ticks=[];QTimer.singleShot(0,lambda:ticks.append(True))
        self.assertTrue(pump(lambda:bool(ticks)));self.assertTrue(isValid(worker))
        worker.release.set();self.assertTrue(pump(lambda:window._shutdown_finished))
        self.assertTrue(self.service.shutdown_ready())


if __name__=='__main__':
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
    unittest.main(verbosity=2)
