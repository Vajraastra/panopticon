"""
Script de Reproducción para Dataset Scorer.
Verifica la clasificación (FACE/HALF/FULL) sobre una imagen específica.
"""
import sys
import os
import argparse

# Setup path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

from modules.dataset_scorer.logic.dataset_scorer import score_image

def test_image(path):
    if not os.path.exists(path):
        print(f"❌ Error: Archivo no encontrado: {path}")
        return

    print(f"\n🔍 Analizando: {os.path.basename(path)}")
    print("-" * 50)
    
    try:
        result = score_image(path)
        
        print(f"📊 Clasificación: {result.classification}")
        print(f"📈 Composite Score: {result.composite_score:.1f}")
        print("-" * 20)
        print(f"  • Face Score: {result.face_score:.1f} (Has Face: {result.has_face})")
        print(f"  • Upper Body: {result.upper_score:.1f}")
        print(f"  • Lower Body: {result.lower_score:.1f}")
        
        if result.error:
            print(f"\n⚠️ Error Interno:{result.error}")
            
    except Exception as e:
        print(f"❌ Excepción Crítica: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Dataset Scorer on single image")
    parser.add_argument("image_path", help="Path to image file")
    args = parser.parse_args()
    
    test_image(args.image_path)
