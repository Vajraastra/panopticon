"""Transfer activity and quota waits, independent of the presentation timer."""

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerState:
    slot: int
    phase: str
    reason: str = ""
    until: float | None = None


class TransferStatus:
    """Announce a pause only when every remaining worker is waiting."""

    def __init__(self, workers: int) -> None:
        self.workers = {
            slot: WorkerState(slot, "preparing") for slot in range(workers)
        }
        self.paused_reason = ""

    def update(self, state: WorkerState) -> tuple[str, WorkerState] | None:
        self.workers[state.slot] = state
        waiting = [s for s in self.workers.values() if s.phase == "waiting"]
        if waiting and all(
            s.phase in {"waiting", "idle"} for s in self.workers.values()
        ):
            timed = [s for s in waiting if s.until is not None]
            next_wait = min(timed, key=lambda s: s.until) if timed else waiting[0]
            if self.paused_reason != next_wait.reason:
                self.paused_reason = next_wait.reason
                return "paused", next_wait
        elif state.phase == "downloading" and self.paused_reason:
            self.paused_reason = ""
            return "resumed", state
        return None
