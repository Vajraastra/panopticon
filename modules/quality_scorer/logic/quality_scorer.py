"""
Lógica Central del Quality Scorer v2.
Evalúa calidad técnica de imágenes para curación de datasets,
optimizado para arte digital, renders 3D e imágenes generadas por IA.

Flujo v2:
1. Scan inicial → score primario
2. Predicción de mejora potencial
3. Aplicar mejoras (si usuario acepta)
4. Re-scan → scores finales
5. Catalogar en folders (100%-60% + below_threshold/)
"""
import os
import cv2
import numpy as np
import shutil

# ============================================================================
# PERFILES DE EVALUACIÓN
# ============================================================================

PROFILES = {
    "digital_art": {
        "name": "Digital Art / Anime",
        "weights": {
            "edge_sharpness": 0.35,
            "compression_artifacts": 0.25,
            "effective_resolution": 0.15,
            "color_consistency": 0.15,
            "composition": 0.10
        }
    },
    "3d_render": {
        "name": "3D Renders",
        "weights": {
            "edge_sharpness": 0.25,
            "compression_artifacts": 0.25,
            "effective_resolution": 0.20,
            "color_consistency": 0.20,
            "composition": 0.10
        }
    },
    "ai_generated": {
        "name": "AI Generated",
        "weights": {
            "edge_sharpness": 0.30,
            "compression_artifacts": 0.30,
            "effective_resolution": 0.15,
            "color_consistency": 0.15,
            "composition": 0.10
        }
    },
    "photography": {
        "name": "Photography",
        "weights": {
            "edge_sharpness": 0.30,
            "compression_artifacts": 0.20,
            "effective_resolution": 0.20,
            "color_consistency": 0.15,
            "composition": 0.15
        }
    }
}

DEFAULT_PROFILE = "digital_art"
MIN_THRESHOLD = 60  # Minimum score for organized folders


# ============================================================================
# MÉTRICAS DE CALIDAD
# ============================================================================

def calculate_edge_sharpness(img):
    """
    Mide la nitidez de bordes usando varianza del Laplaciano + detección Canny.
    Retorna score 0-100.
    """
    if img is None or img.size == 0:
        return 0.0
    
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    
    # Laplacian variance (blur detection)
    laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    
    # Canny edge density
    edges = cv2.Canny(gray, 100, 200)
    edge_density = np.sum(edges > 0) / edges.size
    
    # Combine metrics
    lap_score = min(laplacian_var / 500.0, 1.0) * 100
    edge_score = min(edge_density / 0.10, 1.0) * 100
    
    return (lap_score * 0.7 + edge_score * 0.3)


def calculate_compression_artifacts(img):
    """
    Detecta artefactos de compresión JPEG (blocking, banding).
    Retorna score 0-100 (mayor = menos artefactos = mejor).
    """
    if img is None or img.size == 0:
        return 0.0
    
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    
    h, w = gray.shape
    block_size = 8
    
    if h < block_size * 2 or w < block_size * 2:
        return 100.0
    
    h_diff = 0
    v_diff = 0
    count = 0
    
    for y in range(block_size, h - block_size, block_size):
        for x in range(block_size, w - block_size, block_size):
            h_boundary = abs(float(gray[y, x]) - float(gray[y, x-1]))
            v_boundary = abs(float(gray[y, x]) - float(gray[y-1, x]))
            h_diff += h_boundary
            v_diff += v_boundary
            count += 1
    
    if count == 0:
        return 100.0
    
    avg_diff = (h_diff + v_diff) / (2 * count)
    artifact_score = max(0, 100 - (avg_diff - 5) * 5)
    
    if len(img.shape) == 3:
        unique_colors = len(np.unique(img.reshape(-1, 3), axis=0))
        total_pixels = img.shape[0] * img.shape[1]
        color_ratio = unique_colors / min(total_pixels, 100000)
        banding_score = min(color_ratio * 200, 100)
        artifact_score = (artifact_score * 0.7 + banding_score * 0.3)
    
    return min(max(artifact_score, 0), 100)


