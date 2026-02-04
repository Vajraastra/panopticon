import sys
import os

# Add root to path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

print(f"Project root: {project_root}")

try:
    print("Attempting to import modules.dataset_scorer.module...")
    import modules.dataset_scorer.module as m
    print("Import successful!")
    
    print("Attempting to instantiate DatasetScorerModule...")
    instance = m.DatasetScorerModule()
    print("Instantiation successful!")

except Exception as e:
    print("\n❌ IMPORT FAILED!")
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
