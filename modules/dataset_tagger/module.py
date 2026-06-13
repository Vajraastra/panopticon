from PySide6.QtWidgets import QWidget
from core.base_module import BaseModule
from core.paths import CachePaths
from .ui.tagger_view import DatasetTaggerView


class DatasetTaggerModule(BaseModule):
    """
    Dataset Tagger — genera captions (sidecars .txt estilo kohya) para una
    carpeta de imágenes usando un VLM (modelo de visión) local u online.
    Soporta tags (booru) y lenguaje natural, con plantillas por modelo de
    generación. La salida SIEMPRE va a una carpeta nueva; nunca escribe dentro
    del archivo original (ver Regla de Oro #9).
    """

    def __init__(self):
        super().__init__()
        self._name = "Dataset Tagger"
        self._description = "Caption image folders with a vision LLM for dataset training."
        self._icon = "🏷️"
        # Fallback; la vista sincroniza el acento con la paleta global (theme).
        self.accent_color = "#bd93f9"
        self.view = None

    def get_view(self) -> QWidget:
        """Construcción perezosa de la interfaz."""
        if self.view:
            return self.view
        self.view = DatasetTaggerView(self.context)
        return self.view

    def get_output_folder(self):
        """Subcarpeta de cache para este módulo."""
        return CachePaths.get_tool_cache("dataset_tagger")

    def load_image_set(self, paths: list):
        """Interfaz estándar para recibir imágenes desde otros módulos (Fase 6)."""
        if self.view:
            self.view.load_images(paths)
