"""Guard de export/recovery; compartido y liberado por kernel al morir el proceso."""
from pathlib import Path
from core.file_guard import file_guard

GUARD = '.panopticon.curation.db.guard'
MAGIC = b'PANOPTICON_CURATION_EXPORT_GUARD_V1\n'


def export_guard(folder, *, create=True):
    return file_guard(Path(folder) / GUARD, MAGIC, create=create,
        busy_reason='sidecar_export_busy', invalid_reason='invalid_export_guard')
