"""Worker target for explicit preparation and diagnostics, independent of Qt."""
import threading
import time


class PathIdentityTasks:
    def __init__(self, service, progress=None):
        self.service,self.progress=service,progress
        self.cancel=threading.Event()
        self.token=0

    def begin(self, token):
        self.token=token
        self.cancel=threading.Event()

    def stop(self):
        self.cancel.set()

    def read(self, operation, *args, cancel_event=None, **kwargs):
        if operation=='prepare':
            token=kwargs['token']
            if token!=self.token:
                raise ValueError('path_identity_stale_job')
            event=self.cancel
            if cancel_event is not None and cancel_event.is_set():
                event.set()
            last=0
            def progress(row):
                nonlocal last
                now=time.monotonic()
                if self.progress is not None and now-last>=.25:
                    last=now
                    self.progress(token,row)
            return self.service.backfill_path_identity(cancel_event=event,progress=progress)
        if operation=='resolve':
            return self.service.resolve_path_identity(*args,cancel_event=cancel_event)
        if operation in ('get_path_identity_status','get_path_identity_review_page'):
            return self.service.read(operation,*args,cancel_event=cancel_event,**kwargs)
        raise ValueError('unknown_path_identity_task')
