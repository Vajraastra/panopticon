"""Worker admission for bounded observations; accepted writes keep Native FIFO."""
from concurrent.futures import CancelledError, TimeoutError

from core.library_service import LibraryQueueFull

MAX_BATCH_ROWS = 100
_OPERATIONS = frozenset({'register_files_minimal', 'register_files_observed', 'set_hash_original_bulk', 'store_path_identity_batch'})


class BatchCancelled(RuntimeError):
    pass


def commit_batch(service, operation, records, cancel_event):
    """One outstanding Future; retry only queue_full (never accepted).

    Call from a worker with producer-owned records, not a GUI slot. Cancellation
    removes a queued command when possible. A running command must settle before
    returning so the producer can count a confirmed batch, even after Stop.
    Count bounds do not replace Native's independent payload/field limits.
    """
    if operation not in _OPERATIONS:
        raise ValueError('unsupported_observation_batch')
    if type(records) not in (list, tuple) or len(records) > MAX_BATCH_ROWS:
        raise ValueError('observation_batch_too_large')
    if cancel_event.is_set():
        raise BatchCancelled('observation_batch_cancelled')
    if not records:
        return True
    return _commit(service, operation, (records,), cancel_event)


def commit_control(service, operation, cancel_event):
    """Explicit idempotent schema preparation with the same FIFO admission."""
    if operation != 'prepare_path_identity':
        raise ValueError('unsupported_control_command')
    return _commit(service, operation, (), cancel_event)


def _commit(service, operation, args, cancel_event):
    while True:
        if cancel_event.is_set():
            raise BatchCancelled('observation_batch_cancelled')
        future = service.submit(operation, *args)
        try:
            while True:
                if cancel_event.is_set() and future.cancel():
                    raise BatchCancelled('observation_batch_cancelled')
                try:
                    return future.result(timeout=.05)
                except TimeoutError:
                    if future.done():
                        # Completion can race the timeout. Retrieve the settled
                        # result/error instead of declaring a confirmed write lost.
                        return future.result()
        except CancelledError as exc:
            raise BatchCancelled('observation_batch_cancelled') from exc
        except LibraryQueueFull as exc:
            # No SQL/transaction was accepted. Yield rather than flooding the
            # queue; shutdown/payload/SQL failures must never be retried here.
            if cancel_event.wait(.05):
                raise BatchCancelled('observation_batch_cancelled') from exc
