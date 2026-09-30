"""Adaptador de exportación para LibraryReader: ejecución en su worker, sin SQL Qt."""
from core.curation_sidecar import export_curation


class CurationExportTasks:
    def __init__(self, db_path):
        self.db_path = str(db_path)  # Captura sin filesystem ni conexiones.

    def read(self, operation, *, folder, cancel_event=None):
        if operation != 'export_curation':
            raise ValueError('unknown_curation_task')
        if not isinstance(folder, str) or not folder:
            raise ValueError('invalid_export_folder')
        cancelled = cancel_event.is_set if cancel_event is not None else None
        return export_curation(self.db_path, folder, cancelled=cancelled)
