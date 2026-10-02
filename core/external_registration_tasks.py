"""LibraryReader adapter: explicit registration in its owned worker, never GUI."""
from core.external_registration import register
from core.output_paths import safe_path
from pathlib import Path


class ExternalRegistrationTasks:
    def __init__(self, service):
        self.service = service

    def read(self, operation, path, *, cancel_event=None):
        if operation != 'register_external_image' or not isinstance(path, str) or not path:
            raise ValueError('invalid_external_registration_task')
        source = safe_path(Path(path).absolute())
        return dict(path=path, stored_path=str(source), copy_id=register(self.service, source, cancel_event))
