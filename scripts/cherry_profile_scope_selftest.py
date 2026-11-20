"""Windows fixtures for the shared profile exclusion contract."""
import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from modules.cherry.logic.profile_scope import profile_scope, async_profile_scope
from core.paths import ProjectPaths


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        override = patch.object(ProjectPaths, "root", return_value=self.root)
        override.start(); self.addCleanup(override.stop)
        self.profile = self.root / 'Artist'
        self.profile.mkdir()

    def test_same_profile_case_alias_rejected_other_profile_allowed(self):
        other = self.root / 'Other'
        other.mkdir()
        with profile_scope(self.profile) as ticket:
            self.assertTrue(ticket.assert_current())
            with self.assertRaisesRegex(ValueError, 'cherry_profile_busy'):
                with profile_scope(str(self.profile).upper()): pass
            with profile_scope(other) as second:
                self.assertNotEqual(ticket.identity, second.identity)
        with self.assertRaisesRegex(ValueError, 'scope_closed'): ticket.assert_current()
        with profile_scope(self.profile): pass

    def test_thread_cannot_enter_and_parent_cannot_move(self):
        errors = []
        def competitor():
            try:
                with profile_scope(self.profile): errors.append('unexpected admission')
            except ValueError as error: errors.append(str(error))
        with profile_scope(self.profile):
            worker = threading.Thread(target=competitor)
            worker.start(); worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, ['cherry_profile_busy'])
            with self.assertRaises(OSError): self.profile.rename(self.root / 'Moved')

    def test_cross_process_excluded_and_released_after_failure(self):
        script = ('from modules.cherry.logic.profile_scope import profile_scope; import sys; '
                  'from core.paths import ProjectPaths; from pathlib import Path; '
                  'ProjectPaths.root=staticmethod(lambda:Path(sys.argv[2])); '
                  'ctx=profile_scope(sys.argv[1]); ctx.__enter__()')
        with profile_scope(self.profile):
            result = subprocess.run([sys.executable, '-c', script, str(self.profile), str(self.root)],
                cwd=Path(__file__).resolve().parent, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('cherry_profile_busy', result.stderr)
        try:
            with profile_scope(self.profile): raise RuntimeError('fixture')
        except RuntimeError: pass
        with profile_scope(self.profile): pass

    def test_async_cancellation_releases(self):
        async def fixture():
            entered = asyncio.Event()
            async def worker():
                async with async_profile_scope(self.profile):
                    entered.set()
                    await asyncio.Event().wait()
            task = asyncio.create_task(worker())
            await entered.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            with profile_scope(self.profile): pass
        asyncio.run(fixture())

    def test_fusion_holds_both_scopes_through_cancellation(self):
        from modules.cherry.logic import merge
        other = self.root / 'Other'
        other.mkdir()
        pair = dict(id_a=1, id_b=2, name_a='Artist', name_b='Other',
                    folder_a=str(self.profile), folder_b=str(other),
                    created_at_a='2026-01-01', created_at_b='2026-02-01')
        async def fixture():
            entered, release = asyncio.Event(), asyncio.Event()
            async def work(*args, **kwargs):
                entered.set()
                await release.wait()
                return merge.MergeResult(plan=merge.plan_pair(pair))
            with patch.object(merge, '_merge_pair', work):
                from types import SimpleNamespace
                service = SimpleNamespace(read=lambda *args: {'operations': []})
                task = asyncio.create_task(merge.merge_pair(pair, data_service=service))
                await asyncio.wait_for(entered.wait(), 3)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                for folder in (self.profile, other):
                    with self.assertRaisesRegex(ValueError, 'cherry_profile_busy'):
                        with profile_scope(folder): pass
                release.set()
                with self.assertRaises(asyncio.CancelledError): await task
                for folder in (self.profile, other):
                    with profile_scope(folder): pass
        asyncio.run(fixture())

    def test_pending_native_scope_blocks_other_producers_after_lock_is_released(self):
        import hashlib
        from uuid import uuid4
        from core.library_service import LibraryService
        from modules.cherry.logic.profile_scope import require_available_profile
        service = LibraryService(self.root / 'native.db')
        self.addCleanup(service.close)
        service.submit('prepare_maintenance_operations').result(5)
        operation = str(uuid4())
        with profile_scope(self.profile) as ticket:
            identity = list(ticket.identity)
            service.submit('maintenance_reserve', operation, 1, ticket.canonical, identity,
                [dict(path=ticket.canonical, root_identity=identity)],
                hashlib.sha256(b'').hexdigest(), 0).result(5)
        with profile_scope(self.profile) as ticket:
            with self.assertRaisesRegex(ValueError, 'cherry_profile_recovery_required'):
                require_available_profile(service, ticket)
            service.submit('maintenance_bind_scope_owner', operation,
                dict(owner_nonce=service.maintenance_owner_nonce, kind='directory', handles_held=True,
                     canonical=ticket.canonical, physical=list(ticket.identity))).result(5)
            service.submit('maintenance_cancel_ready', operation, list(ticket.identity)).result(5)
            require_available_profile(service, ticket)


if __name__ == '__main__': unittest.main()
