import os
import hashlib
import numpy as np
from PIL import Image
import imagehash
from concurrent.futures import ThreadPoolExecutor

class Deduplicator:
    """
    Motor de búsqueda de duplicados.
    Soporta comparación exacta (Hash) y similitud visual (pHash).
    """

    ARCHIVE_EXTENSIONS = ('.zip', '.rar')
    IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.avif')

    def __init__(self):
        self.stop_requested = False

    def _pool(self):
        return ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4))

    def get_file_hash(self, path):
        """Calcula MD5 de un archivo para comparación exacta."""
        hasher = hashlib.md5()
        try:
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    if self.stop_requested: return None
                    hasher.update(chunk)
        except OSError:
            return None
        return hasher.hexdigest()

    def get_visual_hash(self, path):
        """Calcula Perceptual Hash (pHash) para similitud visual."""
        if self.stop_requested:
            return None
        try:
            with Image.open(path) as img:
                return str(imagehash.phash(img))
        except Exception as e:
            print(f"Error hashing visual {path}: {e}")
            return None

    def find_duplicates_by_hash(self, folder_path, callback=None):
        """Busca duplicados exactos comparando tamaño y luego MD5."""
        files_by_size = {}
        duplicates = {}

        # 1. Agrupar por tamaño (filtro rápido)
        all_files = []
        for root, _, files in os.walk(folder_path):
            for f in files:
                if f.lower().endswith(self.IMAGE_EXTENSIONS + self.ARCHIVE_EXTENSIONS):
                    path = os.path.join(root, f).replace('\\', '/')
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        # Archivo borrado a media corrida o symlink roto
                        continue
                    all_files.append(path)
                    files_by_size.setdefault(size, []).append(path)

        # 2. Solo hashear archivos con tamaños idénticos, MD5 en paralelo
        total = len(all_files)
        candidates = [p for paths in files_by_size.values() if len(paths) > 1
                      for p in paths]
        processed = total - len(candidates)  # únicos por tamaño: ya descartados

        hashes = {}
        with self._pool() as pool:
            for path, h in zip(candidates, pool.map(self.get_file_hash, candidates)):
                if self.stop_requested: break
                processed += 1
                if h:
                    hashes.setdefault(h, []).append(path)
                if callback: callback(processed, total, f"Hashing: {os.path.basename(path)}")

        for h, h_paths in hashes.items():
            if len(h_paths) > 1:
                duplicates[h] = h_paths

        return duplicates

    def find_duplicates_visual(self, folder_path, threshold=5, callback=None):
        """Busca duplicados por similitud visual usando pHash."""
        paths = []
        for root, _, files in os.walk(folder_path):
            for f in files:
                if f.lower().endswith(self.IMAGE_EXTENSIONS):
                    paths.append(os.path.join(root, f).replace('\\', '/'))

        total = len(paths)
        hashes = {} # path -> hash_obj

        # 1. Calcular todos los hashes en paralelo (decode PIL libera el GIL)
        processed = 0
        with self._pool() as pool:
            for path, h in zip(paths, pool.map(self.get_visual_hash, paths)):
                if self.stop_requested: break
                processed += 1
                if h:
                    hashes[path] = imagehash.hex_to_hash(h)
                if callback: callback(processed, total, f"Visual Hashing: {os.path.basename(path)}")

        # 2. Agrupar por proximidad — hamming vectorizado con numpy
        # (sigue siendo O(n²) pero en C: una pasada numpy por imagen no visitada)
        sorted_paths = list(hashes.keys())
        groups = []
        if sorted_paths:
            bits = np.array([h.hash.flatten() for h in hashes.values()], dtype=bool)
            visited = np.zeros(len(sorted_paths), dtype=bool)

            for i in range(len(sorted_paths)):
                if self.stop_requested: break
                if visited[i]: continue
                visited[i] = True

                # distancia hamming de i contra todos los siguientes
                dist = np.count_nonzero(bits[i + 1:] != bits[i], axis=1)
                near = np.nonzero(dist <= threshold)[0] + i + 1
                members = [j for j in near if not visited[j]]

                if members:
                    visited[members] = True
                    groups.append([sorted_paths[i]] + [sorted_paths[j] for j in members])

        # Convertir a formato dict compatible con la UI
        result = {f"group_{i}": g for i, g in enumerate(groups)}
        return result

    def stop(self):
        self.stop_requested = True