def calculate_effective_resolution(img):
    """
    Detecta si la imagen tiene detalle real o es un upscale artificial.
    Retorna score 0-100.
    """
    if img is None or img.size == 0:
        return 0.0
    
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    
    f = np.fft.fft2(gray.astype(np.float32))
    fshift = np.fft.fftshift(f)
    magnitude = np.log(np.abs(fshift) + 1)
    
    h, w = magnitude.shape
    center_y, center_x = h // 2, w // 2
    
    radius_low = min(h, w) // 8
    radius_high = min(h, w) // 3
    
    y, x = np.ogrid[:h, :w]
    dist = np.sqrt((x - center_x)**2 + (y - center_y)**2)
    
    low_mask = dist <= radius_low
    high_mask = (dist > radius_low) & (dist <= radius_high)
    
    low_energy = np.mean(magnitude[low_mask]) if np.sum(low_mask) > 0 else 1
    high_energy = np.mean(magnitude[high_mask]) if np.sum(high_mask) > 0 else 0
    
    if low_energy > 0:
        ratio = high_energy / low_energy
    else:
        ratio = 0
    
    score = min(ratio / 0.6, 1.0) * 100
    return min(max(score, 0), 100)


def calculate_color_consistency(img):
    """
    Evalúa consistencia de color y suavidad de gradientes.
    Retorna score 0-100.
    """
    if img is None or img.size == 0 or len(img.shape) < 3:
        return 50.0
    
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l_channel = lab[:, :, 0].astype(np.float32)
    
    grad_x = cv2.Sobel(l_channel, cv2.CV_64F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(l_channel, cv2.CV_64F, 0, 1, ksize=3)
    grad_magnitude = np.sqrt(grad_x**2 + grad_y**2)
    
    hist, _ = np.histogram(grad_magnitude.ravel(), bins=50, range=(0, 100))
    hist_normalized = hist / np.sum(hist) if np.sum(hist) > 0 else hist
    
    entropy = -np.sum(hist_normalized * np.log2(hist_normalized + 1e-10))
    score = min(entropy / 4.0, 1.0) * 100
    
    return min(max(score, 0), 100)


def calculate_composition(img):
    """
    Evalúa si la composición está centrada y completa.
    Retorna score 0-100.
    """
    if img is None or img.size == 0:
        return 50.0
    
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    h, w = gray.shape
    
    border_size = max(5, min(h, w) // 20)
    
    top_border = gray[:border_size, :]
    bottom_border = gray[-border_size:, :]
    left_border = gray[:, :border_size]
    right_border = gray[:, -border_size:]
    
    edges_top = cv2.Canny(top_border, 50, 150)
    edges_bottom = cv2.Canny(bottom_border, 50, 150)
    edges_left = cv2.Canny(left_border, 50, 150)
    edges_right = cv2.Canny(right_border, 50, 150)
    
    border_edge_density = (
        np.sum(edges_top > 0) + np.sum(edges_bottom > 0) +
        np.sum(edges_left > 0) + np.sum(edges_right > 0)
    ) / (4 * border_size * max(h, w))
    
    if border_edge_density < 0.03:
        score = 100
    elif border_edge_density < 0.08:
        score = 80
    elif border_edge_density < 0.15:
        score = 60
    else:
        score = 40
    
    return score


# ============================================================================
# SCORING PRINCIPAL
# ============================================================================

def score_image(image_path, profile=DEFAULT_PROFILE):
    """
    Puntúa una imagen individual.
    :return: dict con estadísticas y composite_score (0-100).
    """
    result = {
        "path": image_path,
        "filename": os.path.basename(image_path),
        "edge_sharpness": 0.0,
        "compression_artifacts": 0.0,
        "effective_resolution": 0.0,
        "color_consistency": 0.0,
        "composition": 0.0,
        "composite_score": 0,
        "profile": profile,
        "can_improve": False,
        "predicted_improvement": 0
    }
    
    if not os.path.exists(image_path):
        return result
    
    try:
        img = cv2.imread(image_path)
        if img is None:
            return result
        
        result["edge_sharpness"] = round(calculate_edge_sharpness(img), 2)
        result["compression_artifacts"] = round(calculate_compression_artifacts(img), 2)
        result["effective_resolution"] = round(calculate_effective_resolution(img), 2)
        result["color_consistency"] = round(calculate_color_consistency(img), 2)
        result["composition"] = round(calculate_composition(img), 2)
        
        weights = PROFILES.get(profile, PROFILES[DEFAULT_PROFILE])["weights"]
        
        composite = (
            result["edge_sharpness"] * weights["edge_sharpness"] +
            result["compression_artifacts"] * weights["compression_artifacts"] +
            result["effective_resolution"] * weights["effective_resolution"] +
            result["color_consistency"] * weights["color_consistency"] +
            result["composition"] * weights["composition"]
        )
        
        result["composite_score"] = int(max(0, min(100, composite)))
        
        # Predict improvement potential
        improvement = predict_improvement(result)
        result["can_improve"] = improvement > 3  # At least 3 points improvement
        result["predicted_improvement"] = improvement
        
    except Exception as e:
        print(f"[QualityScorer] Error processing {image_path}: {e}")
    
    return result


def predict_improvement(result):
    """
    Predice cuántos puntos podría mejorar el score con correcciones.
    Basado en las métricas individuales.
    """
    improvement = 0
    
    # Sharpening can help if edge_sharpness is < 70
    if result["edge_sharpness"] < 70:
        improvement += min((70 - result["edge_sharpness"]) * 0.15, 5)
    
    # Denoise can help if color_consistency is < 60
    if result["color_consistency"] < 60:
        improvement += min((60 - result["color_consistency"]) * 0.10, 3)
    
    # Contrast boost can help with low effective_resolution perception
    if result["effective_resolution"] < 65:
        improvement += min((65 - result["effective_resolution"]) * 0.08, 3)
    
    # Cannot fix composition or heavy compression artifacts
    # So those don't contribute to improvement prediction
    
    return round(improvement, 1)


def score_batch(image_paths, profile=DEFAULT_PROFILE, progress_callback=None):
    """
    Evalúa múltiples imágenes y retorna resultados ordenados.
    """
    results = []
    total = len(image_paths)
    
    for i, path in enumerate(image_paths):
        result = score_image(path, profile)
        results.append(result)
        
        if progress_callback:
            progress_callback(i + 1, total, path)
    
    results.sort(key=lambda x: x["composite_score"], reverse=True)
    return results


# ============================================================================
# FUNCIONES DE MEJORA/CORRECCIÓN
# ============================================================================

def apply_sharpening(img, strength=0.5):
    """Aplica Unsharp Mask para mejorar nitidez."""
    if img is None:
        return img
    gaussian = cv2.GaussianBlur(img, (0, 0), 3)
    sharpened = cv2.addWeighted(img, 1.0 + strength, gaussian, -strength, 0)
    return sharpened


def apply_color_normalization(img):
    """Normaliza colores usando CLAHE en canal L."""
    if img is None or len(img.shape) < 3:
        return img
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def apply_denoise(img, strength=7):
    """Reduce ruido sutil sin perder detalle."""
    if img is None:
        return img
    if len(img.shape) == 3:
        return cv2.fastNlMeansDenoisingColored(img, None, strength, strength, 7, 21)
    else:
        return cv2.fastNlMeansDenoising(img, None, strength, 7, 21)


def apply_contrast_boost(img):
    """Mejora contraste local usando CLAHE."""
    if img is None:
        return img
    if len(img.shape) == 3:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        hsv[:, :, 2] = clahe.apply(hsv[:, :, 2])
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    else:
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        return clahe.apply(img)


def enhance_image(image_path, output_path):
    """
    Aplica correcciones automáticas a una imagen.
    :return: output_path si éxito, None si falla.
    """
    try:
        img = cv2.imread(image_path)
        if img is None:
            return None
        
        # Apply corrections in logical order
        img = apply_denoise(img, strength=5)
        img = apply_color_normalization(img)
        img = apply_contrast_boost(img)
        img = apply_sharpening(img, strength=0.4)
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        cv2.imwrite(output_path, img)
        return output_path
        
    except Exception as e:
        print(f"[QualityScorer] Enhancement failed for {image_path}: {e}")
        return None


# ============================================================================
# FLUJO PRINCIPAL v2
# ============================================================================

def run_full_workflow(image_paths, base_folder, profile=DEFAULT_PROFILE, 
                      apply_enhancements=False, progress_callback=None):
    """
    Ejecuta el flujo completo:
    1. Scan inicial
    2. (Opcional) Aplicar mejoras
    3. Re-scan si se aplicaron mejoras
    4. Catalogar en folders
    
    :param image_paths: Lista de rutas de imágenes
    :param base_folder: Carpeta base donde crear 'score/' subfolder
    :param profile: Perfil de evaluación
    :param apply_enhancements: Si True, aplica mejoras automáticas
    :param progress_callback: Callback(step, current, total, message)
    :return: dict con estadísticas completas
    """
    stats = {
        "total_images": len(image_paths),
        "initial_scores": [],
        "final_scores": [],
        "enhanced_count": 0,
        "improvement_achieved": 0,
        "categories": {},
        "below_threshold": 0,
        "output_folder": os.path.join(base_folder, "score"),
        "log": []
    }
    
    if not image_paths:
        stats["log"].append("No images to process.")
        return stats
    
    score_folder = stats["output_folder"]
    os.makedirs(score_folder, exist_ok=True)
    
    total_steps = len(image_paths) * (3 if apply_enhancements else 2)  # scan + (enhance + rescan) + catalog
    current_step = 0
    
    def update_progress(step_name, curr, total, path=""):
        nonlocal current_step
        current_step += 1
        if progress_callback:
            progress_callback(step_name, curr, total, os.path.basename(path) if path else "")
    
    # ==================== STEP 1: Initial Scan ====================
    stats["log"].append(f"=== STEP 1: Initial Scan ({len(image_paths)} images) ===")
    
    initial_results = []
    for i, path in enumerate(image_paths):
        result = score_image(path, profile)
        initial_results.append(result)
        update_progress("Scanning", i + 1, len(image_paths), path)
    
    stats["initial_scores"] = initial_results
    avg_initial = sum(r["composite_score"] for r in initial_results) / len(initial_results)
    stats["log"].append(f"Average initial score: {avg_initial:.1f}")
    
    improvable = [r for r in initial_results if r["can_improve"]]
    stats["log"].append(f"Images that could improve: {len(improvable)}/{len(initial_results)}")
    
    # Determine which images to process
    final_results = initial_results.copy()
    working_paths = {}  # Maps original path -> working copy path
    
    # ==================== STEP 2-3: Enhance (if requested) ====================
    if apply_enhancements and improvable:
        stats["log"].append(f"\n=== STEP 2-3: Enhancing {len(improvable)} images ===")
        
        for i, result in enumerate(improvable):
            orig_path = result["path"]
            filename = os.path.basename(orig_path)
            
            # Create temporary enhanced copy
            temp_path = os.path.join(score_folder, "_temp", filename)
            enhanced_path = enhance_image(orig_path, temp_path)
            
            if enhanced_path:
                working_paths[orig_path] = enhanced_path
                stats["enhanced_count"] += 1
            
            update_progress("Enhancing", i + 1, len(improvable), orig_path)
        
        stats["log"].append(f"Successfully enhanced: {stats['enhanced_count']} images")
        
        # Re-scan enhanced images
        stats["log"].append(f"\n=== STEP 4: Re-scanning enhanced images ===")
        
        for i, result in enumerate(final_results):
            orig_path = result["path"]
            if orig_path in working_paths:
                # Re-score the enhanced version
                new_result = score_image(working_paths[orig_path], profile)
                new_result["path"] = orig_path  # Keep original path for reference
                new_result["enhanced_path"] = working_paths[orig_path]
                new_result["original_score"] = result["composite_score"]
                new_result["improvement"] = new_result["composite_score"] - result["composite_score"]
                final_results[i] = new_result
                stats["improvement_achieved"] += new_result["improvement"]
            
            update_progress("Re-scanning", i + 1, len(final_results), orig_path)
        
        avg_final = sum(r["composite_score"] for r in final_results) / len(final_results)
        stats["log"].append(f"Average final score: {avg_final:.1f} (improvement: +{avg_final - avg_initial:.1f})")
    
    stats["final_scores"] = final_results
    
    # ==================== STEP 5: Catalog into folders ====================
    stats["log"].append(f"\n=== STEP 5: Cataloging images ===")
    
    # Create percentage folders (100%, 90%, 80%, 70%, 60%) and below_60%
    for bucket in ["100%", "90%", "80%", "70%", "60%", "below_60%"]:
        bucket_folder = os.path.join(score_folder, bucket)
        os.makedirs(bucket_folder, exist_ok=True)
        stats["categories"][bucket] = []
    
    for i, result in enumerate(final_results):
        score = result["composite_score"]
        filename = os.path.basename(result["path"])
        
        # Determine bucket
        if score >= 100:
            bucket = "100%"
        elif score >= 90:
            bucket = "90%"
        elif score >= 80:
            bucket = "80%"
        elif score >= 70:
            bucket = "70%"
        elif score >= 60:
            bucket = "60%"
        else:
            bucket = "below_60%"
            stats["below_threshold"] += 1
        
        # Copy file (from enhanced or original)
        if "enhanced_path" in result and os.path.exists(result["enhanced_path"]):
            src_path = result["enhanced_path"]
        else:
            src_path = result["path"]
        
        dest_path = os.path.join(score_folder, bucket, filename)
        
        try:
            shutil.copy2(src_path, dest_path)
            result["final_path"] = dest_path
            result["bucket"] = bucket
            stats["categories"][bucket].append(result)
        except Exception as e:
            stats["log"].append(f"Failed to copy {filename}: {e}")
        
        update_progress("Cataloging", i + 1, len(final_results), result["path"])
    
    # Clean up temp folder
    temp_folder = os.path.join(score_folder, "_temp")
    if os.path.exists(temp_folder):
        try:
            shutil.rmtree(temp_folder)
        except:
            pass
    
    # Generate summary
    stats["log"].append(f"\n=== SUMMARY ===")
    stats["log"].append(f"Total processed: {stats['total_images']}")
    stats["log"].append(f"Enhanced: {stats['enhanced_count']}")
    stats["log"].append(f"Average improvement: +{stats['improvement_achieved'] / max(1, stats['enhanced_count']):.1f} points")
    
    for bucket in ["100%", "90%", "80%", "70%", "60%", "below_60%"]:
        count = len(stats["categories"][bucket])
        if count > 0:
            stats["log"].append(f"  {bucket}: {count} images")
    
    stats["log"].append(f"\nOutput folder: {score_folder}")
    
    return stats


def get_improvable_count(results):
    """Returns count of images that could potentially improve."""
    return sum(1 for r in results if r.get("can_improve", False))


def get_predicted_improvement_summary(results):
    """Returns summary of predicted improvements."""
    improvable = [r for r in results if r.get("can_improve", False)]
    if not improvable:
        return None
    
    total_improvement = sum(r.get("predicted_improvement", 0) for r in improvable)
    avg_improvement = total_improvement / len(improvable)
    
    return {
        "count": len(improvable),
        "avg_improvement": round(avg_improvement, 1),
        "total_improvement": round(total_improvement, 1)
    }
