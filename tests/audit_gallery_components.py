
import sys
import os
import time

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSize

def test_db_manager():
    print("\n[Audit] Testing DatabaseManager...")
    try:
        from modules.librarian.logic.db_manager import DatabaseManager
        db = DatabaseManager()
        folders = db.get_watched_folders()
        print(f"  [OK] Database initialized. Watched folders: {len(folders)}")
        
        # Test basic query
        count = db.conn.execute("SELECT count(*) FROM files").fetchone()[0]
        print(f"  [OK] File count in DB: {count}")
        return True
    except Exception as e:
        print(f"  [FAIL] DatabaseManager error: {e}")
        return False

def test_loader():
    print("\n[Audit] Testing ThumbnailLoader...")
    try:
        from modules.gallery.logic.loader import get_loader
        loader = get_loader()
        
        # We need a qapp for loader signals
        app = QApplication.instance() or QApplication(sys.argv)
        
        print("  [OK] Loader instance retrieved.")
        return True
    except Exception as e:
        print(f"  [FAIL] Loader error: {e}")
        return False

def test_imports():
    print("\n[Audit] Testing Module Imports...")
    fails = 0
    
    components = [
        "modules.gallery.module",
        "modules.librarian.module",
        "core.base_module",
        "core.components.standard_layout"
    ]
    
    for c in components:
        try:
            __import__(c, fromlist=[''])
            print(f"  [OK] Import {c}")
        except Exception as e:
            print(f"  [FAIL] Import {c}: {e}")
            fails += 1
            
    return fails == 0

if __name__ == "__main__":
    print("=== Gallery Component Audit ===")
    
    db_ok = test_db_manager()
    loader_ok = test_loader()
    imports_ok = test_imports()
    
    if db_ok and loader_ok and imports_ok:
        print("\n=== AUDIT PASSED: Components are ready for reuse ===")
        sys.exit(0)
    else:
        print("\n=== AUDIT FAILED: Some components are broken ===")
        sys.exit(1)
