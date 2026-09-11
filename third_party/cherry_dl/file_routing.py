"""Rutas, nombres y espacios de contador por tipo de archivo."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .downloads import EXT_GROUPS, build_filename


@dataclass(frozen=True)
class FileRoute:
    key: str
    folder: str
    prefix: str


VISUAL = FileRoute("visual", "", "")
ARCHIVE = FileRoute("archive", "Comprimidos", "arc")
PROJECT = FileRoute("project", "Proyectos", "pro")
VIDEO = FileRoute("video", "Videos", "vid")
AUDIO = FileRoute("audio", "Audio", "aud")
DOCUMENT = FileRoute("document", "Documentos", "doc")
OTHER = FileRoute("other", "Otros", "otr")
ROUTES = (VISUAL, ARCHIVE, PROJECT, VIDEO, AUDIO, DOCUMENT, OTHER)
LAYOUT_VERSION = 2
COUNTER_NAMESPACES = tuple(spec.key for spec in ROUTES)


def route(filename: str) -> FileRoute:
    """Clasifica por extensión; imágenes y animaciones comparten la raíz."""
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext in EXT_GROUPS["images"][1] | EXT_GROUPS["anim"][1]:
        return VISUAL
    if ext in EXT_GROUPS["zip"][1]:
        return ARCHIVE
    if ext in EXT_GROUPS["project"][1]:
        return PROJECT
    if ext in EXT_GROUPS["video"][1]:
        return VIDEO
    if ext in EXT_GROUPS["audio"][1]:
        return AUDIO
    if ext in EXT_GROUPS["docs"][1]:
        return DOCUMENT
    return OTHER


def category(filename: str) -> str:
    return route(filename).folder


def routed_name(artist: str, counter: int, original_filename: str) -> str:
    """Nombre v2: visual conserva el formato histórico; los demás llevan tipo."""
    spec = route(original_filename)
    legacy = build_filename(artist, counter, original_filename)
    if not spec.prefix:
        return legacy
    ext = Path(legacy).suffix
    marker = f"_{counter:05d}"
    stem = Path(legacy).stem
    artist_part = stem[:-len(marker)] if stem.endswith(marker) else stem
    return f"{spec.prefix}-{artist_part}-{counter:05d}{ext}"


def relative_filename(filename: str) -> str:
    """Nombre que debe persistirse en catalog.db, relativo al proyecto."""
    folder = category(filename)
    return (Path(folder) / filename).as_posix() if folder else filename


def destination(project: Path, filename: str) -> tuple[Path, str]:
    """Carpeta física de descarga y ruta relativa que se guarda en catálogo."""
    relative = relative_filename(filename)
    return project / Path(relative).parent, relative


def prepare_selected_folders(project: Path, extensions: set[str]) -> set[str]:
    """Crea las subcarpetas no visuales elegidas explícitamente por el usuario."""
    folders = {
        category("file" + ext.lower() if ext.startswith(".") else "file." + ext.lower())
        for ext in extensions
    }
    folders.discard("")
    for folder in folders:
        (project / folder).mkdir(parents=True, exist_ok=True)
    return folders
