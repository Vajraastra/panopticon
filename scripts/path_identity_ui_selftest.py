"""Private SQL review pages, Qt responsiveness, locale controls and real close."""
from contextlib import closing
import json
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
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QPushButton
from shiboken6 import isValid
from core.library_service import LibraryService, LibraryError
from core.components.path_identity_review import PathIdentityReviewDialog
from modules.librarian.module import LibrarianModule

app=QApplication.instance() or QApplication([])
QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
app.setFont(QFont('Segoe UI',10))


def pump(condition,timeout=4):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        app.processEvents()
        if condition():
            return True
        time.sleep(.005)
    return condition()


class Review(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.service=LibraryService(self.root/'private.db')
        self.addCleanup(self.service.close)
        self.dialogs=[]
        self.addCleanup(self.close_dialogs)

    def close_dialogs(self):
        for dialog in self.dialogs:
            if isValid(dialog):
                dialog.close()
        self.assertTrue(pump(lambda:all(not isValid(d) or d.shutdown_ready() for d in self.dialogs)))
        app.processEvents()

    def register(self,paths):
        self.service.submit('register_files_minimal',[dict(path=str(p),filename=Path(p).name,size=7,created=1) for p in paths]).result(3)

    def add(self,name):
        path=self.root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'private')
        self.register([path])
        return path

    def dialog(self,language='es',**kwargs):
        locale=json.loads((Path(__file__).resolve().parents[1]/'locales'/f'{language}.json').read_bytes())
        def translate(key,default):
            self.assertIn(key,locale)
            return locale[key]
        dialog=PathIdentityReviewDialog(self.service,translate,**kwargs)
        self.dialogs.append(dialog)
        dialog.show()
        self.assertTrue(pump(lambda:not dialog._busy and dialog.reader.shutdown_ready() and dialog.prepare_button.isEnabled()))
        return dialog

    def test_unprepared_issues_exclude_offline_all_retains_signed_ids_and_ratings(self):
        one=self.add('one.png')
        two=self.add('two.png')
        self.service.submit('edit_curation',str(one),rating=5,add_tags=['keep']).result(3)
        self.service.submit('remove_files',[str(two)]).result(3)
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute("INSERT INTO files(id,path,filename) VALUES(-2,'C:/private/negative.png','negative.png')")
            conn.execute("INSERT INTO files(id,path,filename) VALUES(0,'C:/private/zero.png','zero.png')")
            conn.commit()
        issues=self.service.read('get_path_identity_review_page')
        self.assertEqual([r[0] for r in issues['records'][:2]],[-2,0])
        self.assertEqual(len(issues['records']),3)
        all_rows=self.service.read('get_path_identity_review_page',scope='all')['records']
        self.assertEqual(len(all_rows),4)
        self.assertEqual(all_rows[-2][2],5)
        self.assertEqual(all_rows[-1][4],'offline')

    def test_conflicts_keep_both_ids_and_live_curation_hardlinks_not_conflicts(self):
        one=self.add('Árbol/Mixed.png')
        alias=self.root/'árbol'/'mixed.png'
        self.register([alias])
        linked=self.root/'Distinct.png'
        os.link(one,linked)
        self.register([linked])
        self.service.submit('edit_curation',str(one),rating=5,add_tags=['keep']).result(3)
        self.service.backfill_path_identity()
        rows=self.service.read('get_path_identity_review_page')['records']
        self.assertEqual([r[4] for r in rows],['conflict','conflict'])
        self.assertEqual([r[2] for r in rows],[5,0])
        self.assertEqual(rows[0][5],rows[1][5])
        self.service.submit('remove_files',[str(alias)]).result(3)
        self.assertEqual(self.service.read('get_path_identity_review_page')['records'],[])

    def test_review_pages_bound_and_do_not_approve_a_truncated_inventory(self):
        self.register([f'C:/private/{i}.png' for i in range(205)])
        pages=[]
        page=self.service.read('get_path_identity_review_page',scope='all')
        upper=page['upper_id']
        self.register(['C:/private/late.png'])
        while True:
            pages.append(page)
            if not page['more']:
                break
            page=self.service.read('get_path_identity_review_page',scope='all',after_id=page['after_id'],upper_id=upper)
        self.assertEqual([len(p['records']) for p in pages],[100,100,5])
        self.assertEqual(len({r[0] for p in pages for r in p['records']}),205)
        for kwargs in ({'limit':101},{'limit':True},{'scope':'guess'},{'after_id':True}):
            with self.assertRaises(LibraryError):
                self.service.read('get_path_identity_review_page',**kwargs)

    def test_bilingual_buttons_and_lookup_do_not_register_or_change_file(self):
        one=self.add('Árbol/Mixed.png')
        self.service.backfill_path_identity()
        for language in ('es','en'):
            dialog=self.dialog(language)
            for button in dialog.findChildren(QPushButton):
                self.assertTrue(button.text())
                self.assertTrue(button.toolTip())
            self.assertFalse(dialog.resolve_button.isEnabled())
            dialog.path.setText(str(self.root/'árbol'/'mixed.png'))
            self.assertTrue(dialog.resolve_button.isEnabled())
            dialog.resolve_button.click()
            self.assertTrue(pump(lambda:not dialog._busy and dialog.reader.shutdown_ready()))
            self.assertEqual(dialog.table.rowCount(),1)
            self.assertEqual(self.service.read('count_files'),1)
            self.assertEqual(one.read_bytes(),b'private')
            emitted=[]
            dialog.review_copies_requested.connect(lambda:emitted.append(True))
            dialog.copies_button.click()
            self.assertEqual(emitted,[True])
            dialog.close()
            app.processEvents()

    def test_prepare_requires_explicit_click_and_runs_outside_gui(self):
        self.add('one.png')
        dialog=self.dialog()
        self.assertFalse(self.service.read('get_path_identity_status')['prepared'])
        original=self.service.backfill_path_identity
        calls=[]
        def worker(**kwargs):
            calls.append(threading.get_ident())
            return original(**kwargs)
        busy=[]
        dialog.busy_changed.connect(busy.append)
        with patch.object(self.service,'backfill_path_identity',side_effect=worker):
            dialog.prepare_button.click()
            self.assertTrue(pump(lambda:busy==[True,False] and not dialog._busy and dialog.reader.shutdown_ready()))
        self.assertEqual(len(calls),1)
        self.assertNotEqual(calls[0],threading.get_ident())
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],0)
        self.assertEqual(len(list(self.root.glob('*.pre-path-identity-*.bak'))),1)

    def test_stop_keeps_confirmed_page_and_rejects_double_start(self):
        self.add('one.png')
        self.add('two.png')
        dialog=self.dialog()
        entered=threading.Event()
        original=self.service.backfill_path_identity
        calls=[]
        def worker(**kwargs):
            calls.append(True)
            original_progress=kwargs['progress']
            def progress(row):
                original_progress(row)
                entered.set()
                kwargs['cancel_event'].wait(3)
            kwargs['progress']=progress
            return original(limit=1,**kwargs)
        with patch.object(self.service,'backfill_path_identity',side_effect=worker):
            dialog.prepare()
            self.assertTrue(pump(entered.is_set))
            dialog.prepare()
            dialog.stop_button.click()
            self.assertTrue(pump(lambda:not dialog._busy and dialog.reader.shutdown_ready()))
        self.assertEqual(calls,[True])
        self.assertEqual(self.service.read('get_path_identity_status')['pending'],1)

    def test_delivery_before_native_exit_does_not_release_busy_or_destroy(self):
        self.add('one.png')
        dialog=self.dialog()
        entered,release=threading.Event(),threading.Event()
        def hold(ticket,value,error):
            if isinstance(value,dict) and 'confirmed' in value:
                entered.set()
                release.wait(3)
        dialog.reader._delivered.connect(hold,Qt.DirectConnection)
        busy=[]
        dialog.busy_changed.connect(busy.append)
        dialog.prepare()
        try:
            self.assertTrue(pump(lambda:entered.is_set() and not dialog._preparing))
            self.assertEqual(busy,[True])
            self.assertFalse(dialog.prepare_button.isEnabled())
            dialog.close()
            app.processEvents()
            self.assertTrue(isValid(dialog))
            self.assertFalse(dialog.shutdown_ready())
            dialog._progressed(dialog._token,dict(confirmed=999))
        finally:
            release.set()
        self.assertTrue(pump(lambda:not isValid(dialog)))

    def test_followup_waits_for_actual_reader_exit_instead_of_losing_first_page(self):
        self.add('one.png')
        dialog=self.dialog()
        entered,release=threading.Event(),threading.Event()
        def hold(ticket,value,error):
            if isinstance(value,dict) and 'prepared' in value:
                entered.set()
                release.wait(3)
        dialog.reader._delivered.connect(hold,Qt.DirectConnection)
        dialog.table.setRowCount(0)
        dialog.refresh()
        try:
            self.assertTrue(pump(lambda:entered.is_set() and not dialog._busy))
            time.sleep(.04)
            app.processEvents()
            self.assertEqual(dialog.table.rowCount(),0)
        finally:
            release.set()
        self.assertTrue(pump(lambda:dialog.table.rowCount()==1 and not dialog._busy and dialog.reader.shutdown_ready()))

    def test_busy_guard_does_not_prepare_while_indexing(self):
        self.add('one.png')
        dialog=self.dialog(can_prepare=lambda:False)
        dialog.prepare()
        self.assertFalse(dialog._preparing)
        self.assertFalse(self.service.read('get_path_identity_status')['prepared'])

    def test_lookup_preserves_spelling_instead_of_stripping_ambiguous_spaces(self):
        one=self.add('one.png')
        self.service.backfill_path_identity()
        dialog=self.dialog()
        observed=[]
        original=self.service.resolve_path_identity
        def resolve(path,**kwargs):
            observed.append(path)
            return original(path,**kwargs)
        entered=str(one)+' '
        with patch.object(self.service,'resolve_path_identity',side_effect=resolve):
            dialog.path.setText(entered)
            dialog.resolve()
            self.assertTrue(pump(lambda:not dialog._busy and dialog.reader.shutdown_ready()))
        self.assertEqual(observed,[entered])
        self.assertEqual(dialog.path.text(),entered)
        self.assertEqual(dialog.table.rowCount(),0)
        self.assertEqual(self.service.read('count_files'),1)

    def test_slow_disk_lookup_keeps_qt_responsive_and_close_waits(self):
        one=self.add('one.png')
        self.service.backfill_path_identity()
        dialog=self.dialog()
        entered,release=threading.Event(),threading.Event()
        from core.library_path_migration import observe_path
        def slow(path,**kwargs):
            entered.set()
            release.wait(3)
            return observe_path(path,**kwargs)
        ticks=[]
        timer=QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda:ticks.append(True))
        timer.start()
        with patch('core.library_path_migration.observe_path',side_effect=slow):
            dialog.path.setText(str(one))
            dialog.resolve()
            try:
                self.assertTrue(pump(lambda:entered.is_set() and len(ticks)>=6))
                self.assertTrue(dialog._busy)
                dialog.close()
                app.processEvents()
                self.assertTrue(isValid(dialog))
                self.assertFalse(dialog.shutdown_ready())
            finally:
                release.set()
                timer.stop()
            self.assertTrue(pump(lambda:not isValid(dialog)))

    def test_librarian_integration_busy_and_unload_close_native_dialog(self):
        self.add('one.png')
        module=LibrarianModule()
        module._auto_scan_pending=False
        module.on_load({'data_service':self.service})
        view=module.get_view()
        self.addCleanup(module.on_unload)
        self.assertTrue(module.btn_path_identity.toolTip())
        module.review_path_identities()
        dialog=module._identity_dialog
        self.dialogs.append(dialog)
        self.assertTrue(pump(lambda:not dialog._busy and dialog.reader.shutdown_ready()))
        module._identity_busy_changed(True)
        self.assertFalse(module._can_write_folder())
        self.assertFalse(module.btn_scan.isEnabled())
        module.toggle_scan()
        self.assertIsNone(module._scan_request)
        module._identity_busy_changed(False)
        module.on_unload()
        self.assertTrue(pump(module.shutdown_ready))
        view.close()


if __name__=='__main__':
    unittest.main(verbosity=2)
