"""Tipo de archivo → icono, etiqueta y categoría, igual en toda la app.

Sin Qt: lo usan el distintivo de ``core/components/file_badge.py`` y los resúmenes
de conjunto («98 imágenes · 22 comprimidos»). La categoría decide el color del
distintivo en el tema (``QLabel[role="badge"][kind="…"]``).
"""
from __future__ import annotations

from pathlib import PureWindowsPath

IMAGE, ANIMATED, ARCHIVE, OTHER = 'image', 'animated', 'archive', 'other'

_KINDS = {
    '.png': ('🖼', 'PNG', IMAGE),
    '.jpg': ('📷', 'JPG', IMAGE),
    '.jpeg': ('📷', 'JPG', IMAGE),
    '.webp': ('🌐', 'WEBP', IMAGE),
    '.avif': ('✨', 'AVIF', IMAGE),
    '.bmp': ('🧱', 'BMP', IMAGE),
    '.tif': ('🗂', 'TIFF', IMAGE),
    '.tiff': ('🗂', 'TIFF', IMAGE),
    '.gif': ('🎞', 'GIF', ANIMATED),
    '.zip': ('📦', 'ZIP', ARCHIVE),
    '.rar': ('🗜', 'RAR', ARCHIVE),
    '.cbz': ('📚', 'CBZ', ARCHIVE),
    '.cbr': ('📚', 'CBR', ARCHIVE),
}


def file_kind(path) -> tuple[str, str, str]:
    """(icono, etiqueta, categoría) según la extensión."""
    suffix = PureWindowsPath(str(path)).suffix.lower()
    return _KINDS.get(suffix, ('📄', suffix[1:].upper() or '?', OTHER))


def category(path) -> str:
    return file_kind(path)[2]


def format_size(size) -> str:
    """Tamaño legible en unidades binarias: B, KB, MB, GB, TB."""
    if size is None:
        return '—'
    value = float(size)
    if value < 1024:
        return f'{int(value)} B'
    for unit in ('KB', 'MB', 'GB', 'TB'):
        value /= 1024
        if value < 1024 or unit == 'TB':
            return f'{value:.1f} {unit}' if value < 100 else f'{value:.0f} {unit}'
