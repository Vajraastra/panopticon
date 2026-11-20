"""Shared nonblocking scope for Cherry Sync, fusion and Native maintenance."""
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import threading

from core.file_guard import file_guard
from core.pinned_move import DirectoryPins, final_path, identity128
from core.paths import ProjectPaths

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_MAGIC = b'panopticon-cherry-profile-v1\n'



def physical_identity(volume, file_id):
    raw = bytes.fromhex(file_id)
    return volume, int.from_bytes(raw[:8], 'little'), int.from_bytes(raw[8:], 'little')


def require_available_profile(service, ticket):
    """Filesystem producers must respect the durable Native reservation too."""
    ticket.assert_current()
    if service is None:
        raise ValueError('cherry_native_service_unavailable')
    pending = service.read('get_maintenance_pending', ticket.identity)
    if type(pending) is not dict or type(pending.get('operations')) is not list:
        raise ValueError('cherry_profile_guard_invalid')
    if pending['operations']:
        raise ValueError('cherry_profile_recovery_required')


@dataclass(frozen=True)
class ScopeTicket:
    folder: Path
    canonical: str
    identity: tuple
    directories: DirectoryPins
    record: dict

    def assert_current(self):
        if not self.directories.active or self.directories.closing:
            raise ValueError('cherry_profile_scope_closed')
        kernel = self.directories.kernel
        handle = self.record['handle']
        volume, file_id = identity128(kernel, handle)
        current = physical_identity(volume, file_id)
        canonical = str(final_path(kernel, handle)).replace('\\', '/')
        if current != self.identity or canonical != self.canonical:
            raise ValueError('cherry_profile_identity_changed')
        if os.path.normcase(str(self.folder.resolve(strict=True))) != os.path.normcase(str(final_path(kernel, handle))):
            raise ValueError('cherry_profile_path_changed')
        return True


@contextmanager
def profile_scope(folder):
    """Nonblocking process and cross-process exclusion keyed by directory identity."""
    with ExitStack() as stack:
        pins = stack.enter_context(DirectoryPins())
        try:
            record = pins.pin(folder)
        except FileNotFoundError as error:
            raise ValueError('cherry_profile_missing') from error
        physical = physical_identity(record['volume'], record['file_id'])
        key = hashlib.sha256(repr(physical).encode('ascii')).hexdigest()
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            raise ValueError('cherry_profile_busy')
        stack.callback(lock.release)
        cache = ProjectPaths.root() / 'cache' / 'cherry_profile_guards'
        cache.mkdir(parents=True, exist_ok=True)
        pins.pin(cache)
        stack.enter_context(file_guard(cache / (key + '.lock'), _MAGIC,
            busy_reason='cherry_profile_busy', invalid_reason='cherry_profile_guard_invalid'))
        ticket = ScopeTicket(Path(folder), record['path'].replace('\\', '/'), physical, pins, record)
        ticket.assert_current()
        yield ticket


@asynccontextmanager
async def async_profile_scope(folder):
    # Called in CherryRuntime, never the Qt GUI thread. Acquisition cannot wait.
    with profile_scope(folder) as ticket:
        yield ticket
