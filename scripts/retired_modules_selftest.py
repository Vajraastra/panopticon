"""Discovery/entry-point regression; private Qt/DB fixture and no active codecs."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from core.base_module import BaseModule
from core.mod_loader import ModuleLoader
from core.paths import ProjectPaths
from core.library_service import close_library_services
import main as product

ROOT=Path(__file__).resolve().parents[1]

class Retirement(unittest.TestCase):
    def test_discovery_excludes_retired_tool_without_imports(self):
        loader=ModuleLoader(str(ROOT/'modules'))
        with patch('core.mod_loader.importlib.import_module') as imported:
            names=loader.discover_modules()
        self.assertEqual(len(names),11)
        self.assertNotIn('format_converter',names)
        self.assertNotIn('format_scanner',names)
        self.assertNotIn('character_separator',names)
        self.assertIn('cropper',names)
        self.assertIn('image_optimizer',names)
        imported.assert_not_called()

    def test_explicit_load_cannot_restore_retired_tool_even_if_cached(self):
        loader=ModuleLoader(str(ROOT/'modules'))
        for name in ('format_converter','character_separator','format_scanner'):
            with self.subTest(name=name):
                loader.loaded_modules[name]=object()
                with patch('core.mod_loader.importlib.import_module') as imported:
                    self.assertIsNone(loader.load_module(name,{}))
                imported.assert_not_called()

    def test_separator_cache_left_after_checkout_does_not_create_a_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=Path(directory)/'character_separator'/'__pycache__'
            cache.mkdir(parents=True)
            (cache/'module.cpython-313.pyc').write_bytes(b'stale bytecode')
            self.assertEqual(ModuleLoader(directory).discover_modules(),[])

    def test_existing_output_and_legacy_sources_remain_untouched(self):
        legacy=(ROOT/'modules/format_converter/logic/converter.py').read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'converted.png'
            output.write_bytes(b'existing export')
            ModuleLoader(str(ROOT/'modules')).discover_modules()
            self.assertEqual(output.read_bytes(),b'existing export')
        self.assertEqual((ROOT/'modules/format_converter/logic/converter.py').read_bytes(),legacy)

    def test_dashboard_uses_only_active_tools_and_does_not_build_views(self):
        class Tool(BaseModule):
            def __init__(self,index):
                super().__init__()
                self._name=f'Test {index}'
            def get_view(self):raise AssertionError('eager view')
        with tempfile.TemporaryDirectory() as directory, patch.object(ProjectPaths,'root',return_value=Path(directory)):
            loader=ModuleLoader(str(ROOT/'modules'))
            calls=[]
            def load(name,context):
                calls.append(name)
                instance=Tool(len(calls))
                instance.on_load(context)
                return instance
            with patch.object(product,'ModuleLoader',return_value=loader),patch.object(loader,'load_module',side_effect=load),patch.object(product,'close_library_services',side_effect=lambda **kwargs:close_library_services()):
                window=product.MainWindow()
                try:
                    self.assertEqual(len(window.loaded_modules),11)
                    self.assertEqual(window.module_grid_count,11)
                    self.assertNotIn('format_converter',calls)
                    self.assertNotIn('character_separator',calls)
                    self.assertFalse(window.module_windows)
                finally:
                    window.close()
                    deadline = time.monotonic() + 5
                    while not window._shutdown_finished and time.monotonic() < deadline:
                        QApplication.processEvents()
                        time.sleep(.005)
                    self.assertTrue(window._shutdown_finished, 'dashboard shutdown did not drain')
                    window.deleteLater()
                    QApplication.processEvents()
            close_library_services()

if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    unittest.main(verbosity=2)
