"""Resultado explícito de un recorrido: un fallo nunca equivale a una lista vacía."""
import os
import stat
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum


class InventoryStatus(str, Enum):
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass
class InventoryResult:
    files: list[dict] = field(default_factory=list)
    status: InventoryStatus = InventoryStatus.COMPLETE
    issues: list[str] = field(default_factory=list)
    root_identity: tuple | None = None

    @property
    def complete(self):
        return self.status == InventoryStatus.COMPLETE

    @property
    def paths(self):
        return {os.path.normpath(f["path"]).replace("\\", "/") for f in self.files}

    def fail(self, status, issue=None):
        if self.complete or status == InventoryStatus.CANCELLED:
            self.status = status
        if issue:
            self.issues.append(str(issue))

    def merge(self, other):
        self.files.extend(other.files)
        if not other.complete:
            self.fail(other.status)
        self.issues.extend(other.issues)


def directory_identity(folder):
    """Exige raíz accesible y listable; permite detectar reemplazos antes del sweep."""
    info = os.stat(folder)
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(str(folder))
    with os.scandir(folder) as entries:
        next(entries, None)
    return info.st_dev, info.st_ino


def directory_chain_identity(folder):
    """Padre listable, sin enlaces/reparse points que oculten otro volumen offline."""
    path = Path(folder)
    for entry in (path, *path.parents):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise OSError('linked_directory_requires_review')
    return directory_identity(path)
