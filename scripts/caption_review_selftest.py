"""Caption review association/CAS with private files; no Native/model/network."""
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
from PIL import Image
from PySide6.QtWidgets import QApplication
from modules.dataset_tagger.logic import caption_edit, tag_tools
from modules.dataset_tagger.ui.review_view import ReviewView
from modules.dataset_tagger.logic import tag_db
from tagger_shutdown_selftest import pump


class CaptionReview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / 'a.png'
        self.caption = self.root / 'a.txt'
        Image.new('RGB', (20, 20), 'red').save(self.image)
        self.caption.write_text('old caption', encoding='utf-8')

    def test_edit_preserves_image_and_updates_snapshot(self):
        snap = caption_edit.load(self.caption)
        before = self.image.read_bytes()
        snap = caption_edit.write(self.caption, 'first edit', snap)
        caption_edit.write(self.caption, 'second edit', snap)
        self.assertEqual(self.image.read_bytes(), before)
        self.assertEqual(self.caption.read_text(), 'second edit')
        self.assertFalse(list(self.root.glob('*.guard')))
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_external_caption_edit_is_preserved(self):
        snap = caption_edit.load(self.caption)
        self.caption.write_text('external edit')
        with self.assertRaises(ValueError):
            caption_edit.write(self.caption, 'stale edit', snap)
        self.assertEqual(self.caption.read_text(), 'external edit')

    def test_replaced_image_is_rejected(self):
        snap = caption_edit.load(self.caption)
        Image.new('RGB', (20, 20), 'blue').save(self.image)
        with self.assertRaises(ValueError):
            caption_edit.write(self.caption, 'wrong edit', snap)
        self.assertEqual(self.caption.read_text(), 'old caption')

    def test_multiple_images_are_ambiguous(self):
        Image.new('RGB', (20, 20), 'blue').save(self.root / 'a.jpg')
        with self.assertRaises(ValueError):
            caption_edit.load(self.caption)

    def test_uppercase_image_extension_is_supported(self):
        upper = self.image.with_suffix('.PNG')
        self.image.rename(upper)
        self.assertEqual(caption_edit.load(self.caption)['image'], upper.resolve())

    def test_orphan_caption_is_not_written(self):
        self.image.unlink()
        with self.assertRaises(ValueError):
            tag_tools.write(self.caption, 'wrong edit')
        self.assertEqual(self.caption.read_text(), 'old caption')

    def test_protected_cherry_folder_is_rejected(self):
        (self.root / 'catalog.db').write_bytes(b'private marker')
        with self.assertRaises(ValueError):
            tag_tools.write(self.caption, 'wrong edit')
        self.assertEqual(self.caption.read_text(), 'old caption')

    def test_active_producer_guard_blocks_review(self):
        snap = caption_edit.load(self.caption)
        guard = self.caption.with_name(self.caption.name + '.caption.guard')
        guard.write_text('owned by producer')
        with self.assertRaises(FileExistsError):
            caption_edit.write(self.caption, 'wrong edit', snap)
        self.assertEqual(guard.read_text(), 'owned by producer')

    def test_disk_failure_preserves_caption(self):
        snap = caption_edit.load(self.caption)
        with patch.object(caption_edit.os, 'replace', side_effect=OSError('private disk failure')):
            with self.assertRaises(OSError):
                caption_edit.write(self.caption, 'wrong edit', snap)
        self.assertEqual(self.caption.read_text(), 'old caption')
        self.assertFalse(list(self.root.glob('*.guard')))
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_read_failure_never_becomes_empty_caption(self):
        self.caption.write_bytes(b'\xff')
        with self.assertRaises(UnicodeDecodeError):
            tag_tools.read(self.caption)

    def test_bulk_edits_unique_image_caption(self):
        self.caption.write_text('cat, dog')
        self.assertEqual(tag_tools.replace(self.root, 'cat', 'fox'), 1)
        self.assertEqual(self.caption.read_text(), 'fox, dog')

    def test_bulk_rejects_ambiguous_pair(self):
        Image.new('RGB', (20, 20), 'blue').save(self.root / 'a.jpg')
        with self.assertRaises(ValueError):
            tag_tools.replace(self.root, 'old', 'wrong', as_tag=False)
        self.assertEqual(self.caption.read_text(), 'old caption')

    def test_bulk_preserves_caption_changed_between_read_and_write(self):
        real = tag_tools.write
        def concurrent(path, text, snapshot):
            Path(path).write_text('external edit')
            return real(path, text, snapshot)
        with patch.object(tag_tools, 'write', side_effect=concurrent):
            with self.assertRaises(ValueError):
                tag_tools.replace(self.root, 'old', 'wrong', as_tag=False)
        self.assertEqual(self.caption.read_text(), 'external edit')


