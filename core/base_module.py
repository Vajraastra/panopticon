from PySide6.QtWidgets import QWidget
from PySide6.QtCore import QObject

class BaseModule(QObject):
    """
    Clase base abstracta para todos los módulos de Panopticon.
    Define la interfaz estándar que permite al Core cargar, inicializar
    y comunicar módulos entre sí.
    """
    def __init__(self):
        super().__init__()
        self._name = "Base Module"
        self._description = "Abstract base module"
        self._icon = None

    @property
    def name(self):
        """Nombre amigable del módulo."""
        return self._name

    @property
    def description(self):
        """Breve descripción de la funcionalidad del módulo."""
        return self._description

    @property
    def icon(self):
        """Icono representativo del módulo (emoji o ruta)."""
        return self._icon

    def get_view(self) -> QWidget:
        """
        Retorna el QWidget que representa la interfaz de usuario del módulo.
        Debe ser implementado por las clases hijas.
        """
        pass

    def on_load(self, context=None):
        """
        Se llama cuando el Core carga el módulo.
        :param context: Diccionario con servicios inyectados (theme_manager, locale_manager, event_bus).
        """
        self.context = context

    def on_unload(self):
        """Lógica de limpieza opcional cuando se descarga el módulo."""
        pass

    def run_headless(self, params: dict, input_data: any) -> any:
        """
        Ejecuta la lógica del módulo sin interfaz gráfica.
        Útil para automatización o procesamiento por lotes externo.
        """
        pass

    def load_image_set(self, paths: list):
        """
        Interfaz estándar para recibir una lista de rutas de imagen
        desde otros módulos (ej. desde Librarian o Gallery).
        """
        pass

    def tr(self, key, default=None):
        """
        Traduce una clave usando el LocaleManager inyectado en el contexto.
        Si el servicio no está disponible, retorna el valor por defecto o la clave.
        """
        if hasattr(self, 'context') and self.context and 'locale_manager' in self.context:
            return self.context['locale_manager'].tr(key, default)
        return default if default else key
