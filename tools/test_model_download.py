import sys
import os
import shutil

# Add root
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from core.paths import CachePaths
from modules.dataset_scorer.logic.dataset_scorer import get_yolo_model

def test_download():
    print("🧪 Testing Anime Model Download...")
    
    # Ensure clean state (optional, but good for testing fix)
    # models_dir = CachePaths.get_models_root() / "yolo"
    # anime_model = models_dir / "yolov8n-anime.pt"
    # if anime_model.exists():
    #     print(f"Removing existing model for test: {anime_model}")
    #     anime_model.unlink()
    
    try:
        print("Invoking get_yolo_model('anime')...")
        model = get_yolo_model("anime")
        print("✅ Model loaded successfully!")
        
        # Verify file exists
        models_dir = CachePaths.get_models_root() / "yolo"
        anime_model = models_dir / "yolov8n-anime.pt"
        if anime_model.exists():
            size_mb = anime_model.stat().st_size / (1024*1024)
            print(f"📦 File exists: {anime_model} ({size_mb:.2f} MB)")
        else:
            print("❌ File does not exist after load!")
            
    except Exception as e:
        print(f"❌ Failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_download()
