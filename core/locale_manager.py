import json
import os
import locale
import logging

log = logging.getLogger(__name__)

class LocaleManager:
    """
    Gestor de localización Singleton.
    Maneja la detección del idioma del sistema, la persistencia en config.json
    y la carga de diccionarios de traducción desde archivos .json.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(LocaleManager, cls).__new__(cls)
            cls._instance.current_locale = "en"
            cls._instance.translations = {}
            cls._instance._detect_locale()
            cls._instance._load_translations()
        return cls._instance

    def set_locale(self, locale_code):
        """Cambia el idioma activo, recarga traducciones y guarda la configuración."""
        if locale_code in ["en", "es"]:
            self.current_locale = locale_code
            self._load_translations()
            self._save_config()

    def _save_config(self):
        """Guarda la preferencia de idioma en config.json."""
        config = {"locale": self.current_locale}
        try:
            with open("config.json", "w") as f:
                json.dump(config, f)
        except Exception as e:
            log.warning("Error saving locale config: %s", e)

    def _detect_locale(self):
        """Detecta el idioma: primero desde config.json, luego del sistema operacional."""
        # Prioridad 1: Configuración guardada
        if os.path.exists("config.json"):
            try:
                with open("config.json", "r") as f:
                    config = json.load(f)
                    if "locale" in config:
                        self.current_locale = config["locale"]
                        return
            except Exception as e:
                log.warning("Could not read config.json: %s", e)

        # Prioridad 2: Idioma del sistema (locale.getlocale() — compatible Python 3.11+)
        try:
            sys_lang = (locale.getlocale()[0] or "")
        except Exception:
            sys_lang = ""
        if sys_lang.startswith("es"):
            self.current_locale = "es"
        else:
            self.current_locale = "en"

    def get_locale(self):
        """Retorna el código del idioma actual (es/en)."""
        return self.current_locale

    def _load_translations(self):
        """Carga el archivo JSON correspondiente al idioma actual desde la carpeta /locales."""
        base_path = os.path.join(os.getcwd(), "locales")
        path = os.path.join(base_path, f"{self.current_locale}.json")

        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.translations = json.load(f)
            except Exception as e:
                log.warning("Error loading locale %s: %s", self.current_locale, e)
                self.translations = {}
        else:
            log.warning("Locale file not found: %s (using key defaults)", path)
            self.translations = {}

    def tr(self, key, default=None):
        """
        Busca la traducción de una clave.
        :param key: Identificador en el JSON (ej. 'app.title').
        :param default: Valor de respaldo si no se encuentra la clave.
        :return: El texto traducido o el valor por defecto.
        """
        val = self.translations.get(key)
        if val:
            return val
        return default if default is not None else key
