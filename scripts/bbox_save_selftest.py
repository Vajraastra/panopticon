"""BBox save/publication: private Windows handles, drafts and Qt workers."""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PIL import Image
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QMessageBox
from modules.dataset_tagger.ui import bbox_editor as product
from modules.dataset_tagger.logic.ideogram import editor_load,editor_save,layout
from modules.dataset_tagger.logic.ideogram.datamodel import WorkDoc
from tagger_shutdown_selftest import pump


class BBoxSave(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source.png';self.other=self.root/'other.png';self.out=self.root/'output'
        Image.new('RGB',(120,80),'red').save(self.source)
        Image.new('RGB',(80,60),'blue').save(self.other)
        self.pano=layout.pano_path(self.out,self.source)
        self.widgets=[];self.releases=[];self.images=[]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for event in self.releases:event.set()
        for widget in self.widgets:
            widget.reject();self.assertTrue(pump(widget.shutdown_ready));widget.deleteLater()
        QApplication.processEvents()
        for image in self.images:image.close()
        self.temp.cleanup()

    def editor(self):
        widget=product.BBoxEditor(self.source,self.pano,images=[self.source,self.other],out_dir=self.out)
        self.widgets.append(widget);self.assertTrue(pump(lambda:not widget._loading))
        return widget

    def capture(self,composited=True):
        value=editor_load.load(self.source,self.pano,{},lambda:False)
        image=Image.new('RGB',(120,80),'blue');self.images.extend([image,value['image']])
        return [self.source,self.pano,self.out,value['doc'].to_dict(),image,composited,
                value['source_identity'],value['draft_identity'],value['draft_digest'],
                value['source_digest'],value['pinned_identity']]

    def test_slow_save_off_gui_blocks_reentry_and_closes_only_after_commit(self):
        widget=self.editor();widget._ed_hld.setPlainText('private caption')
        entered,release=threading.Event(),threading.Event();self.releases.append(release)
        real=editor_save.save;threads=[]
        def slow(*args):
            threads.append(threading.get_ident());entered.set();self.assertTrue(release.wait(3));return real(*args)
        with patch.object(editor_save,'save',side_effect=slow):
            start=time.monotonic();widget._save_and_close();self.assertLess(time.monotonic()-start,.3)
            self.assertTrue(entered.wait(1));self.assertTrue(widget._saving)
            self.assertFalse(widget._save());widget._go(1)
            self.assertEqual(widget.image_path,self.source)
            ticks=[];QTimer.singleShot(0,lambda:ticks.append(True));self.assertTrue(pump(lambda:bool(ticks)))
            release.set();self.assertTrue(pump(lambda:widget._closing and widget.shutdown_ready()))
        self.assertNotEqual(threads[0],threading.get_ident())
        self.assertEqual(WorkDoc.load(self.pano).high_level_description,'private caption')

    def test_navigation_waits_for_save_and_failed_save_keeps_context(self):
        widget=self.editor();widget._ed_hld.setPlainText('first')
        widget._go(1);self.assertTrue(pump(lambda:not widget._saving and not widget._loading))
        self.assertEqual(widget.image_path,self.other)
        self.assertEqual(WorkDoc.load(self.pano).high_level_description,'first')
        widget._ed_hld.setPlainText('second')
        with patch.object(editor_save.os,'rename',side_effect=OSError('disk failure')),patch.object(QMessageBox,'warning') as warning:
            widget._go(-1);self.assertTrue(pump(lambda:not widget._saving))
        warning.assert_called_once();self.assertEqual(widget.image_path,self.other)
        self.assertTrue(widget.layout().itemAt(0).widget().isEnabled())

    def test_new_composition_and_draft_committed_source_unchanged(self):
        args=self.capture();before=self.source.read_bytes()
        result=editor_save.save(*args,lambda:False)
        self.assertIsNone(result['error'],result)
        self.assertEqual(self.source.read_bytes(),before)
        with Image.open(self.out/self.source.name) as image:self.assertEqual(image.getpixel((0,0)),(0,0,255))
        self.assertEqual(WorkDoc.load(self.pano).image,self.source.name)
        self.assertEqual(result['draft_digest'],editor_save.digest(self.pano))
        self.assertFalse(list(self.out.rglob('.panopticon-bbox-*')))

    def test_existing_destination_and_source_aliases_never_replaced(self):
        for kind in ('occupied','hardlink','source'):
            args=self.capture();self.out.mkdir(exist_ok=True)
            output=self.out/self.source.name
            if kind=='occupied':output.write_bytes(b'other image')
            elif kind=='hardlink':os.link(self.source,output)
            else:args[2]=self.root;output=self.source
            before=output.read_bytes();source=self.source.read_bytes()
            result=editor_save.save(*args,lambda:False)
            self.assertIn('requires_new_output',result['error'])
            self.assertEqual(output.read_bytes(),before);self.assertEqual(self.source.read_bytes(),source)
            self.assertFalse(self.pano.exists())
            if kind!='source':output.unlink()

    def test_modified_draft_rejected_and_existing_draft_atomic_replace(self):
        self.pano.parent.mkdir(parents=True);doc=WorkDoc(image=self.source.name,image_w=120,image_h=80)
        doc.high_level_description='before';doc.save(self.pano)
        args=self.capture(False);args[3]['global']['high_level_description']='after'
        result=editor_save.save(*args,lambda:False)
        self.assertIsNone(result['error']);self.assertEqual(WorkDoc.load(self.pano).high_level_description,'after')
        args=self.capture(False);self.pano.write_bytes(b'foreign edit')
        result=editor_save.save(*args,lambda:False)
        self.assertIn('draft_changed',result['error']);self.assertEqual(self.pano.read_bytes(),b'foreign edit')

    def test_same_stat_source_changed_bytes_rejected(self):
        # BMP is fixed-size; preserve timestamps to prove fresh SHA verification.
        self.source=self.root/'source.bmp';Image.new('RGB',(120,80),'red').save(self.source)
        self.pano=layout.pano_path(self.out,self.source)
        args=self.capture(False);stamp=self.source.stat()
        Image.new('RGB',(120,80),'blue').save(self.source)
        os.utime(self.source,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        result=editor_save.save(*args,lambda:False)
        self.assertIsNotNone(result['error']);self.assertFalse(self.pano.exists())

    def test_encode_failure_and_cancel_before_publication_leave_no_output(self):
        args=self.capture();before=self.source.read_bytes()
        with patch.object(Image.Image,'save',side_effect=OSError('disk full')):
            result=editor_save.save(*args,lambda:False)
        self.assertIn('disk full',result['error']);self.assertFalse(self.pano.exists())
        event=threading.Event();real=Image.Image.save
        def save(*a,**kw):real(*a,**kw);event.set()
        with patch.object(Image.Image,'save',autospec=True,side_effect=save):
            result=editor_save.save(*args,event.is_set)
        self.assertTrue(result['cancelled']);self.assertIsNone(result['error'])
        self.assertFalse((self.out/self.source.name).exists());self.assertEqual(self.source.read_bytes(),before)
        self.assertFalse(list(self.out.rglob('.panopticon-bbox-*')))

    def test_destination_race_never_overwrites_winner(self):
        args=self.capture();real=Image.Image.save;output=self.out/self.source.name
        def save(*a,**kw):real(*a,**kw);output.write_bytes(b'winner')
        with patch.object(Image.Image,'save',autospec=True,side_effect=save):result=editor_save.save(*args,lambda:False)
        self.assertIsNotNone(result['error']);self.assertIsNone(result['published'])
        self.assertEqual(output.read_bytes(),b'winner');self.assertFalse(self.pano.exists())

    def test_failure_after_image_publish_preserves_output_and_recovery_draft(self):
        args=self.capture();real=editor_save.os.rename
        def rename(source,destination):
            if Path(destination).name==self.pano.name:raise OSError('draft locked')
            return real(source,destination)
        with patch.object(editor_save.os,'rename',side_effect=rename):result=editor_save.save(*args,lambda:False)
        self.assertIn('draft locked',result['error']);self.assertIsNotNone(result['published'])
        self.assertTrue(Path(result['published']).exists());self.assertFalse(self.pano.exists())
        self.assertEqual(WorkDoc.load(result['recovery']).image,self.source.name)
        remaining=list(self.out.rglob('.panopticon-bbox-*'))
        self.assertEqual(len(remaining),1)
        self.assertTrue(os.path.samefile(remaining[0],result['recovery']))

    def test_cancel_after_image_publish_finishes_draft(self):
        args=self.capture();real=editor_save.os.rename;event=threading.Event()
        def rename(source,destination):
            result=real(source,destination)
            if Path(destination).suffix!='.json':event.set()
            return result
        with patch.object(editor_save.os,'rename',side_effect=rename):result=editor_save.save(*args,event.is_set)
        self.assertIsNone(result['error']);self.assertFalse(result['cancelled'])
        self.assertTrue(self.pano.exists());self.assertTrue(Path(result['published']).exists())

    def test_source_handle_blocks_replacement_during_encode(self):
        args=self.capture();real=Image.Image.save
        def save(*a,**kw):
            with self.assertRaises(OSError):self.source.write_bytes(b'cannot alter pinned source')
            real(*a,**kw)
        with patch.object(Image.Image,'save',autospec=True,side_effect=save):result=editor_save.save(*args,lambda:False)
        self.assertIsNone(result['error'])

    def test_close_during_save_cancels_without_late_ui_or_gui_wait(self):
        widget=self.editor();entered,release=threading.Event(),threading.Event();self.releases.append(release)
        real=editor_save.save
        def slow(*args):entered.set();self.assertTrue(release.wait(3));return real(*args)
        with patch.object(editor_save,'save',side_effect=slow),patch.object(QMessageBox,'warning') as warning:
            widget._save_and_close();self.assertTrue(entered.wait(1))
            start=time.monotonic();widget.reject();self.assertLess(time.monotonic()-start,.1)
            self.assertFalse(widget.shutdown_ready());release.set();self.assertTrue(pump(widget.shutdown_ready))
        warning.assert_not_called();self.assertFalse(self.pano.exists())


    def test_partial_result_is_visible_without_success_or_navigation(self):
        widget=self.editor()
        result=dict(error='<failure>',cancelled=False,published='<image>',recovery='<draft>')
        with patch.object(QMessageBox,'warning') as warning:
            widget._saved(result)
        message=warning.call_args.args[2]
        self.assertIn('&lt;image&gt;',message);self.assertIn('&lt;draft&gt;',message)
        self.assertIn('do not repeat',message)
        self.assertFalse(widget._closing);self.assertEqual(widget.image_path,self.source)

    def test_existing_draft_replace_failure_leaves_old_bytes_intact(self):
        self.pano.parent.mkdir(parents=True)
        WorkDoc(image=self.source.name,image_w=120,image_h=80).save(self.pano)
        before=self.pano.read_bytes();args=self.capture(False)
        with patch.object(editor_save.os,'replace',side_effect=OSError('locked draft')):
            result=editor_save.save(*args,lambda:False)
        self.assertIn('locked draft',result['error']);self.assertEqual(self.pano.read_bytes(),before)
        self.assertFalse(list(self.out.rglob('.panopticon-bbox-*')))


    def test_concurrent_draft_saves_one_owner_and_stale_guard_is_preserved(self):
        self.pano.parent.mkdir(parents=True)
        WorkDoc(image=self.source.name,image_w=120,image_h=80).save(self.pano)
        args=self.capture(False);entered,release=threading.Event(),threading.Event()
        real=editor_save.PinnedFile.verify;results=[]
        def slow(*a,**kw):
            entered.set();self.assertTrue(release.wait(3));return real(*a,**kw)
        with patch.object(editor_save.PinnedFile,'verify',autospec=True,side_effect=slow):
            worker=threading.Thread(target=lambda:results.append(editor_save.save(*args,lambda:False)))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                competing=editor_save.save(*args,lambda:False)
                self.assertIsNotNone(competing['error'])
            finally:release.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertIsNone(results[0]['error'])
        guard=self.pano.with_name(self.pano.name+'.bbox.guard')
        self.assertFalse(guard.exists())
        guard.write_text('stale guard needs manual review',encoding='ascii')
        result=editor_save.save(*self.capture(False),lambda:False)
        self.assertIsNotNone(result['error'])
        self.assertEqual(guard.read_text(), 'stale guard needs manual review')


    def test_failure_after_draft_commit_is_reported_as_committed(self):
        args=self.capture(False);real=editor_save.digest;event=threading.Event()
        def fail(path):
            if Path(path).name==self.pano.name:
                event.set();raise OSError('postcommit read failed')
            return real(path)
        with patch.object(editor_save,'digest',side_effect=fail):result=editor_save.save(*args,event.is_set)
        self.assertIn('postcommit read failed',result['error'])
        self.assertFalse(result['cancelled'])
        self.assertTrue(os.path.samefile(result['draft_committed'],self.pano))
        self.assertEqual(WorkDoc.load(self.pano).image,self.source.name)


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    unittest.main()
