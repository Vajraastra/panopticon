from PySide6.QtWidgets import QWidget
from PySide6.QtCore import QObject

class BaseModule(QObject):
    """
    Abstract base class for all Panopticon modules.
    """
    def __init__(self):
        super().__init__()
        self._name = "Base Module"
        self._description = "Abstract base module"
        self._icon = None

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return self._description

    def get_view(self) -> QWidget:
        """
        Returns the QWidget that represents the module's primary view.
        """
        pass

    def on_load(self, context=None):
        """
        Called when the module is loaded by the core.
        :param context: A dictionary or object containing core services (theme_manager, settings, etc.)
        """
        self.context = context

    def on_unload(self):
        """
        Called when the module is unloaded.
        """
        pass

    def run_headless(self, params: dict, input_data: any) -> any:
        """
        Execute the module's core logic without UI. 
        Required for automation and node systems.
        :param params: Dictionary of settings/parameters.
        :param input_data: The input to process (e.g., list of file paths).
        :return: Processed data or result.
        """
        pass

    def load_image_set(self, paths: list):
        """
        Populate the module with a specific set of images.
        To be implemented by child modules that handle image sets.
        """
        pass

    def tr(self, key, default=None):
        """Translate a key using the LocaleManager in context."""
        if hasattr(self, 'context') and self.context and 'locale_manager' in self.context:
            return self.context['locale_manager'].tr(key, default)
        return default if default else key
