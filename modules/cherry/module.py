from PySide6.QtWidgets import QWidget

from core.base_module import BaseModule
from .ui.view import CherryView


class CherryModule(BaseModule):
    """
    Cherry-DL — lanzador del downloader masivo (Kemono/Patreon/Pixiv).

    Fase transicional del wrapper (CF2 de CHERRY_FUSION_DESIGN.md): el motor
    vive vendorizado en `third_party/cherry_dl/` SIN modificar y su GUI qasync
    corre como PROCESO APARTE sobre el venv de Panopticon. Este módulo solo
    lanza y supervisa ese proceso; la UI nativa integrada llega en fases
    posteriores (A2+). El estado (perfiles, index.db) vive en ~/.cherry-dl/,
    compartido con el cherry-dl autónomo.
    """

    def __init__(self):
        super().__init__()
        self._name = "Cherry-DL"
        self._description = "Mass downloader for artist collections (Kemono/Patreon/Pixiv)."
        self._icon = "🍒"
        self.accent_color = "#d2455b"  # rojo cereza (fallback; la vista usa el tema)
        self.view = None

    def get_view(self) -> QWidget:
        """Construcción perezosa de la interfaz (Regla de Oro #5)."""
        if self.view:
            return self.view
        self.view = CherryView(self.context)
        return self.view
