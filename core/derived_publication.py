"""Diario y confirmación de un derivado; sólo fachada Native, nunca SQLite.

El productor verifica disco. El diario previo permite revisar una publicación
interrumpida; ni el diario ni el recibo garantizan atomicidad FS/SQL.
"""
from concurrent.futures import TimeoutError
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from core.derived_copies import _request
from core.duplicate_snapshot import encode
from core.output_paths import safe_path
from core.paths import CachePaths


def write(service, operation, *args, event=None, **kwargs):
    """Cancelar sólo comandos aún pendientes; drenar los ya admitidos.

    Después de publicar llamar SIN evento: un timeout/cancelación no demuestra
    rollback. El worker espera confirmación sin bloquear la GUI.
    """
    if event is not None and event.is_set():
        raise ValueError('optimizer_cancelled')
    future = service.submit(operation, *args, **kwargs)
    while True:
        try:
            result = future.result(timeout=.1)
            if result is not True:
                raise ValueError('derived_write_unconfirmed')
            return
        except TimeoutError:
            if future.done():
                raise  # Una excepción del comando, no un timeout de la espera.
            if event is not None and event.is_set() and future.cancel():
                raise ValueError('optimizer_cancelled')


class DerivedPublication:
    def __init__(self, service, copy_id, request, *, journal_root=None):
        self.service, self.copy_id = service, copy_id
        self.request = dict(request, operation_id=str(uuid4()))
        self.root = Path(journal_root) if journal_root is not None else CachePaths.get_cache_root()/'derived_recovery'
        self.path = None
        self.native_registered = False
        self.receipt = None
        self.data = dict(version=1, operation_id=self.request['operation_id'], copy_id=copy_id,
                         phase='prepared', published=False, native_confirmation='unconfirmed',
                         request=self.request)

    def scope(self, event=None):
        actual = safe_path(self.root, exists=os.path.lexists(self.root), portable=True)
        for watched in self.service.read('get_duplicate_retirement_roots', cancel_event=event):
            if event is not None and event.is_set():
                raise ValueError('optimizer_cancelled')
            root = Path(watched)
            if not root.is_absolute():
                raise ValueError('derived_journal_scope_requires_review')
            if root.drive.casefold() != actual.drive.casefold():
                continue
            root = root.resolve(strict=False)
            try:
                within = os.path.commonpath((str(actual).casefold(), str(root).casefold())) == str(root).casefold()
            except ValueError:
                within = False
            if within:
                raise ValueError('derived_journal_inside_library')
        # portable=True comprueba padres; comprobar además marcadores en la raíz.
        if any(os.path.lexists(actual/name) for name in ('catalog.db', 'panopticon.curation.db')):
            raise ValueError('metadata_cherry_original_protected')
        return actual

    def save(self):
        raw = encode(self.data)
        if len(raw) > 512*1024:
            raise ValueError('derived_journal_limit')
        safe_path(self.path, exists=os.path.lexists(self.path), portable=True)
        fd, name = tempfile.mkstemp(prefix='.journal-', suffix='.tmp', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.lexists(name): os.unlink(name)

    def ready(self, source_identity, result_identity, prepared_output, event):
        # Valida también el payload antes de persistirlo: mismo formato canónico SQL.
        self.fingerprint()
        write(self.service, 'approve_derived_copy', self.copy_id, event=event, **self.request)
        self.root = self.scope(event)
        self.root.mkdir(exist_ok=True)
        safe_path(self.root, portable=True)
        folder = self.root/self.request['operation_id']
        folder.mkdir()
        self.path = folder/'journal.json'
        self.data.update(phase='ready_to_publish', source_identity=source_identity,
                         result_identity=result_identity, prepared_output=str(prepared_output))
        self.save()

    def fingerprint(self):
        return _request(self.copy_id, **self.request,
                        normalize=lambda path: os.path.normpath(path).replace('\\', '/'))[3]

    def confirm(self):
        receipt = self.service.read('get_derived_copy_receipt', self.request['operation_id'])['receipt']
        if receipt is None or receipt['request_fingerprint'] != self.fingerprint():
            raise ValueError('derived_receipt_unconfirmed')
        self.receipt = receipt
        self.native_registered = True
        self.data.update(native_confirmation='confirmed', receipt=receipt)

    def register(self, observation, result_identity):
        self.request['observation'] = observation
        self.data.update(phase='published', published=True, result_identity=result_identity)
        self.save()  # La petición definitiva queda durable ANTES del alta.
        try:
            write(self.service, 'register_derived_copy', self.copy_id, **self.request)
        except Exception as exc:
            # Un error de transporte/confirmación podría llegar después del commit.
            # Consultar el recibo histórico; jamás reenviar ni borrar a ciegas.
            try: self.confirm()
            except Exception: raise exc
        else:
            self.confirm()

    def complete(self):
        self.data['phase'] = 'complete'
        self.save()

    def failed(self, error, published):
        self.data.update(phase='review_required' if published else 'not_published',
                         published=published, error=error)
        if self.path is not None:
            try: self.save()
            except Exception: pass  # El último diario durable permanece; no es éxito.
