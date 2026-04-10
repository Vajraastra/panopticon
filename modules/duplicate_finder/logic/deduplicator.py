import os
import hashlib
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

    def get_file_hash(self, path):
        """Calcula MD5 de un archivo para comparación exacta."""
        hasher = hashlib.md5()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b""):
                if self.stop_requested: return None
                hasher.update(chunk)
        return hasher.hexdigest()

    def get_visual_hash(self, path):
        """Calcula Perceptual Hash (pHash) para similitud visual."""
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
                    all_files.append(path)
                    size = os.path.getsize(path)
                    files_by_size.setdefault(size, []).append(path)

        # 2. Solo hashear archivos con tamaños idénticos
        total = len(all_files)
        processed = 0
        
        for size, paths in files_by_size.items():
            if self.stop_requested: break
            if len(paths) < 2:
                processed += len(paths)
                continue
            
            hashes = {}
            for path in paths:
                if self.stop_requested: break
                h = self.get_file_hash(path)
                if h:
                    hashes.setdefault(h, []).append(path)
                processed += 1
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
        
        # 1. Calcular todos los hashes (paralelizable)
        processed = 0
        for path in paths:
            if self.stop_requested: break
            h = self.get_visual_hash(path)
            if h:
                hashes[path] = imagehash.hex_to_hash(h)
            processed += 1
            if callback: callback(processed, total, f"Visual Hashing: {os.path.basename(path)}")

        # 2. Agrupar por proximidad
        visited = set()
        groups = []
        
        sorted_paths = list(hashes.keys())
        for i, p1 in enumerate(sorted_paths):
            if self.stop_requested: break
            if p1 in visited: continue
            
            current_group = [p1]
            visited.add(p1)
            h1 = hashes[p1]
            
            for p2 in sorted_paths[i+1:]:
                if p2 in visited: continue
                h2 = hashes[p2]
                if (h1 - h2) <= threshold:
                    current_group.append(p2)
                    visited.add(p2)
            
            if len(current_group) > 1:
                groups.append(current_group)
                
        # Convertir a formato dict compatible con la UI
        result = {f"group_{i}": g for i, g in enumerate(groups)}
        return result

    def stop(self):
        self.stop_requested = True
