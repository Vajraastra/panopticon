"""Lazy module handoff contract; no model loading or library writes."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.dataset_tagger import module as tagger
from modules.character_recognizer import module as recognizer


class ConsumerHandoff(unittest.TestCase):
    def targets(self):
        return ((tagger, tagger.DatasetTaggerModule, 'DatasetTaggerView'),
                (recognizer, recognizer.CharacterRecognizerModule, 'CharacterRecognitionView'))

    def test_first_delivery_constructs_view_and_passes_complete_ordered_selection(self):
        for owner, module_type, view_name in self.targets():
            with self.subTest(module=module_type.__name__), patch.object(owner, view_name) as factory:
                view = factory.return_value
                module = module_type()
                module.on_load({})
                self.assertIsNone(module.view)
                paths = ['z.png', 'a.png', 'z.png']
                module.load_image_set(paths)
                factory.assert_called_once_with(module.context)
                view.load_images.assert_called_once_with(paths)
                self.assertIs(module.get_view(), view)
                module.load_image_set(['next.png'])
                factory.assert_called_once()
                view.load_images.assert_called_with(['next.png'])
                module.on_unload()
                view.load_images.reset_mock()
                module.load_image_set(paths)
                view.load_images.assert_not_called()

    def test_empty_and_post_shutdown_delivery_never_create_view(self):
        for owner, module_type, view_name in self.targets():
            with self.subTest(module=module_type.__name__), patch.object(owner, view_name) as factory:
                module = module_type()
                module.on_load({})
                module.load_image_set([])
                factory.assert_not_called()
                module.on_unload()
                module.load_image_set(['late.png'])
                factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
