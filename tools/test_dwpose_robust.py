import sys
import os
import cv2
import numpy as np
from PIL import Image

# Add root
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from modules.dataset_scorer.logic.dataset_scorer import score_image

def test_robust_pose():
    # USAR IMAGEN SUBIDA POR EL USUARIO
    test_path = r"C:\Users\nemph.PANOPTICON\.gemini\antigravity\brain\9ff0869c-d1a6-467f-a75a-9b10dc2eefa0\uploaded_media_1_1769985550648.jpg"
    
    if not os.path.exists(test_path):
        print(f"File not found: {test_path}")
        return

    print(f"🧪 Testing Robust 'Best-Fit' Logic on: {os.path.basename(test_path)}")
    
    try:
        # Probar scoring crudo (Métricas)
        res = score_image(test_path)
        
        # Simular balanceo global (Draft) para ver qué categoría ganaría
        # Usamos una lista con 1 solo elemento y cuotas estándar
        from modules.dataset_scorer.logic.dataset_scorer import balance_dataset
        from dataclasses import asdict
        
        results = [asdict(res)]
        targets = {'FACE': 100, 'HALF_BODY': 100, 'FULL_BODY': 100} # Cuotas abiertas para test
        balanced = balance_dataset(results, targets)
        final_res = balanced[0]
        
        print(f"\n✅ ANALYSIS COMPLETE!")
        print(f"   Category (via Balancing): {final_res['classification']}")
        print(f"   Composite Score:          {final_res['composite_score']:.2f}")
        print(f"   Detailed Metrics (Raw):")
        print(f"     - Face Quality:  {res.face_score:.2f}")
        print(f"     - Upper utility: {res.upper_score:.2f}")
        print(f"     - Lower utility: {res.lower_score:.2f}")
        print(f"     - Ankle focus:   {res.ankle_score:.2f}")
        
    except Exception as e:
        print(f"❌ CRASH: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_robust_pose()