class ReviewUI(unittest.TestCase):
    def setUp(self):
        CaptionReview.setUp(self)
    def view(self):
        with patch.object(tag_db, 'is_ready', return_value=True):
            view = ReviewView({}, folder=self.root, as_tag=False)
        def cleanup():
            view._current = None
            view.close()
            self.assertTrue(pump(view.shutdown_ready))
            view.deleteLater()
            QApplication.processEvents()
        self.addCleanup(cleanup)
        return view

    def test_save_is_off_gui_and_close_finishes_admitted_write(self):
        view = self.view()
        view.editor.setPlainText('saved while closing')
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        real = tag_tools.write
        threads = []
        def slow(*args):
            threads.append(threading.get_ident())
            entered.set()
            self.assertTrue(release.wait(3))
            return real(*args)
        with patch.object(tag_tools, 'write', side_effect=slow):
            start = time.monotonic()
            view._save_current()
            self.assertLess(time.monotonic() - start, .3)
            self.assertTrue(entered.wait(1))
            QApplication.processEvents()
            view.close()
            self.assertFalse(view.shutdown_ready())
            release.set()
            self.assertTrue(pump(view.shutdown_ready))
        self.assertNotEqual(threads[0], threading.get_ident())
        self.assertEqual(self.caption.read_text(), 'saved while closing')

    def test_external_edit_failure_keeps_editor_and_navigation(self):
        view = self.view()
        view.editor.setPlainText('pending local text')
        self.caption.write_text('external text')
        other = self.root / 'b.txt'
        other.write_text('other text')
        Image.new('RGB', (20, 20), 'blue').save(self.root / 'b.png')
        view._load_folder(self.root)
        self.assertTrue(pump(lambda: view._review_io is None))
        self.assertEqual(view.editor.toPlainText(), 'pending local text')
        self.assertEqual(Path(view._current).resolve(), self.caption.resolve())
        self.assertEqual(self.caption.read_text(), 'external text')
        self.assertTrue(view.isEnabled())
        self.assertTrue(view.status.text())

    def test_successful_bulk_refreshes_editor_and_snapshot(self):
        view = self.view()
        view.edit_find.setText('old')
        view.edit_replace.setText('new')
        view._replace_all()
        self.assertTrue(pump(lambda: view._review_io is None))
        self.assertEqual(view.editor.toPlainText(), 'new caption')
        self.assertEqual(view._caption_snapshot['text'], 'new caption')
        view.editor.setPlainText('next edit')
        view._save_current()
        self.assertTrue(pump(lambda: view._review_io is None))
        self.assertEqual(self.caption.read_text(), 'next edit')

    def test_failed_close_preserves_pending_buffer_until_reload(self):
        view = self.view()
        view.editor.setPlainText('pending local text')
        self.caption.write_text('external text')
        view.close()
        self.assertTrue(pump(lambda: view._review_io is None))
        self.assertFalse(view._closing)
        self.assertEqual(view.editor.toPlainText(), 'pending local text')
        self.assertEqual(self.caption.read_text(), 'external text')
        view._select_caption(view._current)
        self.assertEqual(view.editor.toPlainText(), 'external text')

    def test_unchanged_buffer_never_overwrites_external_text(self):
        view = self.view()
        self.caption.write_text('external text')
        with patch.object(tag_tools, 'write') as write:
            self.assertTrue(view._save_current())
        write.assert_not_called()
        self.assertEqual(self.caption.read_text(), 'external text')

    def test_parent_shutdown_conflict_keeps_reload_usable(self):
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        self.addCleanup(parent.deleteLater)
        view = self.view()
        view.setParent(parent)
        view.editor.setPlainText('pending local text')
        self.caption.write_text('external text')
        parent.setEnabled(False)
        view.close()
        self.assertTrue(pump(lambda: view._review_io is None))
        self.assertIsNone(view.parentWidget())
        self.assertTrue(view.isEnabled())
        view._select_caption(view._current)
        view.close()
        self.assertTrue(pump(view.shutdown_ready))

    def test_navigation_saves_previous_caption_before_loading_next(self):
        other = self.root / 'b.txt'
        other.write_text('second caption')
        Image.new('RGB', (20, 20), 'blue').save(self.root / 'b.png')
        view = self.view()
        view.editor.setPlainText('first edit')
        view.file_list.setCurrentRow(1)
        self.assertTrue(pump(lambda: view._review_io is None))
        self.assertEqual(self.caption.read_text(), 'first edit')
        self.assertEqual(view.editor.toPlainText(), 'second caption')
        self.assertEqual(Path(view._current).resolve(), other.resolve())

    def test_changed_reference_never_reaches_suggestion_provider(self):
        view = self.view()
        view._endpoint = 'private://mock'
        view._vlm_model = 'mock'
        view.chk_suggest_image.setChecked(True)
        Image.new('RGB', (20, 20), 'blue').save(self.image)
        with patch('modules.dataset_tagger.ui.review_view.OpenAICompatProvider') as provider:
            view._on_suggest()
        provider.assert_not_called()


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    unittest.main()
