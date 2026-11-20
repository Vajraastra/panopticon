"""Private Windows fixtures for the temporary Native maintenance barrier."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from core.library_service import LibraryService
from modules.cherry.logic.archive_batch import (MaintenanceBlocked,
    process_profiles, process_unindexed_project)


class MaintenanceGuard(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / 'Artist'; self.folder.mkdir()
        self.other = self.root / 'Other'; self.other.mkdir()
        self.source = self.folder / 'Artist_00002.png'
        self.source.write_bytes(b'private existing image')
        self.service = LibraryService(self.root / 'private.db')

    async def asyncTearDown(self):
        self.service.close(); self.temp.cleanup()

    def register(self, offline=False):
        self.service.submit('register_files_minimal', [dict(path=str(self.source),
            filename=self.source.name, size=self.source.stat().st_size, created=1)]).result(5)
        key = self.service.read('get_file_id', str(self.source))
        if offline:
            self.service.submit('archive_copy', key, expected_fingerprint=self.state(key)['fingerprint']).result(5)
        return key

    def state(self, key):
        return self.service.read('get_copy_version_state', key)

    async def test_active_and_offline_history_block_before_engine(self):
        key = self.register()
        for offline in (False, True):
            if offline:
                self.service.submit('archive_copy', key, expected_fingerprint=self.state(key)['fingerprint']).result(5)
            before = self.state(key); names = sorted(p.name for p in self.folder.iterdir())
            with patch('third_party.cherry_dl.archives.process_project', new=AsyncMock()) as engine:
                with self.assertRaisesRegex(MaintenanceBlocked, 'indexed'):
                    await process_unindexed_project(self.folder, 'Artist', data_service=self.service)
                engine.assert_not_awaited()
            self.assertEqual(self.state(key), before)
            self.assertEqual(sorted(p.name for p in self.folder.iterdir()), names)
            self.assertEqual(self.source.read_bytes(), b'private existing image')

    async def test_batch_checks_every_profile_before_touching_first(self):
        self.register(offline=True)
        profiles = [dict(id=1, display_name='Free', folder_path=str(self.other)),
                    dict(id=2, display_name='History', folder_path=str(self.folder))]
        with patch('third_party.cherry_dl.archives.process_project', new=AsyncMock()) as engine:
            with self.assertRaisesRegex(MaintenanceBlocked, 'indexed'):
                await process_profiles(profiles, data_service=self.service)
            engine.assert_not_awaited()
        self.assertEqual(list(self.other.iterdir()), [])

    async def test_unindexed_folder_can_reach_engine(self):
        sentinel = object()
        with patch('third_party.cherry_dl.archives.process_project', new=AsyncMock(return_value=sentinel)) as engine:
            result = await process_unindexed_project(self.folder, 'Artist', data_service=self.service)
        self.assertIs(result, sentinel)
        engine.assert_awaited_once_with(self.folder, 'Artist')

    async def test_missing_or_failed_service_blocks_before_engine(self):
        class Broken:
            def read(self, *args):
                raise RuntimeError('private transport error')
        with patch('third_party.cherry_dl.archives.process_project', new=AsyncMock()) as engine:
            for service in (None, Broken()):
                with self.assertRaisesRegex(MaintenanceBlocked, 'review_required'):
                    await process_unindexed_project(self.folder, 'Artist', data_service=service)
            engine.assert_not_awaited()

    async def test_scope_handles_case_prefixes_and_offline_nested_history(self):
        nested = self.folder / 'discarded'; nested.mkdir()
        self.source = nested / self.source.name; self.source.write_bytes(b'historical')
        self.register(offline=True)
        self.assertTrue(self.service.read('has_maintenance_history', [str(self.folder).replace('\\', '/').upper()]))
        sibling = str(self.folder).replace('\\', '/') + '-elsewhere'
        self.assertFalse(self.service.read('has_maintenance_history', [sibling]))
        with self.assertRaisesRegex(Exception, 'invalid_maintenance_folders'):
            self.service.read('has_maintenance_history', [])

    async def test_watched_scopes_block_parent_child_but_not_neighbor(self):
        self.service.submit('add_watched_folder', str(self.folder)).result(5)
        for target in (self.root, self.folder, self.folder / 'empty'):
            self.assertTrue(self.service.read('has_maintenance_history', [target.as_posix()]))
        self.assertFalse(self.service.read('has_maintenance_history', [self.folder.as_posix() + '-neighbor']))
        with patch('third_party.cherry_dl.archives.process_project', new=AsyncMock()) as engine:
            with self.assertRaisesRegex(MaintenanceBlocked, 'indexed'):
                await process_unindexed_project(self.folder, 'Artist', data_service=self.service)
            engine.assert_not_awaited()

    async def test_cli_apply_requires_review_but_preview_still_runs(self):
        from scripts import cherry_archive_batch as cli
        profile = dict(id=1, display_name='Artist', folder_path=str(self.folder))
        args = argparse.Namespace(profile=None, apply=True, force_audit=False)
        target = self.root / 'report.json'
        plan = dict(reviewed=1, hashed=0, files=[], duplicates=[], errors=[])
        with patch('third_party.cherry_dl.index.list_profiles', new=AsyncMock(return_value=[profile])), \
             patch('third_party.cherry_dl.index.get_profile', new=AsyncMock(return_value=profile)), \
             patch('third_party.cherry_dl.archives.process_project', new=AsyncMock()) as engine, \
             patch('third_party.cherry_dl.project_maintenance.plan_project', new=AsyncMock(return_value=plan)) as preview:
            applied = await cli.run(args, target)
            self.assertTrue(applied['blocked']); engine.assert_not_awaited(); preview.assert_not_awaited()
            self.assertTrue(json.loads(target.read_bytes())['blocked'])
            args.apply = False
            report = await cli.run(args, target)
            self.assertNotIn('blocked', report); self.assertEqual(len(report['projects']), 1)
            preview.assert_awaited_once(); engine.assert_not_awaited()


class MaintenanceGuardUI(unittest.TestCase):
    def test_individual_and_batch_errors_are_localized(self):
        from types import SimpleNamespace
        from PySide6.QtWidgets import QApplication
        from modules.cherry.ui.view import CherryView
        app = QApplication.instance() or QApplication([])
        root = Path(__file__).resolve().parents[1]
        for language in ('en', 'es'):
            translations = json.loads((root / 'locales' / (language + '.json')).read_bytes())
            messages = []
            view = SimpleNamespace(context={}, _maintenance_progress=SimpleNamespace(emit=lambda *_: None),
                _archive_batch_profile=SimpleNamespace(emit=lambda *_: None),
                _archives_finished=SimpleNamespace(emit=lambda *args: messages.append(args)),
                tr=lambda key, default: translations.get(key, default))
            view._maintenance_blocked_message = lambda reason: CherryView._maintenance_blocked_message(view, reason)
            for method, args in [(CherryView._archives_async, ('C:/private', 'Artist')),
                                 (CherryView._archives_batch_async, ([dict(folder_path='C:/private')],))]:
                asyncio.run(method(view, *args))
            expected = translations['tool.cherry.maintenance_review_required']
            self.assertEqual([row[2] for row in messages], [expected, expected])
            self.assertEqual([row[0] for row in messages], ['', ''])
            self.assertEqual(view._maintenance_blocked_message('indexed'), translations['tool.cherry.maintenance_native_blocked'])
        app.processEvents()


if __name__ == '__main__':
    unittest.main(verbosity=2)
