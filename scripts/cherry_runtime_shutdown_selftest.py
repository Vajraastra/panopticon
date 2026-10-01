"""Owned asyncio tasks/executor cleanup; private Qt, filesystem and SQLite."""
import asyncio
from contextlib import ExitStack, closing
import inspect
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from core.paths import ProjectPaths
from core.library_service import close_library_services,request_close_library_services
from modules.cherry.logic.runtime import CherryRuntime,_LoopThread,_LIVE_THREADS
from modules.cherry.module import CherryModule
import main as product

def pump(condition,seconds=4):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        QApplication.processEvents()
        if condition():return True
        time.sleep(.003)
    return False

class RuntimeShutdown(unittest.TestCase):
    def setUp(self):
        self.runtime=CherryRuntime()
        self.releases=[]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for release in self.releases:release.set()
        self.runtime.request_stop()
        self.assertTrue(pump(self.runtime.shutdown_ready))
        QApplication.processEvents()
        self.assertFalse(_LIVE_THREADS)

    def block(self):
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        async def job():
            entered.set()
            await asyncio.to_thread(release.wait,3)
            return 'complete'
        return entered,release,job

    def test_idle_and_unstarted_close_rejects_reopen_and_submission(self):
        self.runtime.request_stop()
        self.assertTrue(self.runtime.shutdown_ready())
        self.assertFalse(self.runtime.start())
        async def empty():pass
        coroutine=empty()
        with self.assertRaises(RuntimeError):self.runtime.submit(coroutine)
        self.assertEqual(inspect.getcoroutinestate(coroutine),'CORO_CLOSED')

    def test_request_stop_returns_while_admitted_executor_work_finishes(self):
        entered,release,job=self.block()
        self.assertTrue(self.runtime.start())
        future=self.runtime.submit(job())
        self.assertTrue(entered.wait(1))
        start=time.monotonic();self.runtime.request_stop()
        self.assertLess(time.monotonic()-start,.1)
        self.assertFalse(self.runtime.shutdown_ready())
        self.assertFalse(self.runtime.start())
        ticks=[];QTimer.singleShot(0,lambda:ticks.append(1))
        self.assertTrue(pump(lambda:bool(ticks)))
        release.set()
        self.assertEqual(future.result(2),'complete')
        self.assertTrue(pump(self.runtime.shutdown_ready))

    def test_cancelled_future_ack_does_not_release_task_until_finally_cleanup(self):
        entered,cleaning,complete,release=[threading.Event() for _ in range(4)]
        self.releases.append(release)
        async def job():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await asyncio.to_thread(release.wait,3)
                complete.set()
        self.runtime.start();future=self.runtime.submit(job())
        self.assertTrue(entered.wait(1))
        self.assertTrue(future.cancel())
        self.assertTrue(cleaning.wait(1))
        self.runtime.request_stop()
        self.assertTrue(future.cancelled())
        self.assertFalse(self.runtime.shutdown_ready())
        self.assertFalse(complete.is_set())
        release.set();self.assertTrue(pump(self.runtime.shutdown_ready))
        self.assertTrue(complete.is_set())

    def test_cancel_before_admission_closes_unstarted_coroutine(self):
        self.runtime.start()
        entered,release=threading.Event(),threading.Event();self.releases.append(release)
        self.runtime.call_soon(lambda:(entered.set(),release.wait(3)))
        self.assertTrue(entered.wait(1))
        ran=[]
        async def job():ran.append(1)
        coroutine=job();future=self.runtime.submit(coroutine)
        self.assertTrue(future.cancel())
        self.runtime.request_stop();release.set()
        self.assertTrue(pump(self.runtime.shutdown_ready))
        self.assertEqual(inspect.getcoroutinestate(coroutine),'CORO_CLOSED')
        self.assertFalse(ran)

    def test_expired_blocking_stop_retains_owner_and_never_terminates_it(self):
        self.runtime.start();entered,release,job=self.block()
        future=self.runtime.submit(job())
        self.assertTrue(entered.wait(1));thread=self.runtime._thread
        with patch.object(thread,'terminate') as terminate:
            with self.assertRaises(TimeoutError):self.runtime.stop(timeout=.001)
            terminate.assert_not_called()
        self.assertIs(self.runtime._thread,thread)
        self.assertTrue(thread.isRunning())
        self.assertIn(thread,_LIVE_THREADS)
        release.set();self.assertEqual(future.result(2),'complete')
        self.assertTrue(pump(self.runtime.shutdown_ready))

    def test_residual_background_cleanup_is_awaited(self):
        entered,finished=threading.Event(),threading.Event()
        async def background():
            try:
                entered.set();await asyncio.Event().wait()
            finally:
                await asyncio.sleep(.04);finished.set()
        async def job():asyncio.create_task(background())
        self.runtime.start();self.runtime.submit(job()).result(1)
        self.assertTrue(entered.wait(1))
        self.runtime.request_stop()
        self.assertTrue(pump(self.runtime.shutdown_ready))
        self.assertTrue(finished.is_set())

    def test_job_exception_propagates_and_does_not_prevent_close(self):
        async def job():raise ValueError('private failure')
        self.runtime.start();future=self.runtime.submit(job())
        with self.assertRaisesRegex(ValueError,'private failure'):future.result(1)
        self.runtime.request_stop();self.assertTrue(pump(self.runtime.shutdown_ready))

    def test_loop_cannot_wait_for_itself(self):
        async def job():
            with self.assertRaisesRegex(RuntimeError,'Cannot wait'):self.runtime.stop()
            return True
        self.runtime.start();self.assertTrue(self.runtime.submit(job()).result(1))

    def test_failed_start_keeps_owner_until_requested_native_cleanup(self):
        release=threading.Event();self.releases.append(release)
        original=_LoopThread.run
        def delayed(thread):release.wait(3);original(thread)
        with patch.object(_LoopThread,'run',delayed),patch.object(_LoopThread,'wait_ready',return_value=False):
            self.assertFalse(self.runtime.start())
            thread=self.runtime._thread
            self.assertIsNotNone(thread)
            self.assertIn(thread,_LIVE_THREADS)
            self.assertFalse(self.runtime.shutdown_ready())
            release.set();self.assertTrue(pump(self.runtime.shutdown_ready))

    def test_accepted_private_sqlite_mutation_commits_before_readiness(self):
        self.runtime.start();entered,release=threading.Event(),threading.Event();self.releases.append(release)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'own-private.db'
            def write():
                with closing(sqlite3.connect(path)) as conn:
                    conn.execute('CREATE TABLE private_test(value)')
                    conn.execute('INSERT INTO private_test VALUES (42)')
                    entered.set();release.wait(3);conn.commit()
            async def job():await asyncio.to_thread(write)
            future=self.runtime.submit(job());self.assertTrue(entered.wait(1))
            self.runtime.request_stop()
            self.assertFalse(self.runtime.shutdown_ready())
            release.set();future.result(2)
            self.assertTrue(pump(self.runtime.shutdown_ready))
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('SELECT value FROM private_test').fetchall(),[(42,)])

    def test_cancel_racing_completion_has_no_loop_callback_errors(self):
        self.runtime.start();errors=[]
        self.runtime.call_soon(self.runtime.loop.set_exception_handler,lambda loop,context:errors.append(context))
        async def job():await asyncio.sleep(0);return 1
        futures=[self.runtime.submit(job()) for _ in range(200)]
        for future in futures:future.cancel()
        self.runtime.request_stop();self.assertTrue(pump(self.runtime.shutdown_ready))
        self.assertFalse(errors)
        self.assertTrue(all(future.done() for future in futures))

    def test_dashboard_waits_for_real_cherry_cleanup_and_keeps_qt_responsive(self):
        entered,release,job=self.block()
        with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
            stack.enter_context(patch.object(ProjectPaths,'root',return_value=Path(directory)))
            stack.enter_context(patch.object(product.MainWindow,'load_available_modules'))
            stack.enter_context(patch.object(product,'close_library_services',side_effect=lambda **kwargs:close_library_services()))
            stack.enter_context(patch.object(product,'request_close_library_services',side_effect=lambda **kwargs:request_close_library_services()))
            window=product.MainWindow();window.show()
            module=CherryModule();module.runtime=self.runtime;module.on_load(window.context)
            window.loaded_modules[module.name]=module
            self.runtime.start();future=self.runtime.submit(job());self.assertTrue(entered.wait(1))
            try:
                start=time.monotonic();window.close()
                self.assertLess(time.monotonic()-start,.1)
                self.assertFalse(window._shutdown_finished)
                self.assertTrue(window.isVisible())
                ticks=[];QTimer.singleShot(0,lambda:ticks.append(1))
                self.assertTrue(pump(lambda:bool(ticks)))
                release.set();future.result(2)
                self.assertTrue(pump(lambda:window._shutdown_finished))
                self.assertTrue(self.runtime.shutdown_ready())
            finally:
                release.set();window.close();pump(lambda:window._shutdown_finished)
                window.deleteLater();QApplication.processEvents();close_library_services()

if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    unittest.main(verbosity=2)
