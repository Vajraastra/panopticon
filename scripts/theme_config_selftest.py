"""Persistencia del tema con archivos temporales, sin preferencias del usuario."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.theme_manager import ThemeManager


class ThemeConfig(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'theme.json'

    def test_roundtrip_keeps_theme_and_only_custom_colors(self):
        theme = ThemeManager(self.path)
        theme.set_theme('grape')
        theme.set_color('accent_main', '#112233')
        saved = json.loads(self.path.read_text())
        self.assertEqual(set(saved), {'current_theme', 'palette_version', 'accent_main'})
        loaded = ThemeManager(self.path)
        self.assertEqual(loaded.current_theme, 'grape')
        self.assertEqual(loaded.get_color('accent_main'), '#112233')

    def test_failed_replace_keeps_previous_preference_and_cleans_temp(self):
        theme = ThemeManager(self.path)
        theme.set_theme('grape')
        before = self.path.read_bytes()
        with patch('core.theme_manager.os.replace', side_effect=OSError('denied')):
            with self.assertLogs('core.theme_manager', level='WARNING'):
                theme.set_theme('light')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invalid_json_shape_does_not_break_startup(self):
        for content in ('[]', 'null', '"theme"', '{'):
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertLogs('core.theme_manager', level='WARNING'):
                    theme = ThemeManager(self.path)
                self.assertEqual(theme.current_theme, 'cyberpunk')
                self.assertEqual(self.path.read_text(), content)

    def test_palette_upgrade_keeps_selected_theme(self):
        self.path.write_text(json.dumps({'current_theme': 'grape', 'palette_version': 1,
                                         'accent_main': '#000000'}))
        theme = ThemeManager(self.path)
        self.assertEqual(theme.current_theme, 'grape')
        self.assertEqual(theme.colors, ThemeManager.THEMES['grape'])
        self.assertEqual(json.loads(self.path.read_text())['palette_version'], ThemeManager.PALETTE_VERSION)


if __name__ == '__main__':
    unittest.main()
