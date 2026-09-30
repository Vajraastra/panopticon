"""Recovery en worker: cancelación antes de iniciar, sin DB central ni trabajo Qt."""
from core.curation_recovery import inspect_recovery, quarantine_recovery


class CurationRecoveryTasks:
    def read(self, operation, *, folder, expected_fingerprint=None, cancel_event=None):
        if operation not in ('inspect_recovery', 'quarantine_recovery'):
            raise ValueError('unknown_recovery_task')
        if not isinstance(folder, str) or not folder:
            raise ValueError('invalid_export_folder')
        if cancel_event is not None and cancel_event.is_set():
            raise ValueError('recovery_cancelled')
        if operation == 'inspect_recovery':
            return inspect_recovery(folder)
        # Una vez iniciada, la cuarentena termina o conserva su plan de fallo parcial.
        # Cerrar la ventana sólo descarta la entrega; no interrumpir entre renombres.
        return quarantine_recovery(folder, expected_fingerprint=expected_fingerprint)
