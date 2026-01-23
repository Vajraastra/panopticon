"""
Face Quality Scorer - Core Logic
Uses YOLOv8-face to score images based on face detection confidence.
"""
import os
import cv2
import numpy as np

# Lazy-load YOLO to avoid startup delay
_model = None

def get_model():
    """Lazy-loads the YOLO model. Prefers yolov8n-face.pt, falls back to yolov8n.pt."""
    global _model
    if _model is None:
        try:
            from ultralytics import YOLO
            
            # Check if custom face model exists
            face_model_path = 'yolov8n-face.pt'
            if os.path.exists(face_model_path):
                print(f"[FaceScorer] Loading custom face model: {face_model_path}")
                _model = YOLO(face_model_path)
            else:
                print("[FaceScorer] Custom 'yolov8n-face.pt' not found. Usage fallback: 'yolov8n.pt' (Person Detection).")
                _model = YOLO('yolov8n.pt') # Downloads automatically
                
        except ImportError:
            raise ImportError("ultralytics package not installed. Run: pip install ultralytics")
    return _model

def calculate_blur_score(image_region):
    """
    Calculates sharpness using Laplacian variance.
    Higher value = sharper image.
    Returns normalized 0-1 score.
    """
    if image_region is None or image_region.size == 0:
        return 0.0
    
    gray = cv2.cvtColor(image_region, cv2.COLOR_BGR2GRAY) if len(image_region.shape) == 3 else image_region
    laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    
    # Normalize: typical sharp images have variance > 500, blurry < 100
    # Map to 0-1 with sigmoid-like curve
    normalized = min(laplacian_var / 500.0, 1.0)
    return normalized

def score_image(image_path):
    """
    Scores an image based on face detection quality.
    
    Returns:
        dict: {
            "path": str,
            "faces_detected": int,
            "best_confidence": float,  # 0.0 - 1.0
            "best_face_ratio": float,  # Face area / image area
            "blur_score": float,       # 0.0 - 1.0 (1 = sharp)
            "composite_score": int,    # 0 - 100
            "has_face": bool
        }
    """
    result = {
        "path": image_path,
        "faces_detected": 0,
        "best_confidence": 0.0,
        "best_face_ratio": 0.0,
        "blur_score": 0.0,
        "composite_score": 0,
        "has_face": False
    }
    
    if not os.path.exists(image_path):
        return result
    
    try:
        # Load image
        img = cv2.imread(image_path)
        if img is None:
            return result
        
        img_h, img_w = img.shape[:2]
        img_area = img_h * img_w
        
        # Run detection
        model = get_model()
        results = model(img, verbose=False)
        
        if not results or len(results) == 0:
            return result
        
        boxes = results[0].boxes
        if boxes is None or len(boxes) == 0:
            return result
        
        # Find best face (highest confidence)
        best_conf = 0.0
        best_box = None
        
        for box in boxes:
            # Filter for class 0 (Person in COCO, Face in yolov8-face)
            if int(box.cls[0]) != 0:
                continue
                
            conf = float(box.conf[0])
            if conf > best_conf:
                best_conf = conf
                best_box = box.xyxy[0].cpu().numpy()
        
        if best_box is None:
            return result
        
        # Calculate face ratio
        x1, y1, x2, y2 = map(int, best_box)
        face_area = (x2 - x1) * (y2 - y1)
        face_ratio = min(face_area / img_area, 0.5)  # Cap at 50%
        
        # Calculate blur on face region
        face_region = img[max(0, y1):min(img_h, y2), max(0, x1):min(img_w, x2)]
        blur_score = calculate_blur_score(face_region)
        
        # Composite score (0-100)
        # Confidence: 70%, Face Size: 20%, Sharpness: 10%
        composite = int(
            (best_conf * 70) + 
            (face_ratio * 2 * 20) +  # face_ratio is 0-0.5, scale to 0-1
            (blur_score * 10)
        )
        composite = max(0, min(100, composite))
        
        result.update({
            "faces_detected": len(boxes),
            "best_confidence": round(best_conf, 3),
            "best_face_ratio": round(face_ratio, 3),
            "blur_score": round(blur_score, 3),
            "composite_score": composite,
            "has_face": True
        })
        
    except Exception as e:
        print(f"[FaceScorer] Error processing {image_path}: {e}")
    
    return result

def score_batch(image_paths, progress_callback=None):
    """
    Scores multiple images and returns sorted results.
    
    Args:
        image_paths: List of image file paths
        progress_callback: Optional callable(current, total, path)
    
    Returns:
        List of score dicts, sorted by composite_score descending
    """
    results = []
    total = len(image_paths)
    
    for i, path in enumerate(image_paths):
        result = score_image(path)
        results.append(result)
        
        if progress_callback:
            progress_callback(i + 1, total, path)
    
    # Sort by composite score (highest first)
    results.sort(key=lambda x: x["composite_score"], reverse=True)
    
    return results

    return accepted, rejected

def sort_files_by_score(results, threshold=50, base_folder=None, move_files=True):
    """
    Sorts files into percentage-based subfolders (100%, 90%, etc.) based on their scores.
    
    Args:
        results: List of score dicts
        threshold: Minimum score to process (below this, files are ignored/not moved)
        base_folder: Root folder to create subfolders in. If None, uses folder of first image.
        move_files: If True, moves files. If False, just simulates/returns stats.
        
    Returns:
        dict: {
            "moved_counts": { "90%": 5, ... },
            "errors": int,
            "total_moved": int
        }
    """
    import shutil
    
    stats = {
        "moved_counts": {},
        "errors": 0,
        "total_moved": 0
    }
    
    if not results:
        return stats
        
    if base_folder is None:
        if not results[0]["path"]:
            return stats
        base_folder = os.path.dirname(results[0]["path"])

    for result in results:
        score = result["composite_score"]
        path = result["path"]
        
        # Skip images below threshold
        if score < threshold:
            continue
        
        # Determine bucket (100, 90, 80, etc.)
        bucket = (score // 10) * 10
        if bucket > 100:
            bucket = 100
        bucket_name = f"{int(bucket)}%"
        
        if move_files:
            # Create folder if needed
            bucket_folder = os.path.join(base_folder, bucket_name)
            try:
                os.makedirs(bucket_folder, exist_ok=True)
                
                # Move file
                dest_path = os.path.join(bucket_folder, os.path.basename(path))
                if path != dest_path: # Avoid moving to self
                    shutil.move(path, dest_path)
            except Exception as e:
                print(f"[FaceScorer] Failed to move {path}: {e}")
                stats["errors"] += 1
                continue

        # Update stats
        stats["moved_counts"][bucket_name] = stats["moved_counts"].get(bucket_name, 0) + 1
        stats["total_moved"] += 1
        
    return stats
