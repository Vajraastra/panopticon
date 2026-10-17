from PySide6.QtWidgets import QWidget

from core.base_module import BaseModule
from .ui.view import PicklerView


class PicklerModule(BaseModule):
    """
    Pickler — curación manual rápida con decisiones verificadas y recuperables:

      • Modo Borrar : mueve la imagen a 'discarded/' dentro de la carpeta del
                      set (recuperable). Nunca borra el original directo.
      • Modo Picker : mueve o copia las imágenes que conservas a 'picked/'
                      dentro del set (o a un destino que elijas; toggle
                      Mover/Copiar en el sidebar).

    D descarta y C conserva según Mover/Copiar, sin depender del modo del botón.
    Ambas avanzan al confirmar la operación; se ignoran las repeticiones y las
    pulsaciones durante carga/transferencia. Ctrl+Z deshace las decisiones del
    conjunto en orden inverso y vuelve a la imagen restaurada.

    Usa el servicio central para conservar identidad/curación; no importa otros módulos.
    """

    def __init__(self):
        super().__init__()
        self._name = "Pickler"
        self._description = "Manually cull and keep images with two error-proof modes."
        self._icon = "🥒"
        # Verde pepinillo como identidad (fallback; la vista usa la paleta del tema).
        self.accent_color = "#8ab832"
        self.view = None

    def get_view(self) -> QWidget:
        """Construcción perezosa de la interfaz (Regla de Oro #5)."""
        if self.view:
            return self.view
        self.view = PicklerView(self.context)
        return self.view

    def on_unload(self):
        if self.view:self.view.shutdown()

    def shutdown_ready(self):
        return self.view is None or self.view.shutdown_ready()

    def load_image_set(self, paths: list):
        """Interfaz estándar: recibe un set de imágenes desde Gallery/Librarian."""
        self.get_view()
        self.view.load_images(paths)
