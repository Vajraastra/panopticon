"""Private full-queue drain and responsive Qt application shutdown."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
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
from core.library_repository import LibraryRepository
from core.library_service import LibraryService, LibraryError, close_library_services, request_close_library_services
from core.paths import ProjectPaths
import main as product

def pump(condition,seconds=3):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        QApplication.processEvents()
        if condition():return True
        time.sleep(.005)
    return False

class Shutdown(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.service=LibraryService(self.root/'private.db',capacity=1)
        self.addCleanup(self.service.close)

    def block(self):
        entered,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        original=LibraryRepository.register_files_minimal
        def blocked(repo,*args,**kwargs):
            entered.set()
            if not release.wait(3):raise TimeoutError('fixture release')
            return original(repo,*args,**kwargs)
        return entered,release,blocked

    def observation(self,name):
        return [dict(path=str(self.root/name),filename=name,size=1,created=1)]

    def test_full_queue_close_request_returns_without_waiting_and_drains_all(self):
        entered,release,blocked=self.block()
        with patch.object(LibraryRepository,'register_files_minimal',blocked):
            first=self.service.submit('register_files_minimal',self.observation('first.png'))
            self.assertTrue(entered.wait(1))
            second=self.service.submit('register_files_minimal',self.observation('second.png'))
            start=time.monotonic()
            self.service.request_close()
            self.assertLess(time.monotonic()-start,.1)
            self.assertFalse(self.service.shutdown_ready())
            with self.assertRaises(LibraryError):self.service.submit('register_files_minimal',self.observation('third.png')).result()
            ticks=[]
            QTimer.singleShot(0,lambda:ticks.append(True))
            self.assertTrue(pump(lambda:bool(ticks)))
            release.set()
            self.assertTrue(first.result(2))
            self.assertTrue(second.result(2))
        self.assertTrue(pump(self.service.shutdown_ready))
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM files').fetchone(),(2,))

    def test_idle_close_is_idempotent_and_rejects_new_reads(self):
        self.service.request_close()
        self.service.request_close()
        with self.assertRaisesRegex(LibraryError,'service_closed'):self.service.read('count_files')
        self.assertTrue(pump(self.service.shutdown_ready))
        self.service.close()
        self.service.close()

    def test_blocking_compatibility_close_still_waits_for_accepted_command(self):
        entered,release,blocked=self.block()
        with patch.object(LibraryRepository,'register_files_minimal',blocked):
            future=self.service.submit('register_files_minimal',self.observation('first.png'))
            self.assertTrue(entered.wait(1))
            with ThreadPoolExecutor(max_workers=1) as pool:
                closed=pool.submit(self.service.close)
                self.assertFalse(closed.done())
                release.set()
                self.assertTrue(future.result(2))
                closed.result(2)
        self.assertTrue(self.service.shutdown_ready())

    def test_cancelled_queued_future_does_not_prevent_drain_or_cleanup(self):
        entered,release,blocked=self.block()
        with patch.object(LibraryRepository,'register_files_minimal',blocked):
            first=self.service.submit('register_files_minimal',self.observation('first.png'))
            self.assertTrue(entered.wait(1))
            second=self.service.submit('register_files_minimal',self.observation('second.png'))
            self.assertTrue(second.cancel())
            self.service.request_close()
            release.set()
            first.result(2)
        self.assertTrue(pump(self.service.shutdown_ready))
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM files').fetchone(),(1,))

    def test_transaction_failure_finishes_future_and_does_not_hang_shutdown(self):
        with patch.object(LibraryRepository,'register_files_minimal',side_effect=RuntimeError('fixture')):
            future=self.service.submit('register_files_minimal',self.observation('first.png'))
            self.service.request_close()
            with self.assertRaisesRegex(LibraryError,'fixture'):future.result(2)
            self.assertTrue(pump(self.service.shutdown_ready))

    def test_writer_cannot_join_itself(self):
        original=LibraryRepository.register_files_minimal
        def worker(repo,*args,**kwargs):
            with self.assertRaisesRegex(RuntimeError,'Cannot join'):self.service.close()
            return original(repo,*args,**kwargs)
        with patch.object(LibraryRepository,'register_files_minimal',worker):
            self.assertTrue(self.service.submit('register_files_minimal',self.observation('first.png')).result(2))

    def test_main_window_keeps_qt_alive_until_native_database_owner_finishes(self):
        with patch.object(ProjectPaths,'root',return_value=self.root),patch.object(product.MainWindow,'load_available_modules'),patch.object(product,'request_close_library_services',side_effect=lambda **kwargs:request_close_library_services()),patch.object(product,'close_library_services',side_effect=lambda **kwargs:close_library_services()):
            window=product.MainWindow()
            self.addCleanup(close_library_services)
            service=window.context['data_service']
            entered,release,blocked=self.block()
            window.show()
            try:
                with patch.object(LibraryRepository,'register_files_minimal',blocked):
                    future=service.submit('register_files_minimal',self.observation('accepted.png'))
                    self.assertTrue(entered.wait(1))
                    start=time.monotonic()
                    window.close()
                    self.assertLess(time.monotonic()-start,.1)
                    self.assertFalse(window._shutdown_finished)
                    self.assertTrue(window.isVisible())
                    ticks=[]
                    QTimer.singleShot(0,lambda:ticks.append(True))
                    self.assertTrue(pump(lambda:bool(ticks)))
                    release.set()
                    self.assertTrue(future.result(2))
                    self.assertTrue(pump(lambda:window._shutdown_finished))
                self.assertTrue(service.shutdown_ready())
                self.assertFalse(window.isVisible())
            finally:
                release.set()
                window.close()
                pump(lambda:window._shutdown_finished)
                window.deleteLater()
                QApplication.processEvents()

if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    unittest.main(verbosity=2)
