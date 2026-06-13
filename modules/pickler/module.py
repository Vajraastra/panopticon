from PySide6.QtWidgets import QWidget

from core.base_module import BaseModule
from .ui.view import PicklerView


class PicklerModule(BaseModule):
    """
    Pickler — curación manual rápida de imágenes con dos modos *error-proof*:

      • Modo Borrar : mueve la imagen a 'discarded/' dentro de la carpeta del
                      set (recuperable). Nunca borra el original directo.
      • Modo Picker : mueve o copia las imágenes que conservas a 'picked/'
                      dentro del set (o a un destino que elijas; toggle
                      Mover/Copiar en el sidebar).

    La acción primaria de cada modo se dispara SOLO por clic derecho (menú
    contextual) o botón explícito; el teclado solo omite/retrocede. Esa fricción
    deliberada evita borrados accidentales al curar miles de imágenes en
    "piloto automático".

    No usa base de datos ni importa otros módulos (Regla de Oro #3).
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

    def load_image_set(self, paths: list):
        """Interfaz estándar: recibe un set de imágenes desde Gallery/Librarian."""
        self.get_view()
        self.view.load_images(paths)
