"""Exercise Separator review with a bounded batch and deterministic CCIP features."""
import os
from pathlib import Path
import sys
import tempfile
from time import monotonic, sleep
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtWidgets import QApplication

from core.ai.ccip import DIMENSIONS
from modules.character_separator.logic import recognition
from modules.character_separator.logic.review_store import ReviewStore
from modules.character_separator.module import CharacterSeparatorModule
from modules.character_separator.ui import view as separator_view


class FixtureCCIP:
    def embed(self, image, check_cancelled):
        check_cancelled()
        character, style, _ = image.getpixel((0, 0))
        vector = np.zeros(DIMENSIONS, dtype=np.float32)
        vector[character] = 1
        vector[32 + style] = 0.01
        return vector / np.linalg.norm(vector)

    def run(self, outputs, inputs):
        # Distinct styles land in separate discovery groups. Similarity to a
        # confirmed reference still identifies them as the same character.
        style = np.argmax(inputs["input"][:, 32:], axis=1)
        return [(style[:, None] != style[None, :]).astype(np.float32)]


class AutoscanReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        work = ROOT / "temp"
        work.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.source = root / "input"
        self.source.mkdir()
        self.store = ReviewStore(root / "reviews.db")
        settings_path = str(root / "settings.ini")
        for replacement in (
            patch.object(separator_view, "ReviewStore", return_value=self.store),
            patch.object(separator_view, "QSettings", side_effect=lambda *_: QSettings(
                settings_path, QSettings.Format.IniFormat,
            )),
            patch.object(recognition, "MAX_IMAGES", 4),
            patch.object(recognition, "CCIPEncoder", FixtureCCIP),
            patch.object(recognition, "metric_session", return_value=FixtureCCIP()),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        for number, (character, style) in enumerate(((0, 0), (0, 1), (0, 1), (1, 2),
                                                      (0, 3), (0, 3), (1, 4))):
            self.make_image(number, character, style)

    def make_image(self, number, character, style, size=(8, 8)):
        with Image.new("RGB", size, (character, style, 0)) as image:
            image.save(self.source / f"{number:02}.png")

    def wait_until(self, condition, message):
        deadline = monotonic() + 10
        while not condition() and monotonic() < deadline:
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            sleep(0.01)
        self.assertTrue(condition(), message)

    def wait_review(self):
        self.wait_until(lambda: self.view.job is None, "Separator review timed out")

    def open_review(self):
        module = CharacterSeparatorModule()
        module.on_load({})
        self.view = module.get_view()
        self.addCleanup(self.close_review, module)
        self.wait_review()
        return module

    def close_review(self, module):
        if module.view is not None:
            module.on_unload()
            self.wait_until(module.shutdown_ready, "Separator shutdown timed out")
            module.view.deleteLater()
            module.view = None
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def scan_next(self):
        self.assertTrue(self.view.scan_button.isEnabled())
        self.view.scan_button.click()
        self.wait_review()
        self.assertIsNotNone(self.view.result, self.view.status.text())

    def groups(self):
        return {
            frozenset(self.view.result.paths[index].name for index in group.members): group.suggested
            for group in self.view.groups.values()
        }

    def select_group(self, *filenames):
        for row, group in enumerate(self.view.groups.values()):
            if {self.view.result.paths[index].name for index in group.members} == set(filenames):
                self.view.group_list.setCurrentRow(row)
                return
        self.fail(f"Review group unavailable: {filenames}")

    def start_review(self):
        module = self.open_review()
        self.view.set_scope(self.source, self.source)
        self.wait_review()
        self.scan_next()
        self.assertEqual(self.view.result.excluded, 3)
        self.assertEqual(self.groups()[frozenset({"01.png", "02.png"})], "")
        return module

    def confirm_anchor(self):
        self.confirm_group_as("Alicia", "00.png")

    def confirm_group_as(self, name, *filenames):
        self.select_group(*filenames)
        self.view.name_edit.setText(name)
        self.view.approve_button.click()
        self.wait_review()
        self.assertIn(name, self.view.session.references())

    def select_character(self, name):
        self.view.tabs.setCurrentIndex(1)
        self.wait_review()
        characters = self.view.confirmed.character_list
        for row in range(characters.count()):
            if characters.item(row).data(Qt.ItemDataRole.UserRole) == name:
                characters.setCurrentRow(row)
                self.wait_review()
                return
        self.fail(f"Confirmed character unavailable: {name}")

    def shown_images(self):
        model = self.view.photo_gallery.model()
        return [model.index(row, 0).data(Qt.ItemDataRole.AccessibleTextRole) for row in range(model.rowCount())]

    def shown_index(self, filename):
        return self.view.photo_gallery.model().index(self.shown_images().index(filename), 0)

    def export_all(self):
        self.assertTrue(self.view.export_button.isEnabled())
        with patch.object(separator_view.CachePaths, "open_folder") as open_folder:
            self.view.export_button.click()
            self.wait_review()
            open_folder.assert_called_once_with(self.source)

    def test_switch_off_preserves_current_groups(self):
        self.start_review()
        self.assertFalse(self.view.autoscan.isChecked())
        self.confirm_anchor()
        self.assertEqual(self.groups()[frozenset({"01.png", "02.png"})], "")
        self.scan_next()
        self.assertEqual(self.groups()[frozenset({"04.png", "05.png"})], "Alicia")

    def test_assigning_one_photo_can_create_character_and_trigger_autoscan(self):
        self.start_review()
        self.view.autoscan.click()
        self.select_group("00.png")
        model = self.view.photo_gallery.model()
        self.view.photo_gallery.setCurrentIndex(model.index(0, 0))
        self.view.name_edit.setText("Alicia")
        self.assertTrue(self.view.assign_photo_button.isEnabled())
        self.view.assign_photo_button.click()
        self.wait_review()
        self.assertEqual(self.groups()[frozenset({"01.png", "02.png"})], "Alicia")

        self.select_group("01.png", "02.png")
        self.view.photo_gallery.doubleClicked.emit(model.index(0, 0))
        dialog = self.view.photo_review
        self.assertIsNotNone(dialog)
        self.wait_until(dialog.suggestion_button.isEnabled, "Photo preview timed out")
        dialog.suggestion_button.click()
        self.wait_review()
        self.assertEqual(self.view.session.decisions[self.source / "01.png"].name, "Alicia")
        self.assertEqual(self.groups()[frozenset({"02.png"})], "Alicia")

    def test_suggestions_remain_reviewable_through_batches_and_reopen(self):
        module = self.start_review()
        self.view.autoscan.click()
        self.confirm_anchor()
        self.assertEqual(self.groups(), {
            frozenset({"01.png", "02.png"}): "Alicia",
            frozenset({"03.png"}): "",
        })
        self.select_group("01.png", "02.png")
        self.assertIn("Alicia?", self.view.group_list.currentItem().text())
        model = self.view.photo_gallery.model()
        self.view.photo_gallery.setCurrentIndex(model.index(0, 0))
        self.assertTrue(self.view.exclude_photo_button.isEnabled())
        self.view.exclude_photo_button.click()
        self.wait_review()
        self.assertIsNone(self.view.session.decisions[self.source / "01.png"].name)
        self.assertTrue(self.view.suggested_button.isEnabled())
        self.assertIn("Alicia?", self.view.suggested_button.text())
        self.view.suggested_button.click()
        self.wait_review()
        self.assertEqual(self.view.session.decisions[self.source / "02.png"].name, "Alicia")

        self.scan_next()
        self.select_group("04.png", "05.png")
        self.assertIn("Alicia?", self.view.group_list.currentItem().text())
        self.assertTrue(self.view.reject_suggestion_button.isEnabled())
        self.view.reject_suggestion_button.click()
        self.wait_review()
        self.assertEqual(self.groups()[frozenset({"04.png", "05.png"})], "")
        self.assertNotIn(self.source / "04.png", self.view.session.decisions)
        self.assertFalse((self.source / "Alicia").exists())

        self.close_review(module)
        self.make_image(5, 0, 3, size=(12, 8))
        self.open_review()
        self.assertTrue(self.view.autoscan.isChecked())
        self.assertEqual(self.view.source, self.source)
        self.scan_next()
        self.assertEqual(self.groups()[frozenset({"04.png"})], "")
        self.assertEqual(self.groups()[frozenset({"05.png"})], "Alicia")

        self.export_all()
        self.assertEqual({path.name for path in (self.source / "Alicia").glob("*.png")},
                         {"00.png", "02.png", "05.png"}, self.view.status.text())
        self.assertEqual(len(list(self.source.glob("*.png"))), 7)

    def test_confirmed_gallery_can_reassign_one_image_and_keep_pending_selection(self):
        module = self.start_review()
        self.confirm_anchor()
        self.confirm_group_as("Alicia", "01.png", "02.png")
        self.confirm_group_as("Berta", "03.png")
        self.scan_next()
        self.select_group("04.png", "05.png")
        gallery = self.view.photo_gallery
        self.assertTrue(self.view.tabs.isAncestorOf(self.view.group_list))
        self.assertTrue(self.view.tabs.isAncestorOf(self.view.confirmed))
        self.assertFalse(self.view.tabs.isAncestorOf(gallery))
        pending = self.shown_images()

        self.select_character("Alicia")
        self.assertIs(self.view.photo_gallery, gallery)
        self.assertCountEqual(self.shown_images(), ["00.png", "01.png", "02.png"])
        photo = self.shown_index("01.png")
        self.assertFalse(photo.data(Qt.ItemDataRole.DecorationRole).isNull())
        gallery.doubleClicked.emit(photo)
        dialog = self.view.photo_review
        self.assertIsNotNone(dialog)
        self.wait_until(dialog.assign_button.isEnabled, "Confirmed image preview timed out")
        dialog.character_picker.setCurrentText("Berta")
        dialog.assign_button.click()
        self.wait_review()

        self.select_character("Alicia")
        self.assertCountEqual(self.shown_images(), ["00.png", "02.png"])
        self.select_character("Berta")
        self.assertCountEqual(self.shown_images(), ["01.png", "03.png"])
        self.view.tabs.setCurrentIndex(0)
        self.assertIs(self.view.photo_gallery, gallery)
        self.assertEqual(self.shown_images(), pending)

        self.close_review(module)
        self.open_review()
        self.select_character("Berta")
        self.assertCountEqual(self.shown_images(), ["01.png", "03.png"])
        self.assertEqual(self.view.session.decisions[self.source / "01.png"].name, "Berta")

    def test_reassigning_export_only_image_updates_verified_destinations(self):
        module = self.start_review()
        self.confirm_anchor()
        self.confirm_group_as("Berta", "03.png")
        self.export_all()
        self.close_review(module)

        module = self.open_review()
        self.select_character("Alicia")
        source = self.source / "04.png"
        old_copy = self.source / "Alicia" / source.name
        new_copy = self.source / "Berta" / source.name
        self.assertNotIn(source, self.view.session.decisions)
        self.assertEqual(self.view.session.receipts[source], ("Alicia", old_copy))
        self.view.photo_gallery.setCurrentIndex(self.shown_index(source.name))
        self.view.name_edit.setText("Berta")
        self.assertTrue(self.view.assign_photo_button.isEnabled())
        self.view.assign_photo_button.click()
        self.wait_review()
        self.assertEqual(self.view.session.decisions[source].name, "Berta")
        self.assertNotIn(source, self.view.session.receipts)
        self.assertFalse(new_copy.exists())

        self.close_review(module)
        self.open_review()
        self.select_character("Berta")
        self.assertIn(source.name, self.shown_images())
        self.assertNotIn(old_copy, self.view.session.destinations["Alicia"])
        self.export_all()
        self.assertEqual(self.view.session.receipts[source], ("Berta", new_copy))
        self.assertEqual(old_copy.read_bytes(), source.read_bytes())
        self.assertEqual(new_copy.read_bytes(), source.read_bytes())

    def test_moving_last_reference_refreshes_suggestions_for_new_character(self):
        self.start_review()
        self.view.autoscan.click()
        self.confirm_anchor()
        pending = frozenset({"01.png", "02.png"})
        self.assertEqual(self.groups()[pending], "Alicia")

        self.select_character("Alicia")
        self.view.photo_gallery.setCurrentIndex(self.shown_index("00.png"))
        self.view.name_edit.setText("Berta")
        self.view.assign_photo_button.click()
        self.wait_review()
        self.assertNotIn("Alicia", self.view.session.references())
        self.assertEqual(self.groups()[pending], "Berta")
        self.view.tabs.setCurrentIndex(0)
        self.select_group(*pending)
        self.assertIn("Berta?", self.view.suggested_button.text())

    def test_confirmed_gallery_loads_one_page_at_a_time(self):
        self.start_review()
        self.confirm_anchor()
        self.confirm_group_as("Alicia", "01.png", "02.png")
        self.confirm_group_as("Berta", "03.png")
        with patch.object(separator_view, "CONFIRMED_PAGE_SIZE", 2):
            self.select_character("Alicia")
            self.assertEqual(self.shown_images(), ["00.png", "01.png"])
            self.assertTrue(self.view.next_page.isEnabled())
            self.view.next_page.click()
            self.wait_review()
            self.assertEqual(self.shown_images(), ["02.png"])
            self.assertFalse(self.view.next_page.isEnabled())
            self.assertTrue(self.view.prev_page.isEnabled())
            self.select_character("Berta")
            self.assertEqual(self.shown_images(), ["03.png"])
            self.select_character("Alicia")
            self.assertEqual(self.shown_images(), ["00.png", "01.png"])


if __name__ == "__main__":
    unittest.main()
