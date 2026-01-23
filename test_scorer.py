
import os
import shutil
import tempfile
import sys

# Add project root to path
sys.path.append(os.getcwd())

from modules.workshop.logic.face_scorer import sort_files_by_score

def test_sorting_logic():
    print("Running Face Scorer Sorting Test...")
    
    # Create temp dir
    temp_dir = tempfile.mkdtemp()
    print(f"Created temp dir: {temp_dir}")
    
    try:
        # Create dummy images
        files = {
            "img_100.png": 100,
            "img_92.png": 92,
            "img_85.png": 85,
            "img_55.png": 55,
            "img_40.png": 40, # Below 50 threshold
        }
        
        results = []
        for name, score in files.items():
            path = os.path.join(temp_dir, name)
            with open(path, 'w') as f:
                f.write("dummy content")
            
            results.append({
                "path": path,
                "composite_score": score
            })
            
        print(f"Created {len(files)} dummy files.")
        
        # Test 1: Sort with threshold 50
        print("\nTest 1: Sorting with threshold 50...")
        stats = sort_files_by_score(results, threshold=50, base_folder=temp_dir, move_files=True)
        
        print(f"Moved: {stats['total_moved']}, Errors: {stats['errors']}")
        print(f"Counts: {stats['moved_counts']}")
        
        # Verify file structure
        expected_moves = {
            "100%": ["img_100.png"],
            "90%": ["img_92.png"],
            "80%": ["img_85.png"],
            "50%": ["img_55.png"]
        }
        
        for folder, expected_files in expected_moves.items():
            folder_path = os.path.join(temp_dir, folder)
            if not os.path.exists(folder_path):
                print(f"❌ Error: Folder {folder} not created.")
                continue
                
            actual_files = os.listdir(folder_path)
            for f in expected_files:
                if f in actual_files:
                    print(f"[OK] Found {f} in {folder}")
                else:
                    print(f"[FAIL] Error: {f} not found in {folder}")
        
        # Verify 40% stayed in root
        if os.path.exists(os.path.join(temp_dir, "img_40.png")):
            print("[OK] img_40.png stayed in root (correct).")
        else:
            print("[FAIL] Error: img_40.png was moved/deleted.")
            
        print("\nTest passed if all checks above are [OK].")
        
    finally:
        # Cleanup
        try:
            shutil.rmtree(temp_dir)
            print(f"Cleaned up temp dir.")
        except:
            pass

if __name__ == "__main__":
    test_sorting_logic()
