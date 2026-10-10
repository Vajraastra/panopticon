"""Private Windows/offscreen profile handoff; no network or real library writes."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QMenu
from core.base_module import BaseModule
from core.library_service import LibraryService
from core.module_handoff import image_set_consumers
from core.native_threads import native_threads_ready
from main import MainWindow
from modules.cherry.module import CherryModule
from modules.cherry.logic import profile_images as discovery
from modules.pickler.module import PicklerModule

app = QApplication.instance() or QApplication([])
app.setStyle('Fusion')


def pump(predicate, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        if predicate():
            return True
        time.sleep(.003)
    return False


class CherryHandoff(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cherry-handoff-')
        self.root = Path(self.temp.name)
        self.service = LibraryService(self.root / 'private.db')
        with patch('main.get_library_service', return_value=self.service), patch.object(
                MainWindow, 'load_available_modules'):
            self.main = MainWindow()
        self.cherry, self.pickler = CherryModule(), PicklerModule()
        self.main.loaded_modules = {}
        for module in (self.cherry, self.pickler):
            module.on_load(self.main.context)
            self.main.loaded_modules[module.name] = module
        self.main.setup_integrations()
        self.view = self.cherry.get_view()
        self.profiles = []
        for pid, name in ((1, 'Zebra'), (2, 'Alpha')):
            folder = self.root / name
            folder.mkdir()
            image = QImage(8, 8, QImage.Format_RGB32)
            image.fill(0xff336699)
            image.save(str(folder / (name + '.png')))
            (folder / 'caption.txt').write_text('caption', encoding='utf-8')
            (folder / 'old.zip').write_bytes(b'archive')
            hidden = folder / '.recovery'
            hidden.mkdir()
            image.save(str(hidden / 'hidden.png'))
            sub = folder / 'sub'
            sub.mkdir()
            image.save(str(sub / 'nested.PNG'))
            self.profiles.append(dict(id=pid, display_name=name, folder_path=str(folder),
                folder_exists=True, primary_site='test', last_checked=None, url_count=0))
        self.view._on_profiles_ready(self.profiles)

    def tearDown(self):
        self.cherry.on_unload()
        self.pickler.on_unload()
        self.main._cherry_ingestor.request_shutdown()
        self.assertTrue(pump(lambda: self.cherry.shutdown_ready()
                             and self.pickler.shutdown_ready() and native_threads_ready()
                             and self.main._cherry_ingestor.shutdown_ready()))
        for window in self.main.module_windows.values():
            window.hide()
            window.deleteLater()
        self.main.hide()
        self.main.deleteLater()
        app.processEvents()
        self.service.close()
        self.temp.cleanup()

    def test_sorted_right_click_delivers_clicked_profile_to_real_pickler(self):
        self.view._table.selectRow(1)  # Zebra selected before right-clicking Alpha.
        received = []
        self.cherry.request_open_module.connect(lambda name, paths: received.append((name, paths)))

        def choose():
            menu = next(w for w in app.topLevelWidgets() if isinstance(w, QMenu) and w.isVisible())
            submenu = menu.actions()[0].menu()
            self.assertTrue(submenu.isEnabled())
            action = submenu.actions()[0]
            self.assertEqual(action.data(), 'Pickler')
            self.assertTrue(action.toolTip())
            action.trigger()
            menu.close()

        item = self.view._table.item(0, 0)
        self.assertEqual(item.data(Qt.UserRole), 2)
        QTimer.singleShot(0, choose)
        self.view._show_profile_menu(self.view._table.visualItemRect(item).center())
        self.assertTrue(pump(lambda: bool(received) and self.pickler.view is not None
            and self.pickler.view._worker is None and len(self.pickler.view._images) == 2))
        self.assertEqual(received[0][0], 'Pickler')
        self.assertEqual({Path(p).name for p in received[0][1]}, {'Alpha.png', 'nested.PNG'})
        self.assertTrue(all('/Alpha/' in p for p in received[0][1]))
        self.assertIn('Pickler', self.main.module_windows)
        self.view._cancel_send()  # An open menu may outlive the completed read.
        self.assertTrue((self.root / 'Alpha' / 'Alpha.png').is_file())
        self.assertTrue((self.root / 'Alpha' / '.recovery' / 'hidden.png').is_file())

    def test_read_error_and_empty_never_open_destination(self):
        received = []
        self.view.image_set_ready.connect(lambda *args: received.append(args))
        profile = dict(self.profiles[0], folder_path=str(self.root / 'missing'))
        self.view._send_profile(profile, 'Pickler')
        self.assertTrue(pump(lambda: self.view._send_worker is None))
        self.assertFalse(received)
        empty = self.root / 'empty'
        empty.mkdir()
        profile = dict(self.profiles[0], folder_path=str(empty))
        self.view._profiles = [profile]
        self.view._send_profile(profile, 'Pickler')
        self.assertTrue(pump(lambda: self.view._send_worker is None))
        self.assertFalse(received)
        self.assertFalse(self.main.module_windows)

    def test_cancel_and_shutdown_suppress_late_delivery_and_duplicate_jobs(self):
        entered, release = threading.Event(), threading.Event()
        received = []
        self.view.image_set_ready.connect(lambda *args: received.append(args))

        def slow(folder, event):
            entered.set()
            release.wait(3)
            # Simulate a result racing cancellation; shutdown must still suppress it.
            return [str(self.root / 'Zebra' / 'Zebra.png')]

        with patch.object(discovery, 'profile_images', slow):
            self.view._send_profile(self.profiles[0], 'Pickler')
            self.assertTrue(entered.wait(2))
            worker = self.view._send_worker
            self.view._send_profile(self.profiles[1], 'Pickler')
            self.assertIs(self.view._send_worker, worker)
            menu = self.view._profile_menu(self.profiles[0])
            self.assertFalse(menu.actions()[0].menu().isEnabled())
            self.assertTrue(menu.actions()[-1].toolTip())
            menu.actions()[-1].trigger()
            self.assertTrue(worker._cancel.is_set())
            self.cherry.on_unload()
            self.assertFalse(self.cherry.shutdown_ready())
            release.set()
            self.assertTrue(pump(lambda: self.view._send_worker is None and self.cherry.shutdown_ready()))
            menu.deleteLater()
        self.assertFalse(received)

    def test_discovery_never_returns_partial_on_budget_or_cancel(self):
        folder = self.root / 'Alpha'
        with patch.object(discovery, 'MAX_IMAGES', 1):
            with self.assertRaisesRegex(ValueError, 'profile_images_limit'):
                discovery.profile_images(folder, threading.Event())
        event = threading.Event()
        event.set()
        with self.assertRaisesRegex(ValueError, 'profile_images_cancelled'):
            discovery.profile_images(folder, event)

    def test_cancel_racing_a_result_does_not_send_or_open(self):
        entered, release = threading.Event(), threading.Event()
        received = []
        self.view.image_set_ready.connect(lambda *args: received.append(args))

        def slow(folder, event):
            entered.set()
            release.wait(3)
            return [str(self.root / 'Zebra' / 'Zebra.png')]

        with patch.object(discovery, 'profile_images', slow):
            self.view._send_profile(self.profiles[0], 'Pickler')
            self.assertTrue(entered.wait(2))
            self.view._send_worker.stop()
            release.set()
            self.assertTrue(pump(lambda: self.view._send_worker is None))
        self.assertFalse(received)
        self.assertFalse(self.main.module_windows)

    def test_inherited_no_op_and_unavailable_tools_are_excluded(self):
        unsupported = BaseModule()
        modules = dict(self.main.loaded_modules, unsupported=unsupported)
        self.assertEqual(set(image_set_consumers(modules, self.cherry)), {'Pickler'})
        self.view.set_send_targets([])
        menu = self.view._profile_menu(self.profiles[0])
        self.assertFalse(menu.actions()[0].menu().isEnabled())
        menu.deleteLater()
        self.main.open_cherry_image_set('missing', ['input.png'])
        self.assertFalse(self.main.module_windows)


if __name__ == '__main__':
    unittest.main()
