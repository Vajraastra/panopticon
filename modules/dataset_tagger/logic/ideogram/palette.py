"""
Extracción de paleta de color por k-means para el caption de Ideogram 4.

- Paleta GLOBAL: sobre la imagen completa (≤16 colores → style_description).
- Paleta por ELEMENTO: sobre el crop del objeto (≤5 colores).

NUNCA se usa para color de texto: k-means sobre un crop de texto devuelve el
fondo, no el glifo (el texto se anota con eyedropper / es ground truth manual).

Devuelve colores '#RRGGBB' MAYÚSCULAS, ordenados por frecuencia (dominante
primero). Usa MiniBatchKMeans (scikit-learn, ya en requirements).
"""
import numpy as np
from PIL import Image
from sklearn.cluster import MiniBatchKMeans

from . import schema

# Lado máximo al que se reduce la imagen antes de agrupar: la paleta no necesita
# resolución completa y así el k-means es rápido aun con imágenes grandes.
_SAMPLE_MAX_SIDE = 256


def _to_rgb_array(source):
    """Acepta una ruta, un PIL.Image o un ndarray y devuelve un ndarray RGB."""
    if isinstance(source, np.ndarray):
        arr = source
        if arr.ndim == 2:  # escala de grises -> RGB
            arr = np.stack([arr] * 3, axis=-1)
        return arr[:, :, :3]
    img = source if isinstance(source, Image.Image) else Image.open(source)
    img = img.convert("RGB")
    img.thumbnail((_SAMPLE_MAX_SIDE, _SAMPLE_MAX_SIDE))
    return np.asarray(img)


def extract_palette(source, k=5, max_colors=None):
    """Extrae hasta `k` colores dominantes como hex '#RRGGBB'.

    `source`: ruta, PIL.Image o ndarray RGB.
    `max_colors`: recorte final opcional (p.ej. schema.STYLE_PALETTE_MAX). Si se
    da, prevalece sobre `k` como tope.
    """
    arr = _to_rgb_array(source)
    pixels = arr.reshape(-1, 3).astype(np.float32)
    if len(pixels) == 0:
        return []

    n_clusters = max(1, min(k, len(np.unique(pixels, axis=0))))
    km = MiniBatchKMeans(n_clusters=n_clusters, random_state=0, n_init="auto")
    labels = km.fit_predict(pixels)

    # Ordena los centros por tamaño de cluster (color dominante primero).
    counts = np.bincount(labels, minlength=n_clusters)
    order = np.argsort(counts)[::-1]
    centers = km.cluster_centers_[order]

    palette = []
    for c in centers:
        hex_color = schema.normalize_hex(tuple(c))
        if hex_color and hex_color not in palette:
            palette.append(hex_color)

    limit = max_colors if max_colors is not None else k
    return palette[:limit]
