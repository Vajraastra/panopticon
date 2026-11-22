"""Reproduce cross-thread batch transitions without network or user data."""
import asyncio
import os
from pathlib import Path
import sys
import threading
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, Qt
from modules.cherry.logic.download_bridge import DownloadBridge
from core.library_service import LibraryService
from modules.cherry.logic.download_queue import DownloadQueue
from modules.cherry.logic.runtime import CherryRuntime
from third_party.cherry_dl.download_service import DownloadSummary


def pump(condition, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if condition():
            return True
        time.sleep(.002)
    return condition()


def profile(pid):
    return {"id": pid, "display_name": str(pid), "urls": [
        {"url": "https://pawchive.pw/patreon/user/1", "enabled": True},
    ]}


class BridgeLifecycle(unittest.TestCase):
    def setUp(self):
        self.runtime = CherryRuntime()
        self.runtime.start()
        folder = Path(__file__).resolve().parents[1] / ".work"
        folder.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = LibraryService(self.root / "native.db")
        self.addCleanup(self.service.close)
        self.bridge = DownloadBridge(self.runtime, data_service=self.service)
        self.gates = []
        self.addCleanup(self.cleanup)

    def profile(self, pid):
        folder = self.root / str(pid)
        folder.mkdir(exist_ok=True)
        return dict(profile(pid), folder_path=str(folder))

    def cleanup(self):
        for gate in self.gates:
            gate.set()
        self.bridge.cancel()
        # Also drain tasks if a broken bridge lost its owner during reproduction.
        self.runtime.call_soon(lambda: [task.cancel()
                                       for task in tuple(self.runtime._thread.jobs)])
        self.runtime.request_stop()
        self.assertTrue(pump(self.runtime.shutdown_ready))

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def transition(self, failed=False):
        calls, done, notices, blocked = [], [], [], threading.Event()
        release = self.gate()
        queue = DownloadQueue(self.bridge)
        queue.queue_finished.connect(done.append)
        queue.profile_catalog_changed.connect(lambda profile: notices.append(profile['id']))

        # Hold the emitter inside emit() after Qt has queued the terminal event.
        # The GUI then advances before the worker can execute another statement.
        def pause_emitter(*args):
            if len(calls) == 1:
                blocked.set()
                release.wait(3)

        terminal = self.bridge.failed_signal if failed else self.bridge.finished_signal
        terminal.connect(pause_emitter, Qt.DirectConnection)

        async def download(prof, **kwargs):
            self.assertTrue(callable(kwargs.get('prepare_artist')))
            calls.append(prof["id"])
            if failed and len(calls) == 1:
                raise ValueError("synthetic first-profile failure")
            return DownloadSummary()

        with patch("third_party.cherry_dl.download_service.run_profile_download", download):
            self.assertTrue(queue.start([self.profile(1), self.profile(2)]))
            self.assertTrue(blocked.wait(1))
            self.assertTrue(pump(lambda: bool(done) or queue._current is not None
                                 and queue._current.state.profile["id"] == 2))
            self.assertTrue(self.bridge.is_busy, "next job must own the bridge")
            release.set()
            self.assertTrue(pump(lambda: bool(done)))
            self.assertEqual(calls, [1, 2])
            self.assertEqual(done[0].done, 1 if failed else 2)
            self.assertEqual(len(done[0].failed), int(failed))
            self.assertEqual(notices, [1] if failed else [])
            self.assertFalse(self.bridge.is_busy)

    def test_success_advances_even_if_emitter_has_not_returned(self):
        self.transition()

    def test_failure_advances_even_if_emitter_has_not_returned(self):
        self.transition(failed=True)

    def test_old_future_callback_cannot_release_next_job(self):
        queued, release, entered = [], self.gate(), threading.Event()
        first = self.gate()
        original = self.bridge._on_future_done

        def delayed(*args):
            first.set()
            release.wait(3)
            original(*args)

        async def download(prof, **kwargs):
            if prof["id"] == 2:
                entered.set()
                await asyncio.Event().wait()
            return DownloadSummary()

        self.bridge.finished_signal.connect(queued.append)
        with patch("third_party.cherry_dl.download_service.run_profile_download", download), \
                patch.object(self.bridge, "_on_future_done", delayed):
            self.assertTrue(self.bridge.start(self.profile(1)))
            self.assertTrue(first.wait(1))
            self.assertTrue(pump(lambda: bool(queued)))
            self.assertTrue(self.bridge.start(self.profile(2)))
            release.set()
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.bridge.is_busy)
            self.assertFalse(self.bridge.start(self.profile(3)))
            self.assertTrue(self.bridge.cancel())
            self.assertTrue(pump(lambda: not self.bridge.is_busy))

    def test_cancelled_future_keeps_busy_until_task_cleanup(self):
        entered, cleaning, cleaned = [threading.Event() for _ in range(3)]
        release = self.gate()
        cancelled, writes = [], []
        self.bridge.cancelled_signal.connect(lambda: cancelled.append(cleaned.is_set()))
        self.bridge.catalog_write_possible.connect(lambda: writes.append(cleaned.is_set()))

        async def download(prof, **kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await asyncio.to_thread(release.wait, 3)
                cleaned.set()

        with patch("third_party.cherry_dl.download_service.run_profile_download", download):
            self.assertTrue(self.bridge.start(self.profile(1)))
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.bridge._future.cancel())
            self.assertTrue(cleaning.wait(1))
            self.assertTrue(self.bridge.is_busy)
            self.assertFalse(self.bridge.start(self.profile(2)))
            QCoreApplication.processEvents()
            self.assertEqual(cancelled, [])
            release.set()
            self.assertTrue(pump(lambda: bool(cancelled)))
            self.assertEqual(cancelled, [True])
            self.assertEqual(writes, [True])
            self.assertFalse(self.bridge.is_busy)

    def test_cancel_before_admission_releases_once(self):
        entered, release = threading.Event(), self.gate()
        self.runtime.call_soon(lambda: (entered.set(), release.wait(3)))
        self.assertTrue(entered.wait(1))
        cancelled, calls, writes = [], [], []
        self.bridge.cancelled_signal.connect(lambda: cancelled.append(1))
        self.bridge.catalog_write_possible.connect(lambda: writes.append(True))

        async def download(prof, **kwargs):
            calls.append(prof["id"])
            return DownloadSummary()

        with patch("third_party.cherry_dl.download_service.run_profile_download", download):
            self.assertTrue(self.bridge.start(self.profile(1)))
            self.assertTrue(self.bridge.cancel())
            self.assertTrue(pump(lambda: bool(cancelled)))
            self.assertFalse(self.bridge.is_busy)
            release.set()
            self.runtime.run_sync(asyncio.sleep(0), timeout=2)
            QCoreApplication.processEvents()
            self.assertEqual(cancelled, [1])
            self.assertEqual(calls, [])
            self.assertEqual(writes, [])

    def test_missing_profile_folder_is_rebuilt_and_download_starts(self):
        done, calls = [], []
        missing = self.root / 'deleted'
        self.bridge.finished_signal.connect(done.append)
        async def download(prof, **kwargs):
            calls.append(prof)
            return DownloadSummary()
        with patch('third_party.cherry_dl.download_service.run_profile_download', download):
            self.assertTrue(self.bridge.start(dict(profile(1), folder_path=str(missing))))
            self.assertTrue(pump(lambda: bool(done)))
        self.assertTrue(missing.is_dir())
        self.assertEqual(len(calls), 1)

    def test_missing_profile_without_parent_never_creates_tree_or_enters_engine(self):
        failed, calls, writes = [], [], []
        missing = self.root / 'no_parent' / 'unavailable'
        self.bridge.failed_signal.connect(failed.append)
        self.bridge.catalog_write_possible.connect(lambda: writes.append(True))
        async def download(prof, **kwargs):
            calls.append(prof)
            return DownloadSummary()
        with patch('third_party.cherry_dl.download_service.run_profile_download', download):
            self.assertTrue(self.bridge.start(dict(profile(1), folder_path=str(missing))))
            self.assertTrue(pump(lambda: bool(failed)))
            self.assertIn('cherry_profile_parent_missing', failed[0])
            self.assertFalse(missing.exists())
            self.assertEqual(calls, [])
            self.assertEqual(writes, [])

    def test_pending_native_maintenance_prevents_engine_start(self):
        import hashlib
        from uuid import uuid4
        from modules.cherry.logic.profile_scope import profile_scope
        prof = self.profile(1)
        self.service.submit('prepare_maintenance_operations').result(5)
        with profile_scope(prof['folder_path']) as ticket:
            identity = list(ticket.identity)
            self.service.submit('maintenance_reserve', str(uuid4()), 1, ticket.canonical, identity,
                [dict(path=ticket.canonical, root_identity=identity)],
                hashlib.sha256(b'').hexdigest(), 0).result(5)
        calls, failed = [], []
        self.bridge.failed_signal.connect(failed.append)
        async def download(prof, **kwargs):
            calls.append(prof)
            return DownloadSummary()
        with patch('third_party.cherry_dl.download_service.run_profile_download', download):
            self.assertTrue(self.bridge.start(prof))
            self.assertTrue(pump(lambda: bool(failed)))
            self.assertIn('cherry_profile_recovery_required', failed[0])
            self.assertEqual(calls, [])


if __name__ == "__main__":
    app = QCoreApplication([])
    unittest.main()
