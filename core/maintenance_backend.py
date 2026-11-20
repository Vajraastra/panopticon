"""Worker adapter for the Native LibraryService maintenance protocol."""
from concurrent.futures import TimeoutError
from core.library_maintenance_operations import require

class MaintenanceBackend:
    def __init__(self,service):
        self.service=service
        require(hasattr(service,'maintenance_owner_nonce'),'maintenance_service_required')

    @property
    def owner_nonce(self):return self.service.maintenance_owner_nonce

    def write(self,name,*args,**kwargs):
        future=self.service.submit(name,*args,**kwargs)
        while True:
            try:
                result=future.result(timeout=.1);require(result is True,'native_write_not_confirmed');return result
            except TimeoutError:
                if future.done():raise
            except KeyboardInterrupt:
                # Accepted SQL work must settle before worker finally unbinds
                # and closes its real root/source directory handles.
                continue

    def read(self,name,*args,**kwargs):return self.service.read(name,*args,**kwargs)


Backend=MaintenanceBackend
