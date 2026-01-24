
import os
import importlib
import inspect
from core.base_module import BaseModule

class ModuleLoader:
    """
    Encargado del descubrimiento y carga dinámica de módulos.
    Escanea la carpeta /modules en busca de subcarpetas que contengan
    una implementación válida de BaseModule.
    """
    def __init__(self, modules_dir="modules"):
        self.modules_dir = modules_dir
        self.loaded_modules = {}

    def discover_modules(self):
        """
        Escanea el directorio de módulos y retorna una lista de nombres de carpetas.
        Ignora archivos internos y carpetas ocultas.
        """
        if not os.path.exists(self.modules_dir):
            return []
        
        folders = [f for f in os.listdir(self.modules_dir) 
                   if os.path.isdir(os.path.join(self.modules_dir, f)) 
                   and not f.startswith("__")]
        return folders

    def load_module(self, module_name, context=None):
        """
        Carga un módulo específico por su nombre utilizando importación dinámica.
        Busca una clase dentro de 'module.py' que herede de BaseModule.
        :param module_name: Nombre de la carpeta del módulo.
        :param context: Diccionario de servicios (Theme, EventBus) para inyectar vía on_load.
        :return: Instancia del módulo cargado o None si falla.
        """
        if module_name in self.loaded_modules:
            return self.loaded_modules[module_name]

        try:
            # Importación dinámica del archivo module.py dentro de la carpeta del módulo
            module_path = f"modules.{module_name}.module"
            imported_module = importlib.import_module(module_path)

            # Inspección de miembros para encontrar la clase que hereda de BaseModule
            for name, obj in inspect.getmembers(imported_module):
                if inspect.isclass(obj) and issubclass(obj, BaseModule) and obj is not BaseModule:
                    instance = obj()
                    # Inyección de dependencias
                    instance.on_load(context)
                    self.loaded_modules[module_name] = instance
                    return instance
        except Exception as e:
            print(f"Error loading module {module_name}: {e}")
            return None
        
        return None
