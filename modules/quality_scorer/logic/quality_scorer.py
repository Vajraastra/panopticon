"""
Quality Scorer — Fase 2: métricas de calidad técnica.

Evalúa nitidez, artefactos de compresión, resolución efectiva,
consistencia de color y composición. Retorna score 0–100 por imagen.
"""
import os
import cv2
import numpy as np


# ============================================================================
# PERFILES DE EVALUACIÓN
# ============================================================================

PROFILES = {
    "digital_art": {
        "name": "Digital Art / Anime",
        "weights": {
            "edge_sharpness":         0.35,
            "compression_artifacts":  0.25,
            "effective_resolution":   0.15,
            "color_consistency":      0.15,
            "composition":            0.10,
        }
    },
    "3d_render": {
        "name": "3D Renders",
        "weights": {
            "edge_sharpness":         0.25,
            "compression_artifacts":  0.25,
            "effective_resolution":   0.20,
            "color_consistency":      0.20,
            "composition":            0.10,
        }
    },
    "ai_generated": {
        "name": "AI Generated",
        "weights": {
            "edge_sharpness":         0.30,
            "compression_artifacts":  0.30,
            "effective_resolution":   0.15,
            "color_consistency":      0.15,
            "composition":            0.10,
        }
    },
    "photography": {
        "name": "Photography",
        "weights": {
            "edge_sharpness":         0.30,
            "compression_artifacts":  0.20,
            "effective_resolution":   0.20,
            "color_consistency":      0.15,
            "composition":            0.15,
        }
    },
}

DEFAULT_PROFILE = "digital_art"


# ============================================================================
# MÉTRICAS TÉCNICAS
# ============================================================================

def calculate_edge_sharpness(img) -> float:
    """Nitidez via varianza del Laplaciano + densidad de bordes Canny. 0–100."""
    if img is None or img.size == 0:
        return 0.0
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    lap_score  = min(cv2.Laplacian(gray, cv2.CV_64F).var() / 500.0, 1.0) * 100
    edges      = cv2.Canny(gray, 100, 200)
    edge_score = min(np.sum(edges > 0) / edges.size / 0.10, 1.0) * 100
    return lap_score * 0.7 + edge_score * 0.3


def calculate_compression_artifacts(img) -> float:
    """
    Detecta blocking y banding JPEG.
    Mayor score = menos artefactos = mejor calidad. 0–100.
    """
    if img is None or img.size == 0:
        return 0.0
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    h, w = gray.shape
    block = 8
    if h < block * 2 or w < block * 2:
        return 100.0

    h_diff = v_diff = count = 0
    for y in range(block, h - block, block):
        for x in range(block, w - block, block):
            h_diff += abs(float(gray[y, x]) - float(gray[y, x - 1]))
            v_diff += abs(float(gray[y, x]) - float(gray[y - 1, x]))
            count  += 1

    avg_diff      = (h_diff + v_diff) / (2 * max(count, 1))
    artifact_score = max(0, 100 - (avg_diff - 5) * 5)

    if len(img.shape) == 3:
        unique = len(np.unique(img.reshape(-1, 3), axis=0))
        total  = img.shape[0] * img.shape[1]
        banding = min(unique / min(total, 100000) * 200, 100)
        artifact_score = artifact_score * 0.7 + banding * 0.3

    return min(max(artifact_score, 0), 100)


def calculate_effective_resolution(img) -> float:
    """Detecta si hay detalle real o es un upscale artificial via FFT. 0–100."""
    if img is None or img.size == 0:
        return 0.0
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    f   = np.fft.fft2(gray.astype(np.float32))
    mag = np.log(np.abs(np.fft.fftshift(f)) + 1)
    h, w = mag.shape
    cy, cx = h // 2, w // 2
    r = min(h, w)
    y_g, x_g = np.ogrid[:h, :w]
    dist = np.sqrt((x_g - cx) ** 2 + (y_g - cy) ** 2)
    low  = mag[dist <= r // 8]
    high = mag[(dist > r // 8) & (dist <= r // 3)]
    lo_e = np.mean(low)  if low.size  > 0 else 1.0
    hi_e = np.mean(high) if high.size > 0 else 0.0
    return min(max(min(hi_e / max(lo_e, 1e-6) / 0.6, 1.0) * 100, 0), 100)


def calculate_color_consistency(img) -> float:
    """Evalúa suavidad de gradientes de color via entropía en LAB. 0–100."""
    if img is None or img.size == 0 or len(img.shape) < 3:
        return 50.0
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    L   = lab[:, :, 0].astype(np.float32)
    gx  = cv2.Sobel(L, cv2.CV_64F, 1, 0, ksize=3)
    gy  = cv2.Sobel(L, cv2.CV_64F, 0, 1, ksize=3)
    mag = np.sqrt(gx ** 2 + gy ** 2)
    hist, _ = np.histogram(mag.ravel(), bins=50, range=(0, 100))
    hn  = hist / max(np.sum(hist), 1)
    entropy = -np.sum(hn * np.log2(hn + 1e-10))
    return min(max(min(entropy / 4.0, 1.0) * 100, 0), 100)


def calculate_composition(img) -> float:
    """
    Penaliza bordes con alta densidad de contornos (sujeto cortado).
    Mayor score = composición más contenida. 0–100.
    """
    if img is None or img.size == 0:
        return 50.0
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    h, w = gray.shape
    b    = max(5, min(h, w) // 20)

    density = (
        np.sum(cv2.Canny(gray[:b,  :], 50, 150) > 0) +
        np.sum(cv2.Canny(gray[-b:, :], 50, 150) > 0) +
        np.sum(cv2.Canny(gray[:, :b],  50, 150) > 0) +
        np.sum(cv2.Canny(gray[:, -b:], 50, 150) > 0)
    ) / (4 * b * max(h, w))

    if   density < 0.03: return 100.0
    elif density < 0.08: return 80.0
    elif density < 0.15: return 60.0
    else:                return 40.0


# ============================================================================
# SCORING PRINCIPAL
# ============================================================================

def score_image(image_path: str, profile: str = DEFAULT_PROFILE) -> dict:
    """
    Puntúa una imagen con métricas técnicas.
    :return: dict con métricas individuales y composite_score (0–100).
    """
    result = {
        "path":                  image_path,
        "filename":              os.path.basename(image_path),
        "edge_sharpness":        0.0,
        "compression_artifacts": 0.0,
        "effective_resolution":  0.0,
        "color_consistency":     0.0,
        "composition":           0.0,
        "composite_score":       0,
        "profile":               profile,
    }
    if not os.path.exists(image_path):
        return result
    try:
        img = cv2.imread(image_path)
        if img is None:
            return result

        result["edge_sharpness"]        = round(calculate_edge_sharpness(img), 2)
        result["compression_artifacts"] = round(calculate_compression_artifacts(img), 2)
        result["effective_resolution"]  = round(calculate_effective_resolution(img), 2)
        result["color_consistency"]     = round(calculate_color_consistency(img), 2)
        result["composition"]           = round(calculate_composition(img), 2)

        weights   = PROFILES.get(profile, PROFILES[DEFAULT_PROFILE])["weights"]
        composite = sum(result[k] * weights[k] for k in weights)
        result["composite_score"] = int(max(0, min(100, composite)))

    except Exception as e:
        print(f"[QualityScorer] Error en {image_path}: {e}")

    return result
