"""Guardas de documentación externa y enlaces; nunca abren DBs portables."""
import os
from pathlib import Path


def linked(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def plain_chain(path):
    for entry in (path, *path.parents):
        if linked(entry):
            raise ValueError('duplicate_retirement_linked_path')


def documented(path, sidecars=None):
    sidecars = {} if sidecars is None else sidecars
    for suffix in ('.txt', '.xmp', '.json'):
        if os.path.lexists(path.with_suffix(suffix)) or os.path.lexists(Path(str(path)+suffix)):
            return True
    # El usuario puede seleccionar una subcarpeta del proyecto portable: revisar
    # también sus ancestros externos al scope, hasta la raíz del volumen.
    for parent in path.parents:
        if parent not in sidecars:
            sidecars[parent] = os.path.lexists(parent/'panopticon.curation.db')
        if sidecars[parent]:
            return True
    return False
