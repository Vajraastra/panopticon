"""Private Qt reader/dialog/profile shutdown; no models or real libraries."""
from contextlib import ExitStack, closing
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
import numpy as np
from shiboken6 import isValid
from PySide6.QtCore import QThread, Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog
from core.library_async import LibraryReader
from core.library_service import get_library_service,close_library_services,request_close_library_services
from core.library_repository import LibraryRepository
from core.paths import ProjectPaths
from core.theme_manager import ThemeManager
from core.components.reader_dialog import ReaderDialog
from core.components.copy_versions import CopyVersionsDialog
from core.components.curation_review import CurationReviewDialog
from core.components.curation_recovery import CurationRecoveryDialog
from core.curation_review import CurationReviewTasks
from core.curation_recovery_tasks import CurationRecoveryTasks
from modules.librarian.module import LibrarianModule
from modules.character_recognizer.module import CharacterRecognizerModule
from modules.character_recognizer.logic.profile_service import ProfileService,close_profile_services


def pump(predicate,seconds=3):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        QApplication.processEvents()
        if predicate():return True
        time.sleep(.003)
    return False


class AuxiliaryShutdown(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(ProjectPaths,'root',return_value=self.root))
        self.service=get_library_service()
        self.addCleanup(close_library_services)
        self.addCleanup(close_profile_services)
        self.modules=[];self.dialogs=[];self.readers=[];self.threads=[];self.releases=[]
        self.addCleanup(self.cleanup)
        self.context=dict(data_service=self.service,theme_manager=ThemeManager())

    def cleanup(self):
        for gate in self.releases:gate.set()
        for thread in self.threads:
            if isValid(thread):self.assertTrue(thread.wait(3000))
        for module in self.modules:module.on_unload()
        for reader in self.readers:
            if isValid(reader):reader.close()
            for thread in reader._threads:
                thread.join(3);self.assertFalse(thread.is_alive())
        for module in self.modules:self.assertTrue(pump(module.shutdown_ready))
        for dialog in self.dialogs:
            if isValid(dialog):
                dialog.close()
                self.assertTrue(pump(lambda: not isValid(dialog) or dialog.shutdown_ready()))
                if isValid(dialog):dialog.deleteLater()
        for module in self.modules:
            if module.view is not None:module.view.close();module.view.deleteLater()
        QApplication.processEvents()

    def module(self,cls):
        module=cls();module.on_load(self.context);module.get_view();self.modules.append(module)
        if hasattr(module,'_lazy_load_timer'):module._lazy_load_timer.stop()
        return module

    def gate(self):
        entered,release=threading.Event(),threading.Event()
        self.releases.append(release)
        def held(*args,**kwargs):
            entered.set();release.wait(4);return None
        return entered,release,held

    def attach(self,parent,held):
        class Service:
            read=staticmethod(held)
        reader=LibraryReader(Service(),parent);self.readers.append(reader)
        reader.request('private_operation')
        return reader

    def test_reader_start_failure_delivers_error_and_can_retry(self):
        class Service:
            def read(self,*args,**kwargs):return 'retry worked'
        reader=LibraryReader(Service());self.readers.append(reader)
        results=[];reader.result.connect(lambda *args:results.append(args))
        with patch('core.library_async.threading.Thread.start',side_effect=OSError('private start failure')):
            ticket=reader.request('private_operation')
        self.assertTrue(pump(lambda:bool(results)))
        self.assertEqual(results[0][0],ticket)
        self.assertIsInstance(results[0][2],OSError)
        self.assertFalse(reader._active)
        self.assertTrue(reader.shutdown_ready())
        self.assertIsNone(reader._thread)
        reader.request('private_operation')
        self.assertTrue(pump(lambda:len(results)==2))
        self.assertEqual(results[1][1],'retry worked')

    def test_reader_start_failure_is_suppressed_if_owner_closes(self):
        reader=LibraryReader(None);self.readers.append(reader)
        results=[];reader.result.connect(lambda *args:results.append(args))
        with patch('core.library_async.threading.Thread.start',side_effect=OSError('fixture')):
            reader.request('operation')
        reader.close();QApplication.processEvents()
        self.assertEqual(results,[])
        self.assertTrue(reader.shutdown_ready())

    def test_all_auxiliary_dialog_close_paths_defer_destruction(self):
        for intent in ('accept','reject','close'):
            with self.subTest(intent=intent):
                dialog=ReaderDialog();dialog.setAttribute(Qt.WA_DeleteOnClose)
                self.dialogs.append(dialog)
                entered,release,held=self.gate()
                self.attach(dialog,held)
                self.assertTrue(entered.wait(1));dialog.show()
                start=time.monotonic();getattr(dialog,intent)()
                self.assertLess(time.monotonic()-start,.1)
                self.assertTrue(isValid(dialog));self.assertTrue(dialog.isVisible())
                self.assertFalse(dialog.shutdown_ready())
                ticks=[];QTimer.singleShot(0,lambda:ticks.append(1))
                self.assertTrue(pump(lambda:bool(ticks)))
                release.set()
                self.assertTrue(pump(lambda:not isValid(dialog)))

    def test_real_copy_versions_dialog_reject_retains_its_reader(self):
        entered,release,held=self.gate()
        with patch.object(self.service,'read',side_effect=held):
            dialog=CopyVersionsDialog(self.service,lambda k,d:d)
            self.dialogs.append(dialog)
            self.readers.extend((dialog.reader,dialog.details_reader,dialog.action_reader))
            self.assertTrue(entered.wait(1));dialog.show();dialog.reject()
            self.assertFalse(dialog.shutdown_ready());self.assertTrue(isValid(dialog))
            release.set()
            self.assertTrue(pump(lambda:not isValid(dialog)))

    def test_real_curation_review_accept_does_not_destroy_reader(self):
        entered,release,held=self.gate()
        with patch.object(CurationReviewTasks,'read',side_effect=held):
            dialog=CurationReviewDialog(self.service.db_path,self.root,lambda k,d:d,ThemeManager(),service=self.service)
            self.dialogs.append(dialog);self.readers.extend((dialog.reader,dialog.apply_reader))
            self.assertTrue(entered.wait(1));dialog.show();dialog.accept()
            self.assertFalse(dialog.shutdown_ready());self.assertTrue(isValid(dialog))
            release.set();self.assertTrue(pump(lambda:not isValid(dialog)))

    def test_real_curation_recovery_close_does_not_destroy_reader(self):
        entered,release,held=self.gate()
        with patch.object(CurationRecoveryTasks,'read',side_effect=held):
            dialog=CurationRecoveryDialog(self.root,lambda k,d:d)
            self.dialogs.append(dialog);self.readers.append(dialog.reader)
            self.assertTrue(entered.wait(1));dialog.show();dialog.close()
            self.assertFalse(dialog.shutdown_ready());self.assertTrue(isValid(dialog))
            release.set();self.assertTrue(pump(lambda:not isValid(dialog)))

    def test_librarian_readiness_includes_module_readers(self):
        module=self.module(LibrarianModule)
        self.assertTrue(pump(lambda:module._overview_ready))
        entered,release,held=self.gate()
        with patch.object(self.service,'read',side_effect=held):
            module.overview_reader.request('count_files')
            self.readers.append(module.overview_reader)
            self.assertTrue(entered.wait(1));module.on_unload()
            self.assertFalse(module.shutdown_ready())
            release.set();self.assertTrue(pump(module.shutdown_ready))

    def test_librarian_readiness_includes_unreferenced_child_dialog(self):
        module=self.module(LibrarianModule)
        dialog=ReaderDialog(module.view);self.dialogs.append(dialog)
        entered,release,held=self.gate();self.attach(dialog,held)
        self.assertTrue(entered.wait(1));dialog.reject();module.on_unload()
        self.assertFalse(module.shutdown_ready())
        release.set();self.assertTrue(pump(module.shutdown_ready))

    def test_profile_service_ready_means_native_cleanup(self):
        service=ProfileService(self.root/'standalone.db',capacity=1)
        self.addCleanup(lambda:service.close(wait=True))
        entered,release,held=self.gate()
        original=service._add_reference
        def blocked(*args):held();return original(*args)
        with patch.object(service,'_add_reference',side_effect=blocked):
            future=service.submit('add_reference','A',np.array([1.,2.]))
            self.assertTrue(entered.wait(1))
            queued=service.submit('add_reference','B',np.array([2.,3.]))
            service.close();self.assertFalse(service.shutdown_ready())
            with self.assertRaisesRegex(RuntimeError,'closed'):service.submit('snapshot').result(1)
            release.set();self.assertTrue(future.result(2));self.assertTrue(queued.result(2))
            self.assertTrue(pump(service.shutdown_ready))
        with closing(sqlite3.connect(service.path)) as conn:
            self.assertEqual(conn.execute('SELECT name FROM characters ORDER BY name').fetchall(),[('A',),('B',)])

    def test_recognizer_waits_for_accepted_profile_write(self):
        module=self.module(CharacterRecognizerModule);view=module.content_view
        self.assertTrue(pump(lambda:not view._profile_read_pending))
        service=view.profile_db.service
        entered,release,held=self.gate();original=service._add_reference
        def blocked(*args):held();return original(*args)
        with patch.object(service,'_add_reference',side_effect=blocked):
            future=service.submit('add_reference','Private',np.array([1.,2.]))
            self.assertTrue(entered.wait(1));module.on_unload()
            start=time.monotonic();self.assertFalse(module.shutdown_ready())
            self.assertLess(time.monotonic()-start,.1)
            release.set();self.assertTrue(future.result(2))
            self.assertTrue(pump(module.shutdown_ready))
        self.assertFalse(service._thread.is_alive())
        with closing(sqlite3.connect(service.path)) as conn:
            self.assertEqual(conn.execute('SELECT samples_count FROM characters').fetchall(),[(1,)])

    def test_recognizer_keeps_profile_admission_until_native_workers_finish(self):
        module=self.module(CharacterRecognizerModule);view=module.content_view
        self.assertTrue(pump(lambda:not view._profile_read_pending))
        entered,release,held=self.gate()
        class Worker(QThread):
            def run(self):held()
            def stop(self):pass
        worker=Worker();view.worker=worker;self.threads.append(worker)
        worker.start();self.assertTrue(entered.wait(1))
        service=view.profile_db.service
        with patch.object(service,'close',wraps=service.close) as close:
            module.on_unload();self.assertFalse(module.shutdown_ready());close.assert_not_called()
            self.assertFalse(service._closed)
            release.set();self.assertTrue(worker.wait(3000))
            self.assertTrue(pump(module.shutdown_ready));self.assertTrue(service._closed)
        view.worker=None;worker.deleteLater()

    def test_recognizer_waits_for_native_reader_before_profile_close(self):
        module=self.module(CharacterRecognizerModule);view=module.content_view
        self.assertTrue(pump(lambda:not view._profile_read_pending))
        entered,release,held=self.gate()
        with patch.object(self.service,'read',side_effect=held):
            view._reader.request('get_curation_snapshot',[]);self.readers.append(view._reader)
            self.assertTrue(entered.wait(1));module.on_unload()
            self.assertFalse(module.shutdown_ready());self.assertFalse(view.profile_db.service._closed)
            release.set();self.assertTrue(pump(module.shutdown_ready))

    def test_accepted_copy_action_commits_before_dialog_can_close(self):
        path=(self.root/'a.png').as_posix()
        self.service.submit('register_files_minimal',[dict(path=path,filename='a.png',size=1,created=1)]).result(2)
        self.service.submit('edit_curation',path,add_tags=['curated'],rating=5).result(2)
        dialog=CopyVersionsDialog(self.service,lambda k,d:d)
        self.dialogs.append(dialog);self.readers.extend((dialog.reader,dialog.details_reader,dialog.action_reader))
        self.assertTrue(pump(lambda:not dialog._busy))
        dialog.scope.setCurrentIndex(1)
        self.assertTrue(pump(lambda:dialog._reviewed is not None))
        entered,release,held=self.gate();original=LibraryRepository.archive_copy
        def blocked(owner,*args,**kwargs):held();return original(owner,*args,**kwargs)
        with patch.object(LibraryRepository,'archive_copy',blocked):
            dialog.show();dialog.archive_button.click()
            self.assertTrue(entered.wait(1));dialog.close()
            self.assertFalse(dialog.shutdown_ready());self.assertTrue(isValid(dialog))
            release.set();self.assertTrue(pump(lambda:not isValid(dialog)))
        key=self.service.read('get_copy_versions_page',offline_only=False)['copies'][0]['id']
        state=self.service.read('get_copy_version_state',key)['state']
        self.assertTrue(state['file']['is_offline']);self.assertEqual(state['file']['rating'],5)
        self.assertEqual([tag['name'] for tag in state['tags']],['curated'])

    def window(self,module):
        with patch('core.logging_config.setup_logging'):
            import main as product
        self.stack.enter_context(patch.object(product.MainWindow,'load_available_modules'))
        self.stack.enter_context(patch.object(product,'close_library_services',side_effect=lambda **kwargs:close_library_services()))
        self.stack.enter_context(patch.object(product,'request_close_library_services',side_effect=lambda **kwargs:request_close_library_services()))
        window=product.MainWindow();window.loaded_modules[module.name]=module;window.show()
        def close_window():
            window.close();self.assertTrue(pump(lambda:window._shutdown_finished));window.deleteLater()
        self.addCleanup(close_window)
        return window

    def test_dashboard_keeps_central_admission_until_auxiliary_reader_finishes(self):
        module=self.module(LibrarianModule)
        self.assertTrue(pump(lambda:module._overview_ready))
        dialog=ReaderDialog(module.view);self.dialogs.append(dialog)
        entered,release,held=self.gate();self.attach(dialog,held)
        self.assertTrue(entered.wait(1));window=self.window(module)
        window.close();self.assertFalse(window._shutdown_finished)
        self.assertFalse(self.service._closed)
        ticks=[];QTimer.singleShot(0,lambda:ticks.append(1))
        self.assertTrue(pump(lambda:bool(ticks)))
        release.set();self.assertTrue(pump(lambda:window._shutdown_finished))
        self.assertTrue(self.service.shutdown_ready())

    def test_dashboard_keeps_central_admission_until_profile_commit_finishes(self):
        module=self.module(CharacterRecognizerModule);view=module.content_view
        self.assertTrue(pump(lambda:not view._profile_read_pending))
        service=view.profile_db.service
        entered,release,held=self.gate();original=service._add_reference
        def blocked(*args):held();return original(*args)
        with patch.object(service,'_add_reference',side_effect=blocked):
            future=service.submit('add_reference','Accepted',np.array([2.,3.]))
            self.assertTrue(entered.wait(1));window=self.window(module)
            window.close();self.assertFalse(window._shutdown_finished)
            self.assertFalse(self.service._closed)
            release.set();self.assertTrue(future.result(2))
            self.assertTrue(pump(lambda:window._shutdown_finished))
        self.assertTrue(service.shutdown_ready())
        self.assertTrue(self.service.shutdown_ready())


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    unittest.main(verbosity=2)
