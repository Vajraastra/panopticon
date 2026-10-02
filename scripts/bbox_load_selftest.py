"""BBox loading: private Windows/Qt images, original geometry and worker ownership."""
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
from PySide6.QtCore import QTimer, QRectF
from PySide6.QtWidgets import QApplication, QMessageBox
from modules.dataset_tagger.ui import bbox_editor as product
from modules.dataset_tagger.logic.ideogram import editor_load, layout
from modules.dataset_tagger.logic.ideogram.datamodel import WorkDoc, Element
from tagger_shutdown_selftest import pump


class BBoxLoad(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.image=self.root/'source.png'
        self.other=self.root/'other.png'
        self.out=self.root/'output'
        Image.new('RGB',(120,80),'red').save(self.image)
        Image.new('RGB',(80,60),'blue').save(self.other)
        self.widgets=[]; self.releases=[]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for event in self.releases:event.set()
        for widget in self.widgets:
            widget.reject()
            self.assertTrue(pump(widget.shutdown_ready))
            widget.deleteLater()
        QApplication.processEvents()
        self.temp.cleanup()

    def editor(self,ready=True):
        widget=product.BBoxEditor(self.image,layout.pano_path(self.out,self.image),
                                  images=[self.image,self.other],out_dir=self.out,
                                  style_defaults=dict(art_style='fixture style'))
        self.widgets.append(widget)
        if ready:self.assertTrue(pump(lambda:not widget._loading))
        return widget

    def test_initial_load_off_gui_blocks_actions_and_keeps_qt_responsive(self):
        entered,release=threading.Event(),threading.Event(); self.releases.append(release)
        real=editor_load.load; threads=[]
        def slow(*args):
            threads.append(threading.get_ident()); entered.set()
            self.assertTrue(release.wait(3)); return real(*args)
        with patch.object(editor_load,'load',side_effect=slow):
            start=time.monotonic(); widget=self.editor(False)
            self.assertLess(time.monotonic()-start,.3)
            self.assertTrue(entered.wait(1))
            self.assertFalse(widget._save())
            with patch.object(product,'_AutoDetectWorker') as factory:widget._auto_detect()
            factory.assert_not_called()
            widget._go(1); self.assertEqual(widget.image_path,self.image)
            ticks=[]; QTimer.singleShot(0,lambda:ticks.append(True))
            self.assertTrue(pump(lambda:bool(ticks)))
            release.set(); self.assertTrue(pump(lambda:not widget._loading))
        self.assertNotEqual(threads[0],threading.get_ident())
        self.assertTrue(widget._loaded)
        self.assertEqual(widget.scene.sceneRect(),QRectF(0,0,120,80))
        self.assertEqual(widget._doc.style.art_style,'fixture style')
        self.assertFalse(widget.pano_path.exists())

    def test_close_retains_initial_decoder_and_discards_late_result(self):
        entered,release=threading.Event(),threading.Event(); self.releases.append(release)
        real=editor_load.load
        def slow(*args):
            entered.set(); self.assertTrue(release.wait(3)); return real(*args)
        with patch.object(editor_load,'load',side_effect=slow):
            widget=self.editor(False); self.assertTrue(entered.wait(1))
            widget.reject(); self.assertFalse(widget.shutdown_ready())
            release.set(); self.assertTrue(pump(widget.shutdown_ready))
        self.assertFalse(widget._loaded)
        self.assertIsNone(widget._work_image)

    def test_full_original_coordinates_and_existing_draft_preserved(self):
        Image.new('RGB',(4096,3072),'red').save(self.image)
        path=layout.pano_path(self.out,self.image); path.parent.mkdir(parents=True)
        doc=WorkDoc(image=self.image.name,image_w=4096,image_h=3072)
        doc.elements=[Element(id=1,type='obj',bbox_px=[1024,768,2048,1536])]
        doc.save(path); before=path.read_bytes(); source=self.image.read_bytes()
        widget=self.editor()
        self.assertEqual(widget._pixmap.width(),4096)
        self.assertEqual(widget.scene.sceneRect().height(),3072)
        self.assertEqual(widget._items[0].element.bbox_px,[1024,768,2048,1536])
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.image.read_bytes(),source)

    def test_navigation_load_failure_keeps_old_context_then_retry(self):
        widget=self.editor(); self.other.write_bytes(b'not an image')
        with patch.object(QMessageBox,'warning') as warning:
            widget._go(1); self.assertTrue(pump(lambda:not widget._loading))
        warning.assert_called_once()
        self.assertEqual(widget.image_path,self.image)
        self.assertEqual(widget._index,0)
        self.assertEqual(widget._work_image.size,(120,80))
        self.assertTrue(widget.layout().itemAt(0).widget().isEnabled())
        Image.new('RGB',(80,60),'blue').save(self.other)
        widget._go(1); self.assertTrue(pump(lambda:not widget._loading))
        self.assertEqual(widget.image_path,self.other)
        self.assertEqual(widget._index,1)
        self.assertEqual(widget._doc.image_w,80)

    def test_corrupt_or_mismatched_draft_is_not_replaced_with_empty_work(self):
        path=layout.pano_path(self.out,self.image); path.parent.mkdir(parents=True)
        for raw in (b'broken',b'{"image":"other.png","image_w":120,"image_h":80}'):
            path.write_bytes(raw)
            with patch.object(QMessageBox,'warning') as warning:widget=self.editor()
            warning.assert_called_once()
            self.assertFalse(widget._loaded)
            self.assertFalse(widget._save())
            self.assertEqual(path.read_bytes(),raw)

    def test_draft_changed_during_read_is_rejected(self):
        path=layout.pano_path(self.out,self.image); path.parent.mkdir(parents=True)
        WorkDoc(image=self.image.name,image_w=120,image_h=80).save(path)
        real=WorkDoc.from_dict
        def changed(*args):
            doc=real(*args); path.write_bytes(b'changed during reading'); return doc
        with patch.object(WorkDoc,'from_dict',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'source_changed'):
                editor_load.load(self.image,path,{},lambda:False)

    def test_source_and_draft_resource_limits(self):
        path=layout.pano_path(self.out,self.image)
        with patch.object(editor_load,'MAX_EDITOR_PIXELS',100):
            with self.assertRaisesRegex(ValueError,'pixel_limit'):
                editor_load.load(self.image,path,{},lambda:False)
        with patch.object(editor_load,'MAX_SOURCE_BYTES',1):
            with self.assertRaisesRegex(ValueError,'file_limit'):
                editor_load.load(self.image,path,{},lambda:False)
        path.parent.mkdir(parents=True); path.write_bytes(b'{}')
        with patch.object(editor_load,'MAX_DRAFT_BYTES',1):
            with self.assertRaisesRegex(ValueError,'draft_limit'):
                editor_load.load(self.image,path,{},lambda:False)


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    unittest.main()
