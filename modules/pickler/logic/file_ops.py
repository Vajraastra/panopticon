"""
Operaciones de archivo del módulo Pickler.

Lógica pura, sin UI. Dos acciones de curación, ambas NO destructivas sobre el
original (biblia §9, Regla #2/#3 — copias primero, cleanup con aprobación):

  • reject() → mueve a una papelera RECUPERABLE dentro del cache central.
  • keep()   → mueve o copia el archivo a una carpeta destino elegida.

Ninguna función usa `Path.unlink()` sobre el original. Todas resuelven
colisiones de nombre añadiendo un sufijo `_1`, `_2`, … para no sobrescribir.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from core.paths import CachePaths


def rejected_folder() -> Path:
    """Papelera recuperable del Pickler: cache/pickler/rejected/."""
    folder = CachePaths.get_tool_cache("pickler") / "rejected"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _unique_path(dest_dir: Path, name: str) -> Path:
    """Devuelve una ruta libre en dest_dir; si existe, añade sufijo _N."""
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 1
    while True:
        candidate = dest_dir / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def reject(path: Path) -> Path:
    """
    Envía la imagen a la papelera recuperable.
    Devuelve la ruta nueva (para poder deshacer).
    """
    path = Path(path)
    dest = _unique_path(rejected_folder(), path.name)
    shutil.move(str(path), str(dest))
    return dest


def keep(path: Path, dest_dir: str | Path, copy: bool) -> Path:
    """
    Conserva la imagen moviéndola o copiándola a dest_dir según `copy`.
    Devuelve la ruta nueva (para poder deshacer).
    """
    path = Path(path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(dest_dir, path.name)
    if copy:
        shutil.copy2(str(path), str(dest))  # copy2 preserva mtime/metadata básica
    else:
        shutil.move(str(path), str(dest))
    return dest


def undo(action: str, original: str | Path, current: str | Path) -> Path:
    """
    Revierte la última acción.

    :param action: "keep_move", "keep_copy" o "reject".
    :param original: ruta original del archivo.
    :param current: ruta donde quedó tras la acción.
    :return: la ruta original restaurada.
    """
    original = Path(original)
    current = Path(current)
    if action == "keep_copy":
        # Fue una copia: el original sigue ahí; basta con borrar la copia.
        if current.exists():
            current.unlink()
        return original
    # keep_move o reject: el archivo se movió; lo regresamos a su sitio.
    original.parent.mkdir(parents=True, exist_ok=True)
    if current.exists():
        shutil.move(str(current), str(original))
    return original
