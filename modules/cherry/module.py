from PySide6.QtWidgets import QWidget

from core.base_module import BaseModule
from .logic.runtime import CherryRuntime
from .ui.view import CherryView


class CherryModule(BaseModule):
    """
    Cherry-DL — descargador masivo de colecciones (Kemono/Patreon/Pixiv).

    Módulo nativo sobre el motor vendorizado en `third_party/cherry_dl/`. La vista
    de perfiles lee `index.db` a través de `CherryRuntime`, el hilo que hospeda
    el event loop asyncio del que depende todo el motor. Altas, ajustes, descarga,
    mantenimiento y fusión se realizan desde Panopticon.

    El runtime es del módulo, no de la vista: así hay un solo loop por sesión y
    un dueño claro que lo apaga en `on_unload`.
    """

    def __init__(self):
        super().__init__()
        self._name = "Cherry-DL"
        self._description = "Mass downloader for artist collections (Kemono/Patreon/Pixiv)."
        self._icon = "🍒"
        self.accent_color = "#d2455b"  # rojo cereza (fallback; la vista usa el tema)
        self.view = None
        self.runtime = CherryRuntime()

    def get_view(self) -> QWidget:
        """Construcción perezosa de la interfaz (Regla de Oro #5).

        Crear la vista NO arranca el runtime: eso pasa en su primer `showEvent`,
        cuando el dashboard abre por primera vez su ventana independiente.
        """
        if self.view:
            return self.view
        self.view = CherryView(self.context, runtime=self.runtime)
        return self.view

    def on_unload(self):
        """Cancela el trabajo visible antes de parar su loop propietario."""
        if self.view is not None:
            self.view.shutdown()
        else:
            self.runtime.stop()
