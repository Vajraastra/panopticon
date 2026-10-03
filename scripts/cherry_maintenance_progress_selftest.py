"""Windows, private fixtures: sparse writes, recovery and bounded GUI progress."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from third_party.cherry_dl import catalog, project_maintenance as maintenance
from third_party.cherry_dl.maintenance_progress import MaintenanceProgress, ProgressReporter
from modules.cherry.logic.archive_batch import process_profiles


@asynccontextmanager
async def traced(statements):
    original = catalog._db
    @asynccontextmanager
    async def connect(path):
        async with original(path) as db:
            await db.set_trace_callback(statements.append)
            yield db
    with patch.object(catalog, '_db', connect):
        yield


def mutations(statements):
    return [s for s in statements if s.lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE '))]


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        await catalog.init_catalog(self.folder)
        self.files = []
        for n in range(1, 5):
            data = f'private-image-{n}'.encode()
            name = f'Test_{n:05d}.png'
            (self.folder / name).write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            await catalog.add_file(self.folder, digest, name, f'https://example.test/{n}', len(data), n)
            self.files.append(dict(hash=digest, old=name, new=name, size=len(data), counter=n))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_repeat_no_dml_and_no_receipt_rewrite(self):
        state = self.folder / '.cherry-archives'
        state.mkdir()
        receipt = state / 'fixture.json'
        item = self.files[0]
        receipt.write_text(json.dumps(dict(state='completed', entries=[dict(hash=item['hash'], target=item['new'], counter=1)])), encoding='utf-8')
        await maintenance.maintain_project(self.folder, 'Test')
        before = (receipt.read_bytes(), receipt.stat().st_mtime_ns)
        statements = []
        async with traced(statements):
            result, plan = await maintenance.maintain_project(self.folder, 'Test')
        self.assertEqual(mutations(statements), [])
        self.assertEqual((receipt.read_bytes(), receipt.stat().st_mtime_ns), before)
        self.assertEqual((result.compacted, result.duplicates_removed, result.registered), (0, 0, 0))
        self.assertEqual(plan['status'], 'completed')

    async def test_sparse_update_preserves_source_and_date(self):
        await catalog.apply_project_maintenance(self.folder, self.files, {'legacy': 4}, 1)
        before = {r['hash']: r for r in await catalog.get_all_files(self.folder)}
        self.files[0]['new'] = 'New_00001.png'
        statements = []
        async with traced(statements):
            await catalog.apply_project_maintenance(self.folder, self.files, {'legacy': 4}, 1)
        self.assertEqual(len(mutations(statements)), 1)
        after = {r['hash']: r for r in await catalog.get_all_files(self.folder)}
        for item in self.files:
            h = item['hash']
            self.assertEqual(before[h]['url_source'], after[h]['url_source'])
            self.assertEqual(before[h]['date_added'], after[h]['date_added'])
        self.assertEqual(after[self.files[0]['hash']]['filename'], 'New_00001.png')

    async def test_conflict_with_unchanged_keeper_preserves_history(self):
        await catalog.add_file(self.folder, 'historic', self.files[0]['new'], 'https://example.test/history', 999, 1)
        await catalog.apply_project_maintenance(self.folder, self.files, {'legacy': 4}, 1)
        rows = {r['hash']: r for r in await catalog.get_all_files(self.folder)}
        self.assertEqual(rows['historic']['filename'], '_purged_historic')
        self.assertEqual(rows['historic']['url_source'], 'https://example.test/history')
        self.assertEqual(rows[self.files[0]['hash']]['filename'], self.files[0]['new'])

    async def test_swap_targets_and_new_file(self):
        self.files[0]['new'], self.files[1]['new'] = self.files[1]['new'], self.files[0]['new']
        self.files.append(dict(hash='new', new='Test_00005.png', size=10, counter=5))
        await catalog.apply_project_maintenance(self.folder, self.files, {'legacy': 5}, 1)
        rows = {r['hash']: r for r in await catalog.get_all_files(self.folder)}
        for item in self.files:
            self.assertEqual(rows[item['hash']]['filename'], item['new'])
        self.assertEqual(await catalog.get_meta_int(self.folder, 'counter'), 5)

    async def test_catalog_cancel_rolls_back(self):
        before = await catalog.get_all_files(self.folder)
        self.files[0]['new'] = 'Different_00001.png'
        def cancel(event):
            if event.phase == 'catalog_write' and event.done:
                raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await catalog.apply_project_maintenance(self.folder, self.files, {'legacy': 4}, 1, progress=ProgressReporter(cancel))
        self.assertEqual(await catalog.get_all_files(self.folder), before)

    async def test_batch_order_and_phases(self):
        events, order = [], []
        result = await process_profiles([
            dict(id=2, display_name='First', folder_path=str(self.folder)),
            dict(id=1, display_name='Second', folder_path=str(self.folder)),
        ], on_profile=lambda *args: order.append(args), on_progress=events.append)
        self.assertFalse(result.errors)
        self.assertEqual([r[2] for r in order], [2, 1])
        phases = {e.phase for e in events}
        self.assertTrue({'inventory', 'hash', 'rename', 'catalog_write', 'verify', 'extract'} <= phases)
        self.assertTrue(any(e.phase == 'inventory' and e.total is None for e in events))
        self.assertTrue(any(e.phase == 'verify' and e.done == e.total == 4 for e in events))

    async def test_inventory_cancel_keeps_sources(self):
        before = sorted(p.name for p in self.folder.iterdir())
        def cancel(event):
            if event.phase == 'inventory':
                raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await maintenance.maintain_project(self.folder, 'Test', progress=ProgressReporter(cancel))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), before)

    async def test_repeat_benchmark_2000(self):
        # Deliberately test SQL cost independently of disk traversal and hashing.
        items = [dict(hash=f'benchmark-{n}', new=f'Bench_{n:05d}.png', size=10, counter=n) for n in range(2000)]
        await catalog.apply_project_maintenance(self.folder, items, {'legacy': 2000}, 1)
        statements = []
        started = time.perf_counter()
        async with traced(statements):
            await catalog.apply_project_maintenance(self.folder, items, {'legacy': 2000}, 1)
        elapsed = time.perf_counter() - started
        self.assertEqual(mutations(statements), [])
        self.assertLess(len(statements), 12)
        print(f'Unchanged 2000-row catalog: {elapsed:.4f}s, {len(statements)} statements, 0 DML')


class ProgressTests(unittest.TestCase):
    def test_rate_limit_and_boundaries(self):
        events = []
        with patch('third_party.cherry_dl.maintenance_progress.monotonic', return_value=1):
            report = ProgressReporter(events.append)
            for n in range(10001):
                report('hash', n, 10000)
            report('verify', 0, None)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[1].done, 10000)

    def test_gui_keeps_project_and_bounded_console(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from modules.cherry.module import CherryModule
        app = QApplication.instance() or QApplication([])
        module = CherryModule()
        module.on_load({})
        view = module.get_view()
        logs, phases = [], []
        view._panel.append_log = lambda text, *args: logs.append(text)
        view._panel.set_phase = phases.append
        view._on_active_profile_changed = lambda _: None
        try:
            view._on_archive_batch_profile(2, 8, 42, 'Project Ñ')
            for n in range(50):
                view._on_maintenance_progress(MaintenanceProgress('hash', n, 50, 'image.png'))
            self.assertIn('2/8', phases[-1])
            self.assertIn('Project Ñ', phases[-1])
            self.assertIn('49/50', phases[-1])
            self.assertIn('image.png', phases[-1])
            self.assertEqual(len(logs), 2)
        finally:
            module.on_unload()
            self.assertTrue(module.shutdown_ready())
            view.close()
            app.processEvents()


if __name__ == '__main__':
    unittest.main()
