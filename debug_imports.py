
import sys
import os

sys.path.append(os.getcwd())

print("Attempting to import modules.gallery.ui.viewer_window...")
try:
    from modules.gallery.ui import viewer_window
    print("SUCCESS: Module imported.")
except Exception as e:
    print(f"FAILED: {e}")
    import traceback
    traceback.print_exc()
