from PySide6.QtWidgets import QWidget, QLabel
from core.base_module import BaseModule
from core.paths import CachePaths
from .ui.recognition_view import CharacterRecognitionView

class CharacterRecognizerModule(BaseModule):
    def __init__(self):
        super().__init__()
        # 1. Metadatos obligatorios para el Dashboard
        self._name = "Character Recognizer"
        self._description = "Identify and tag characters using AI."
        self._icon = "👤"
        self.accent_color = "#FF9800"  # Orange
        self.view = None
    
    def get_view(self) -> QWidget:
        """Punto de entrada visual."""
        if self.view: return self.view
        
        # 2. Construcción perezosa de la interfaz
        # El contenido principal será nuestra vista personalizada
        # La vista ahora encapsula su propio StandardToolLayout
        self.content_view = CharacterRecognitionView(self.context)
        self.view = self.content_view
        
        return self.view

    def get_output_folder(self):
        """Retorna la subcarpeta de cache para este módulo."""
        return CachePaths.get_tool_cache("character_recognizer")
    
    def get_default_input_folder(self):
        return CachePaths.get_cache_root()
    
    def load_image_set(self, paths: list):
        """Interfaz estándar para recibir datos masivos."""
        if self.view:
            self.content_view.load_images(paths)

    def run_headless(self, paths: list):
        """Ejecuta el reconocimiento sin interfaz (para pipelines)."""
        from .logic.recognition_engine import RecognitionEngine
        from .logic.profile_db import ProfileDB
        engine = RecognitionEngine()
        engine.initialize()
        db = ProfileDB()
        results = {}
        for path in paths:
            import cv2
            import numpy as np
            with open(path, "rb") as f:
                arr = np.asarray(bytearray(f.read()), dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                continue
            embedding, _, _ = engine.analyze_image(img)
            if embedding is None:
                continue
            best_name, best_score = None, 0.0
            for name, ref_emb in db.get_all_profiles():
                score = engine.compare(embedding, ref_emb)
                if score > best_score:
                    best_score = score
                    best_name = name if score > 0.6 else None
            results[path] = best_name
        return results
