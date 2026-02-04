import sys
import os

# Add root to path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from core.mod_loader import ModuleLoader
from core.base_module import BaseModule

print(f"Testing ModuleLoader from {project_root}")

loader = ModuleLoader()
modules = loader.discover_modules()
print(f"Discovered modules: {modules}")

if "dataset_scorer" in modules:
    print("Attempting to load dataset_scorer via Loader...")
    instance = loader.load_module("dataset_scorer", context={})
    
    if instance:
        print(f"✅ SUCCESS: Module loaded. instance={instance}")
        print(f"   Name: {instance._name}")
    else:
        print("❌ FAILURE: Loader returned None.")
else:
    print("❌ FAILURE: dataset_scorer not found in discovery.")
