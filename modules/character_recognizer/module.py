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
        self._closed = False
    
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

    def on_unload(self):
        self._closed = True
        if self.view:
            self.content_view.shutdown()

    def shutdown_ready(self):
        return not self.view or self.content_view.shutdown_ready()
    
    def get_default_input_folder(self):
        return CachePaths.get_cache_root()
    
    def load_image_set(self, paths: list):
        """Interfaz estándar para recibir datos masivos."""
        if paths and not self._closed:
            self.get_view().load_images(paths)

    def run_headless(self, paths: list, *, mode='ccip'):
        """Ejecuta el reconocimiento sin interfaz (para pipelines)."""
        from core.ai.ccip import CCIPEncoder
        from .logic.image_analysis import analyze_file, match_embedding
        from .logic.recognition_engine import RecognitionEngine
        from .logic.profile_db import ProfileDB
        engine = CCIPEncoder() if mode == 'ccip' else RecognitionEngine()
        if mode != 'ccip':
            engine.initialize()
        db = ProfileDB(mode='ccip') if mode == 'ccip' else ProfileDB()
        profiles = db.get_all_profiles()  # Un snapshot coherente para todo el lote headless.
        results = {}
        for path in paths:
            _, embedding, _, _ = analyze_file(path, engine, mode)
            if embedding is None:
                continue
            results[path], _ = match_embedding(engine, embedding, profiles, mode)
        return results
