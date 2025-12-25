
from abc import ABC, abstractmethod
from PySide6.QtWidgets import QWidget

class BaseModule(ABC):
    """
    Abstract base class for all Panopticon modules.
    """
    def __init__(self):
        self._name = "Base Module"
        self._description = "Abstract base module"
        self._icon = None

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return self._description

    @abstractmethod
    def get_view(self) -> QWidget:
        """
        Returns the QWidget that represents the module's primary view.
        """
        pass

    def on_load(self):
        """
        Called when the module is loaded by the core.
        """
        pass

    def on_unload(self):
        """
        Called when the module is unloaded.
        """
        pass
