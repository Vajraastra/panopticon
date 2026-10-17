"""Private offscreen Pickler UI: asynchronous actions and real worker shutdown."""
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
from PySide6.QtCore import QSettings,QThread,QCoreApplication,QEvent,QTimer,QObject,Qt
from PySide6.QtGui import QImage,QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QPushButton,QListWidget,QMessageBox,QDialog,QLineEdit
from core.event_bus import EventBus
from core.library_service import LibraryService
from core.native_threads import native_threads_ready
from core.metadata.bundle import MetadataBundle
from modules.pickler.module import PicklerModule
from modules.pickler.logic import transfers
from modules.pickler.logic.worker import PicklerWorker

app=QApplication.instance() or QApplication([])


def pump(predicate,seconds=6):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        app.processEvents(); QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        if predicate():return True
        time.sleep(.003)
    return False


class PicklerUI(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name); self.service=LibraryService(self.root/'private.db'); self.addCleanup(self.service.close)
        self.folder=self.root/'input'; self.folder.mkdir()
        self.source=self.folder/'image.png'
        image=QImage(20,10,QImage.Format_RGB32); image.fill(0xff336699); self.assertTrue(image.save(str(self.source)))
        self.source.with_suffix('.txt').write_bytes(b'private caption')
        self.service.submit('register_files_minimal',[dict(path=str(self.source),filename=self.source.name,size=self.source.stat().st_size,created=1)]).result(5)
        self.key=self.service.read('get_file_id',str(self.source)); self.service.submit('edit_curation',str(self.source),rating=5,add_tags=['curated']).result(5)
        setting=patch('modules.pickler.ui.view.QSettings',lambda *_:QSettings(str(self.root/'private.ini'),QSettings.IniFormat))
        setting.start(); self.addCleanup(setting.stop)
        self.bus=EventBus(); self.notices=[]; self.bus.subscribe('library_changed',self.notices.append)
        self.module=PicklerModule(); self.module.on_load(dict(data_service=self.service,event_bus=self.bus,pickler_journal_root=self.root/'journals'))
        self.view=self.module.get_view(); self.addCleanup(self.cleanup)
        self.view.load_images([str(self.source)]); self.assertTrue(pump(lambda:self.view._worker is None))
        self.assertTrue(pump(lambda:self.view._preview.shutdown_ready()))

    def cleanup(self):
        self.view.shutdown(); self.assertTrue(pump(self.view.shutdown_ready))
        self.view.close(); self.view.deleteLater(); app.processEvents()
        self.assertTrue(pump(native_threads_ready))

    def wait(self):self.assertTrue(pump(lambda:self.view._worker is None))

    def test_keep_move_confirms_before_counters_and_notices(self):
        self.view._pick_current(); self.wait()
        self.assertEqual(self.view._kept,1); self.assertEqual(self.view._images,[])
        receipt=self.view._last_op['receipt']; self.assertEqual(receipt['result_copy_id'],self.key)
        self.assertEqual(len(self.notices),2); self.assertFalse(self.source.exists())
        self.assertEqual(self.service.read('get_copy_version_state',self.key)['state']['file']['rating'],5)

    def test_copy_and_undo_keep_original_and_independent_offline_identity(self):
        self.view._copy_mode=True; self.view._pick_current(); self.wait()
        previous=self.view._last_op; self.assertEqual(self.view._kept,1); self.assertTrue(self.source.exists())
        self.assertNotEqual(previous['receipt']['result_copy_id'],self.key)
        self.view._undo(); self.wait()
        self.assertEqual(self.view._kept,0); self.assertIsNone(self.view._last_op)
        self.assertEqual(self.service.read('get_copy_version_state',previous['receipt']['result_copy_id'])['state']['file']['is_offline'],1)
        self.assertEqual(len(self.notices),4)

    def test_discard_and_undo_preserve_id_and_set_position(self):
        self.view._delete_current(); self.wait(); self.assertEqual(self.view._discarded,1)
        self.view._undo(); self.wait(); self.assertEqual(self.view._discarded,0)
        self.assertTrue(self.source.exists()); self.assertEqual(self.view._index,0); self.assertEqual(len(self.view._images),1)
        self.assertEqual(self.service.read('get_file_id',str(self.source.resolve())),self.key)

    def test_io_stays_off_gui_and_repeated_action_is_not_admitted(self):
        self.show_keyboard()
        entered=threading.Event(); release=threading.Event(); threads=[]; transfer=transfers.transfer
        self.addCleanup(release.set)
        incoming=self.folder/'incoming.png'; incoming.write_bytes(self.source.read_bytes())
        def blocked(*args,**kwargs):
            threads.append(threading.get_ident()); entered.set(); release.wait(4)
            return transfer(*args,**kwargs)
        with patch('modules.pickler.logic.transfers.transfer',side_effect=blocked):
            self.view._pick_current(); self.assertTrue(pump(entered.is_set))
            active=self.view._worker; self.view._pick_current(); self.view._next()
            for key, modifiers in ((Qt.Key_C,Qt.NoModifier),(Qt.Key_D,Qt.NoModifier),(Qt.Key_Z,Qt.ControlModifier)):
                QTest.keyClick(self.view,key,modifiers)
            messages=[]
            def dismiss(dialog):
                self.assertTrue(dialog.button(QMessageBox.Ok).toolTip())
                messages.append(dialog.text())
            with patch.object(QMessageBox,'exec',dismiss):
                self.module.load_image_set([str(self.folder/'incoming.png')])
            self.assertEqual(len(messages),1)
            self.assertIn('Send the set again',messages[0])
            self.assertEqual(self.view._dialogs,[])
            self.assertIs(self.view._worker,active); self.assertEqual(self.view._index,0)
            ticks=[]; QTimer.singleShot(0,lambda:ticks.append(True)); self.assertTrue(pump(lambda:bool(ticks),.5))
            self.assertFalse(self.view._btn_primary.isEnabled()); self.assertEqual(self.view._kept,0); self.assertEqual(self.notices,[])
            release.set(); self.wait()
        self.assertNotEqual(threads[0],threading.get_ident()); self.assertEqual(self.view._kept,1)
        self.assertEqual(self.view._images,[])
        self.module.load_image_set([str(incoming)]); self.wait()
        self.assertEqual(self.view._images,[incoming])

    def test_handoff_preserves_the_complete_input_order(self):
        incoming=self.folder/'incoming.png'; incoming.write_bytes(self.source.read_bytes())
        another=self.folder/'another.png'; another.write_bytes(self.source.read_bytes())
        paths=[incoming,self.source,another]
        self.module.load_image_set([str(path) for path in paths]); self.wait()
        self.assertEqual(self.view._images,paths)
        self.assertEqual(self.service.read('get_file_id',str(self.source)),self.key)
        self.assertEqual(self.view._index,0)


    def test_busy_handoff_warning_closes_with_view_without_starting_new_job(self):
        entered=threading.Event(); release=threading.Event(); transfer=transfers.transfer
        self.addCleanup(release.set)
        def blocked(*args,**kwargs):
            entered.set(); release.wait(4); return transfer(*args,**kwargs)
        with patch('modules.pickler.logic.transfers.transfer',side_effect=blocked):
            self.view._pick_current(); self.assertTrue(pump(entered.is_set))
            active=self.view._worker; seen=[]
            def close_warning():
                seen.append(len(self.view._dialogs)); self.module.on_unload()
            QTimer.singleShot(0,close_warning)
            self.module.load_image_set([str(self.folder/'incoming.png')])
            self.assertEqual(seen,[1]); self.assertIs(self.view._worker,active)
            self.assertEqual(self.view._dialogs,[])
            release.set(); self.assertTrue(pump(self.module.shutdown_ready))
        self.assertTrue(self.source.exists())
        self.assertEqual(self.view._images,[self.source])

    def test_failure_keeps_counters_set_and_undo_state(self):
        with patch('modules.pickler.logic.transfers.transfer',side_effect=OSError('private failure')):
            self.view._pick_current(); self.wait()
        self.assertEqual(self.view._kept,0); self.assertEqual(len(self.view._images),1)
        self.assertIsNone(self.view._last_op); self.assertEqual(self.notices,[])
        self.assertIn('private failure',self.view._canvas.lbl_info.text()); self.assertTrue(self.source.exists())

    def test_folder_scan_runs_on_worker_and_ignores_non_images(self):
        (self.folder/'notes.txt').write_text('not an image')
        self.view.load_folder(self.folder); self.wait()
        self.assertEqual(len(self.view._images),1); self.assertEqual(self.view._images[0].name,self.source.name)
        self.assertEqual(self.view._set_folder,self.folder.resolve())

    def test_close_cancels_before_publication_and_waits_actual_worker(self):
        entered=threading.Event(); release=threading.Event(); transfer=transfers.transfer
        self.addCleanup(release.set)
        def blocked(*args,**kwargs):
            entered.set(); release.wait(4); return transfer(*args,**kwargs)
        with patch('modules.pickler.logic.transfers.transfer',side_effect=blocked):
            self.view._pick_current(); self.assertTrue(pump(entered.is_set))
            started=time.monotonic(); self.module.on_unload()
            self.assertLess(time.monotonic()-started,.2); self.assertFalse(self.module.shutdown_ready())
            release.set(); self.assertTrue(pump(self.module.shutdown_ready))
        self.assertTrue(self.source.exists()); self.assertEqual(self.view._kept,0)

    def test_old_preview_instance_cannot_deliver_same_ticket_after_replacement(self):
        previous=self.view._preview; previous.close(); self.assertTrue(pump(previous.shutdown_ready))
        self.view._retired_previews.append(previous); self.view._preview=None; self.view._new_preview(); self.view._preview_ticket=1
        self.view._canvas.img.set_source(None)
        image=QImage(2,2,QImage.Format_RGB32)
        self.view._preview_ready(previous,1,{'image':image},None)
        self.assertIsNone(self.view._canvas.img._source)

    def test_close_while_preview_finishes_never_starts_a_transfer(self):
        entered=threading.Event(); release=threading.Event(); self.addCleanup(release.set)
        def slow(path,loader,event):
            entered.set(); release.wait(4); return None
        self.view._preview.loader=slow; self.view._refresh_view(); self.assertTrue(pump(entered.is_set))
        self.view._pick_current(); self.assertTrue(self.view._pending_start)
        self.view.shutdown(); self.assertFalse(self.view.shutdown_ready()); release.set()
        self.assertTrue(pump(self.view.shutdown_ready)); self.assertTrue(self.source.exists())
        self.assertFalse(self.service.read('get_transfer_status')['prepared'])

    def test_new_buttons_have_help_and_no_eager_transfer_schema(self):
        self.assertTrue(self.view._btn_recovery.toolTip()); self.assertFalse(self.service.read('get_transfer_status')['prepared'])
        for button in self.view.findChildren(QPushButton):self.assertTrue(button.toolTip(),button.text())

    def test_terminal_signal_does_not_release_worker_before_native_cleanup(self):
        entered=threading.Event(); release=threading.Event(); self.addCleanup(release.set)
        class DelayedCleanup(PicklerWorker):
            def run(worker):
                super().run()
                worker.resource=QObject()
                def cleanup():entered.set(); release.wait(4)
                worker.resource.destroyed.connect(cleanup,Qt.DirectConnection)
                worker.resource.deleteLater()
        with patch('modules.pickler.ui.view.PicklerWorker',DelayedCleanup):
            self.view._pick_current(); self.assertTrue(pump(entered.is_set))
            self.assertIsNotNone(self.view._worker); self.assertEqual(self.view._kept,0)
            self.assertFalse(self.view._btn_primary.isEnabled())
            self.assertEqual(self.service.read('get_copy_version_state',self.key)['state']['file']['rating'],5)
            release.set(); self.wait()
        self.assertEqual(self.view._kept,1)

    def test_review_uses_worker_and_does_not_create_native_schema(self):
        shown=[]
        with patch.object(self.view,'_show_recovery',side_effect=shown.append):
            self.view._start_job('review',()); self.wait()
        self.assertEqual(shown,[{'records':[],'truncated':False}])
        self.assertFalse(self.service.read('get_transfer_status')['prepared'])

    def test_nested_recovery_confirmation_closes_with_owned_view(self):
        result={'records':[dict(path=str(self.root/'fake.json'),request={'source_path':str(self.source),
            'result_path':str(self.root/'output.png')},phase='approved')],'truncated':False}
        def select():
            dialog=app.activeModalWidget(); listing=dialog.findChild(QListWidget); listing.setCurrentRow(0)
            button=next(b for b in dialog.findChildren(QPushButton) if b.text()=='Resolve selected operation')
            button.click()
        QTimer.singleShot(0,select); QTimer.singleShot(25,self.view.shutdown)
        self.view._show_recovery(result)
        self.assertEqual(self.view._dialogs,[]); self.assertTrue(pump(self.view.shutdown_ready))
        self.assertTrue(self.source.exists())

    def carousel(self):
        paths=[self.source]
        for name in ('second.png','third.png'):
            path=self.folder/name
            path.write_bytes(self.source.read_bytes())
            path.with_suffix('.txt').write_bytes(name.encode())
            paths.append(path)
        self.view.load_images([str(path) for path in paths]); self.wait()
        return paths

    def keys_ready(self):
        self.assertTrue(pump(lambda:self.view._decision_ready))

    def show_keyboard(self):
        self.view.resize(1100,850); self.view.show(); self.view.activateWindow()
        self.view._btn_skip.setFocus()
        app.processEvents(); self.keys_ready()

    def test_keyboard_discards_and_keeps_with_auto_advance_and_multiple_undo(self):
        paths=self.carousel(); self.show_keyboard()
        QTest.keyClick(self.view._btn_skip,Qt.Key_D); self.wait()
        self.assertEqual(self.view._images,[paths[1],paths[2]])
        self.assertTrue((self.folder/'discarded'/paths[0].name).exists())
        self.keys_ready()
        QTest.keyClick(self.view._canvas,Qt.Key_C); self.wait()
        self.assertEqual(self.view._images,[paths[2]])
        self.assertTrue((self.folder/'picked'/paths[1].name).exists())
        self.view._rb_copy.setChecked(True); self.keys_ready()
        QTest.keyClick(self.view._canvas,Qt.Key_C); self.wait()
        self.assertEqual(self.view._images,[]); self.assertTrue(paths[2].exists())
        self.assertEqual(self.view._keep_dest(),self.folder/'picked')
        self.assertEqual(self.view._discard_dest(),self.folder/'discarded')
        with patch('modules.pickler.ui.view.CachePaths.open_folder') as opened:
            self.view._open_dest(); self.view._open_discarded()
            self.assertEqual([call.args[0] for call in opened.call_args_list],
                             [self.folder/'picked',self.folder/'discarded'])
        self.assertEqual(len(self.view._undo_stack),3)
        self.assertIn('3',self.view._btn_undo.text())
        for index in (2,1,0):
            QTest.keyClick(self.view,Qt.Key_Z,Qt.ControlModifier); self.wait()
            self.assertEqual(self.view._images,paths[index:])
            self.assertEqual(self.view._images[self.view._index],paths[index])
            self.assertEqual(len(self.view._undo_stack),index)
        self.assertEqual((self.view._kept,self.view._discarded),(0,0))
        self.assertTrue(all(path.exists() and path.with_suffix('.txt').exists() for path in paths))
        self.assertEqual(self.service.read('get_file_id',str(self.source.resolve())),self.key)

    def test_keyboard_ignores_repeat_loading_modifiers_and_modal_dialog(self):
        paths=self.carousel(); self.show_keyboard()
        QTest.keyPress(self.view._canvas,Qt.Key_C); self.wait(); self.keys_ready()
        count=len(self.view._undo_stack)
        repeat=QKeyEvent(QEvent.KeyPress,Qt.Key_C,Qt.NoModifier,'c',True,10)
        QApplication.sendEvent(self.view._canvas,repeat); app.processEvents()
        self.assertIsNone(self.view._worker); self.assertEqual(len(self.view._undo_stack),count)
        QTest.keyRelease(self.view._canvas,Qt.Key_C)
        QTest.keyClick(self.view._canvas,Qt.Key_D,Qt.ControlModifier)
        self.assertIsNone(self.view._worker)
        self.view._next()
        QTest.keyClick(self.view._canvas,Qt.Key_D)
        self.assertIsNone(self.view._worker); self.assertEqual(len(self.view._undo_stack),count)
        self.keys_ready()
        dialog=QDialog(self.view); dialog.setModal(True); entry=QLineEdit(dialog)
        dialog.show(); entry.setFocus(); app.processEvents()
        QTest.keyClick(entry,Qt.Key_C)
        self.assertEqual(entry.text(),'c'); self.assertIsNone(self.view._worker)
        dialog.close(); dialog.deleteLater()
        self.assertTrue(paths[2].exists())

    def test_failed_undo_preserves_entire_history_and_new_input_clears_it(self):
        paths=self.carousel()
        self.view._delete_current(); self.wait()
        self.view._pick_current(); self.wait()
        pending=list(self.view._undo_stack)
        paths[1].write_bytes(b'occupied original path')
        self.view._undo(); self.wait()
        self.assertEqual(self.view._undo_stack,pending)
        self.assertEqual(paths[1].read_bytes(),b'occupied original path')
        self.assertEqual((self.view._kept,self.view._discarded),(1,1))
        self.view.load_images([str(paths[2])]); self.wait()
        self.assertEqual(self.view._undo_stack,[])
        self.assertEqual((self.view._kept,self.view._discarded),(0,0))
        self.assertFalse(self.view._btn_undo.isEnabled())

    def test_help_tracks_actual_destinations_transfer_setting_and_recovery(self):
        self.view._custom_dest=self.root/'chosen'; self.view._update_dest_label()
        self.assertIn(str(self.root/'chosen'),self.view._lbl_keys.text())
        self.assertIn('D:',self.view._lbl_keys.text()); self.assertIn('C:',self.view._lbl_keys.text())
        self.view._rb_copy.setChecked(True)
        self.assertIn('Copy',self.view._lbl_keys.text())
        self.view._rb_move.setChecked(True)
        self.assertIn('Move',self.view._lbl_keys.text())
        self.assertIn('Ctrl+Z',self.view._lbl_recover.text())
        self.assertIn('discarded/',self.view._lbl_recover.text())
        self.assertIn('closing Pickler',self.view._btn_undo.toolTip())

    def test_multiple_undo_restores_order_after_skipping_and_processing_from_middle(self):
        paths=self.carousel()
        self.view._next(); self.view._delete_current(); self.wait()
        self.view._copy_mode=True; self.view._pick_current(); self.wait()
        self.assertEqual(self.view._images,[paths[0]])
        self.view._undo(); self.wait()
        self.assertEqual(self.view._images,[paths[0],paths[2]])
        self.assertEqual(self.view._index,1)
        self.view._undo(); self.wait()
        self.assertEqual(self.view._images,paths); self.assertEqual(self.view._index,1)

    def test_unreadable_preview_never_admits_keyboard_decisions(self):
        broken=self.folder/'broken.png'; broken.write_bytes(b'not an image')
        self.view.load_images([str(broken)]); self.wait()
        self.view.show(); self.view.activateWindow(); app.processEvents()
        self.assertTrue(pump(lambda:'could not be displayed' in self.view._lbl_keyboard_state.text()))
        for key in (Qt.Key_C,Qt.Key_D):QTest.keyClick(self.view,key)
        self.assertIsNone(self.view._worker); self.assertFalse(self.view._decision_ready)
        self.assertTrue(broken.exists()); self.assertEqual(self.view._undo_stack,[])


if __name__=='__main__':unittest.main(verbosity=2)
