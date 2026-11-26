from PySide6.QtWidgets import QWidget

from core.base_module import BaseModule
from .ui.view import PicklerView


class PicklerModule(BaseModule):
    """
    Pickler — curación manual con decisiones verificadas y recuperables.

    El panel permite elegir Mover, Copiar o Descartar y marcar con el botón principal.
    D marca para descartar en 'discarded/'. Mover y copiar usan 'picked/' o el destino elegido.
    Aplicar o cerrar confirma esas marcas en orden. Ctrl+Z desmarca o deshace
    la última decisión del conjunto y vuelve a esa imagen.

    Usa el servicio central para conservar identidad/curación; no importa otros módulos.
    """

    def __init__(self):
        super().__init__()
        self._name = "Pickler"
        self._description = "Mark images to move, copy, or discard, then apply the decisions together."
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

    def recovery_entries(self):
        # El botón propio de Pickler se queda: la herramienta se bloquea hasta resolverlo.
        return [("pickler_transfers",
                 self.tr("pickler.recovery", "Review interrupted transfers…").rstrip('…'),
                 self.tr("pickler.recovery_tip",
                    "Restore the source of a pending transfer, keeping its current curation. "
                    "Verified copy undo can resume; confirmed transfers are retained."),
                 lambda: self.view is not None and self.view._review_transfers())]

    def on_window_closed(self):
        if self.view:
            self.view.flush_library_changes()

    def prepare_close(self, retry):
        return self.view is None or self.view.prepare_close(retry)

    def on_unload(self):
        if self.view:self.view.shutdown()

    def shutdown_ready(self):
        return self.view is None or self.view.shutdown_ready()

    def load_image_set(self, paths: list):
        """Interfaz estándar: recibe un set de imágenes desde Gallery/Librarian."""
        self.get_view()
        self.view.load_images(paths)
