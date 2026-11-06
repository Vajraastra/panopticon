"""Fachada especializada: construirla no toca disco ni SQLite."""
from core.paths import ProjectPaths
from .profile_service import get_profile_service


class ProfileDB:
    def __init__(self, path=None, service=None, *, mode='arcface'):
        self.db_path = str(path or ProjectPaths.root() / 'data' / 'character_profiles.db')
        self.service = service or get_profile_service(self.db_path, mode=mode)

    def add_reference(self, name, embedding):
        """API síncrona para consumidores fuera de Qt; errores propagados."""
        return self.service.submit('add_reference', name, embedding).result()

    def get_snapshot(self):
        """Caché inmutable por revisión; esperar sólo desde workers/headless."""
        return self.service.submit('snapshot').result()

    def get_all_profiles(self):
        return list(self.get_snapshot().profiles)
