"""Entry-point selection must not depend on imported class names."""
from abc import abstractmethod
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.base_module import BaseModule
from core.mod_loader import ModuleLoader


class ModuleLoaderTests(unittest.TestCase):
    def setUp(self):
        self.module = ModuleType('modules.fixture.module')
        self.loader = ModuleLoader()

    def tool(self, name, origin=None):
        cls = type(name, (BaseModule,), {'__module__': origin or self.module.__name__})
        setattr(self.module, name, cls)
        return cls

    def load(self):
        with patch('core.mod_loader.importlib.import_module', return_value=self.module):
            return self.loader.load_module('fixture', {'private': True})

    def test_imported_subclass_does_not_replace_local_entry(self):
        self.tool('AImported', 'core.fixture')
        local = self.tool('ZActual')
        loaded = self.load()
        self.assertIs(type(loaded), local)
        self.assertEqual(loaded.context, {'private': True})

    def test_only_imported_subclass_is_not_a_module(self):
        self.tool('Imported', 'core.fixture')
        self.assertIsNone(self.load())
        self.assertNotIn('fixture', self.loader.loaded_modules)

    def test_ambiguous_entry_is_rejected_without_initialization(self):
        first = self.tool('First')
        second = self.tool('Second')
        with patch.object(first, '__init__') as a, patch.object(second, '__init__') as b:
            self.assertIsNone(self.load())
        a.assert_not_called()
        b.assert_not_called()

    def test_alias_of_same_class_does_not_create_ambiguity(self):
        local = self.tool('Actual')
        self.module.Alias = local
        self.assertIs(type(self.load()), local)

    def test_cached_module_keeps_its_instance(self):
        self.tool('Actual')
        first = self.load()
        with patch('core.mod_loader.importlib.import_module') as imported:
            self.assertIs(self.loader.load_module('fixture'), first)
            imported.assert_not_called()

    def test_failed_entry_is_not_cached(self):
        local = self.tool('Actual')
        with patch.object(local, 'on_load', side_effect=RuntimeError('fixture')):
            self.assertIsNone(self.load())
        self.assertNotIn('fixture', self.loader.loaded_modules)


if __name__ == '__main__':
    unittest.main()
