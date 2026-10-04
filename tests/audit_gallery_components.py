
import sys
import os
import tempfile
from pathlib import Path

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PySide6.QtWidgets import QApplication

def test_db_manager():
    print("\n[Audit] Testing DatabaseManager...")
    try:
        from core.database_manager import DatabaseManager
        from core.library_service import LibraryService
        with tempfile.TemporaryDirectory() as directory:
            service = LibraryService(Path(directory) / 'fixture.db')
            try:
                db = DatabaseManager(service=service)
                assert db.get_watched_folders() == []
                assert db.count_files() == 0
                print("  [OK] Temporary library is empty and readable.")
            finally:
                service.close()
        return True
    except Exception as e:
        print(f"  [FAIL] DatabaseManager error: {e}")
        return False

def test_loader():
    print("\n[Audit] Testing ThumbnailLoader...")
    try:
        from modules.gallery.logic.loader import get_loader
        # Qt must exist before constructing its objects.
        app = QApplication.instance() or QApplication(sys.argv)
        loader = get_loader()
        loader.shutdown()
        assert loader.shutdown_ready()
        
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
