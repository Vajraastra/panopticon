"""Entrega Qt sin compartir conexiones ni esperar SQLite desde la GUI."""
import threading
import weakref

from PySide6.QtCore import QObject, Qt, Signal, Slot
from core.library_payload import snapshot_read
from core.library_service import LibraryError


class LibraryReader(QObject):
    """Un lector activo y una petición pendiente reemplazable por consumidor.

    request/close se llaman en el hilo de este QObject. Los tickets invalidan
    resultados anteriores incluso si SQLite terminó antes de la cancelación.
    """
    result = Signal(int, object, object)
    _delivered = Signal(int, object, object)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._ticket = 0
        self._active = False
        self._pending = None
        self._closed = False
        self._cancel = None
        self._thread = None
        self._threads = set()
        self._delivered.connect(self._receive, Qt.QueuedConnection)
        self.destroyed.connect(lambda: self.close())

    def request(self, operation, *args, **kwargs):
        if self._closed:
            raise RuntimeError('Reader closed')
        self._ticket += 1
        error = None
        try:
            saved_args, saved_kwargs = snapshot_read(args, kwargs)
        except ValueError as exc:
            saved_args, saved_kwargs = (), {}
            error = LibraryError(operation, str(exc))
        request = self._ticket, operation, saved_args, saved_kwargs, error
        if self._active:
            self._pending = request
            self._cancel.set()
        else:
            self._start(request)
        return self._ticket

    def _start(self, request):
        self._active = True
        cancel = self._cancel = threading.Event()
        ticket, operation, args, kwargs, preflight_error = request
        if preflight_error is not None:
            # Use the same queued delivery/state machine, including latest-only
            # pending requests. Never open SQL or launch a thread for this error.
            self._delivered.emit(ticket, None, preflight_error)
            return

        def run():
            value = error = None
            try:
                value = getattr(self.service, 'read_worker', self.service.read)(operation, *args, cancel_event=cancel, **kwargs)
            except Exception as exc:
                error = exc
            try:
                self._delivered.emit(ticket, value, error)
            except RuntimeError:
                pass  # El dueño Qt fue destruido; la conexión ya está cerrada.

        self._threads = {thread for thread in self._threads if thread.is_alive()}
        self._thread = threading.Thread(target=run, name='PanopticonRead', daemon=True)
        self._threads.add(self._thread)
        try:
            self._thread.start()
        except Exception as exc:
            self._threads.discard(self._thread)
            self._thread = None
            self._delivered.emit(ticket, None, exc)

    @Slot(int, object, object)
    def _receive(self, ticket, value, error):
        self._active = False
        if self._closed:
            return
        if self._pending is not None:
            pending, self._pending = self._pending, None
            self._start(pending)
        elif ticket == self._ticket:
            self.result.emit(ticket, value, error)

    def invalidate(self):
        self._ticket += 1
        self._pending = None
        if self._cancel is not None:
            self._cancel.set()

    def close(self):
        self._closed = True
        self.invalidate()

    def shutdown_ready(self):
        # A queued delivery is not native completion. Keep even a preceding
        # reader whose delivery started a new request while its emit unwinds.
        return all(not thread.is_alive() for thread in self._threads)


class LibraryWriter(QObject):
    """Puente de Futures del escritor central a slots del hilo Qt del dueño."""
    result = Signal(int, object, object)
    _delivered = Signal(int, object, object)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._ticket = 0
        self._closed = False
        self._delivered.connect(self._receive, Qt.QueuedConnection)

    def submit(self, operation, *args, **kwargs):
        if self._closed:
            raise RuntimeError('Writer bridge closed')
        self._ticket += 1
        ticket = self._ticket
        receiver = weakref.ref(self)

        def completed(future):
            owner = receiver()
            if owner is None:
                return
            value = error = None
            try:
                value = future.result()
            except Exception as exc:
                error = exc
            try:
                owner._delivered.emit(ticket, value, error)
            except RuntimeError:
                pass

        self.service.submit(operation, *args, **kwargs).add_done_callback(completed)
        return ticket

    @Slot(int, object, object)
    def _receive(self, ticket, value, error):
        if not self._closed:
            self.result.emit(ticket, value, error)

    def close(self):
        # Las escrituras aceptadas siguen a cargo del servicio; no perder curación.
        self._closed = True
