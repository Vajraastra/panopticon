
import os
import importlib
import inspect
from core.base_module import BaseModule

class ModuleLoader:
    """
    Handles dynamic discovery and loading of modules from the modules/ directory.
    """
    def __init__(self, modules_dir="modules"):
        self.modules_dir = modules_dir
        self.loaded_modules = {}

    def discover_modules(self):
        """
        Scans the modules directory for valid modules.
        Returns a list of module names (folders).
        """
        if not os.path.exists(self.modules_dir):
            return []
        
        folders = [f for f in os.listdir(self.modules_dir) 
                   if os.path.isdir(os.path.join(self.modules_dir, f)) 
                   and not f.startswith("__")]
        return folders

    def load_module(self, module_name):
        """
        Loads a specific module by name.
        """
        if module_name in self.loaded_modules:
            return self.loaded_modules[module_name]

        try:
            # Try to import the module's entry point (expected to be in modules/name/__init__.py or modules/name/main.py)
            # For simplicity, we'll look for modules/name/module.py and a class that inherits from BaseModule
            module_path = f"modules.{module_name}.module"
            imported_module = importlib.import_module(module_path)

            for name, obj in inspect.getmembers(imported_module):
                if inspect.isclass(obj) and issubclass(obj, BaseModule) and obj is not BaseModule:
                    instance = obj()
                    instance.on_load()
                    self.loaded_modules[module_name] = instance
                    return instance
        except Exception as e:
            print(f"Error loading module {module_name}: {e}")
            return None
        
        return None
