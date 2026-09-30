"""Recovery con kernel Windows y muerte de proceso real, sólo recursos temporales."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.curation_export_lock import export_guard, GUARD, MAGIC
from core.curation_recovery import inspect_recovery, quarantine_recovery, LOCK, PREFIX
from core.curation_sidecar import export_curation, NAME
import curation_snapshot_selftest as helpers

ROOT = Path(__file__).resolve().parents[1]


class Recovery(helpers.Fixture):
    def setUp(self):
        super().setUp()
        self.folder = Path(self.temp.name) / 'portátil ñ'
        self.folder.mkdir()
        self.path = self.folder / 'image.png'
        self.path.write_bytes(b'unchanged image')
        self.register(self.path.as_posix())
        export_curation(self.service.db_path, self.folder)
        self.published = (self.folder / NAME).read_bytes()
        self.image = self.path.read_bytes()
        self.central_path = self.path.as_posix()
        self.central = self.service.read('get_curation_restore_state', self.central_path)

    def stale(self, *, temporary=True, journals=False):
        path = self.folder / (PREFIX + 'aaaaaaaa.tmp')
        path.write_bytes(b'owned interrupted temporary')
        info = path.stat()
        manifest = dict(owner='panopticon.curation.export', version=1, token='a'*32,
            temporary=path.name, dev=info.st_dev, ino=info.st_ino)
        (self.folder / LOCK).write_text(json.dumps(manifest), encoding='utf-8')
        if journals:
            for suffix in ('-journal', '-wal', '-shm'):
                Path(str(path) + suffix).write_bytes(suffix.encode())
        if not temporary:
            path.unlink()
        return path

    def recover(self):
        snapshot = inspect_recovery(self.folder)
        return quarantine_recovery(self.folder, expected_fingerprint=snapshot['fingerprint'])

    def assert_intact(self):
        self.assertEqual((self.folder / NAME).read_bytes(), self.published)
        self.assertEqual(self.path.read_bytes(), self.image)
        self.assertEqual(self.service.read('count_files'), 1)
        self.assertEqual(self.service.read('get_curation_restore_state', self.central_path), self.central)

    def test_success_retains_valid_guard_and_leaves_no_lock_or_temporary(self):
        self.assertEqual((self.folder / GUARD).read_bytes(), MAGIC)
        self.assertFalse((self.folder / LOCK).exists())
        self.assertEqual(inspect_recovery(self.folder)['status'], 'clean')
        self.assertEqual(list(self.folder.glob(PREFIX + '*.tmp')), [])

    def test_inspection_and_quarantine_preserve_bytes_target_and_unclaimed_files(self):
        owned = self.stale(journals=True)
        unrelated = self.folder / (PREFIX + 'unclaimed.tmp')
        unrelated.write_bytes(b'unclaimed bytes')
        cherry = self.folder / 'catalog.db'
        cherry.write_bytes(b'foreign catalog')
        before = {path.name: path.read_bytes() for path in self.folder.iterdir() if path.is_file()}
        snapshot = inspect_recovery(self.folder)
        self.assertEqual({path.name: path.read_bytes() for path in self.folder.iterdir() if path.is_file()}, before)
        self.assertEqual(snapshot['status'], 'recoverable')
        result = quarantine_recovery(self.folder, expected_fingerprint=snapshot['fingerprint'])
        target = Path(result['folder'])
        self.assertEqual(result['files'][-1], LOCK)
        self.assertEqual(len(result['files']), 5)
        for name in result['files']:
            self.assertEqual((target / name).read_bytes(), before[name])
            self.assertFalse((self.folder / name).exists())
        self.assertEqual(json.loads((target / 'recovery.json').read_text())['fingerprint'], snapshot['fingerprint'])
        self.assertEqual(unrelated.read_bytes(), before[unrelated.name])
        self.assertEqual(cherry.read_bytes(), before[cherry.name])
        self.assert_intact()
        export_curation(self.service.db_path, self.folder)
        self.assertEqual(inspect_recovery(self.folder)['status'], 'clean')

    def test_lock_after_completed_publication_can_be_quarantined_without_touching_target(self):
        self.stale(temporary=False)
        self.assertEqual(self.recover()['files'], [LOCK])
        self.assert_intact()

    def test_empty_legacy_foreign_and_malformed_locks_are_never_removed(self):
        for value in (b'', b'held', b'{}', b'foreign', b'x'*4097):
            (self.folder / LOCK).write_bytes(value)
            with self.assertRaisesRegex(ValueError, 'unverifiable_export_lock'):
                inspect_recovery(self.folder)
            self.assertEqual((self.folder / LOCK).read_bytes(), value)
        self.assert_intact()

    def test_manifest_cannot_select_paths_outside_owned_temporary_namespace(self):
        self.stale()
        path = self.folder / LOCK
        original = json.loads(path.read_bytes())
        for name in ('../image.png', 'catalog.db', 'C:/image.png', '..\\image.png', PREFIX + 'x.tmp/child'):
            path.write_text(json.dumps(dict(original, temporary=name)), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'unverifiable_export_lock'):
                inspect_recovery(self.folder)
        self.assert_intact()

    def test_replaced_temporary_identity_is_not_claimed(self):
        path = self.stale()
        path.rename(self.folder / 'old-temporary')
        path.write_bytes(b'foreign replacement')
        with self.assertRaisesRegex(ValueError, 'recovery_identity_changed'):
            inspect_recovery(self.folder)
        self.assertEqual(path.read_bytes(), b'foreign replacement')
        self.assertTrue((self.folder / LOCK).exists())

    def test_changed_snapshot_requires_new_explicit_approval(self):
        path = self.stale()
        snapshot = inspect_recovery(self.folder)
        path.write_bytes(b'changed after inspection')
        with self.assertRaisesRegex(ValueError, 'recovery_changed'):
            quarantine_recovery(self.folder, expected_fingerprint=snapshot['fingerprint'])
        self.assertEqual(list(self.folder.glob('.panopticon.curation.recovery-*')), [])
        self.assertTrue((self.folder / LOCK).exists())

    def test_hardlinked_and_symlinked_resources_are_rejected(self):
        path = self.stale()
        alias = self.folder / 'temporary-alias'
        os.link(path, alias)
        with self.assertRaisesRegex(ValueError, 'unsafe_recovery_resource'):
            inspect_recovery(self.folder)
        alias.unlink()
        lock = self.folder / LOCK
        with patch.object(Path, 'lstat') as mock:
            import stat
            mock.return_value.st_mode = stat.S_IFLNK
            mock.return_value.st_nlink = 1
            with self.assertRaisesRegex(ValueError, 'unsafe_recovery_resource'):
                inspect_recovery(self.folder)
        self.assertTrue(lock.exists())

    def test_foreign_guard_is_not_overwritten(self):
        (self.folder / GUARD).write_bytes(b'foreign guard bytes')
        with self.assertRaisesRegex(ValueError, 'invalid_export_guard'):
            export_curation(self.service.db_path, self.folder)
        self.assertEqual((self.folder / GUARD).read_bytes(), b'foreign guard bytes')
        self.assert_intact()

    def test_missing_guard_does_not_invent_ownership_of_stale_lock(self):
        self.stale()
        (self.folder / GUARD).unlink()
        with self.assertRaisesRegex(ValueError, 'unverifiable_export_lock'):
            inspect_recovery(self.folder)
        self.assertFalse((self.folder / GUARD).exists())
        self.assertTrue((self.folder / LOCK).exists())

    def test_same_volume_folder_movement_keeps_recovery_portable(self):
        self.stale()
        moved = Path(self.temp.name) / 'moved ñ'
        self.folder.rename(moved)
        self.folder = moved
        self.path = moved / self.path.name
        self.assertEqual(self.recover()['status'], 'quarantined')
        self.assert_intact()

    def test_partial_quarantine_keeps_plan_bytes_and_lock_for_new_review(self):
        path = self.stale(journals=True)
        original = Path.rename
        def fail(source, target):
            if source.name.endswith('-journal'):
                raise OSError('forced_move_failure')
            return original(source, target)
        with patch.object(Path, 'rename', fail):
            with self.assertRaisesRegex(RuntimeError, 'recovery_incomplete:'):
                self.recover()
        quarantine = list(self.folder.glob('.panopticon.curation.recovery-*'))
        self.assertEqual(len(quarantine), 1)
        self.assertTrue((quarantine[0] / 'recovery.json').exists())
        self.assertEqual((quarantine[0] / path.name).read_bytes(), b'owned interrupted temporary')
        self.assertTrue((self.folder / LOCK).exists())
        self.assertEqual(self.recover()['status'], 'quarantined')
        self.assert_intact()

    def test_live_native_process_blocks_export_and_recovery_without_waiting(self):
        self.stale()
        ready = Path(self.temp.name) / 'ready'
        code = 'from core.curation_export_lock import export_guard; from pathlib import Path; import sys\nwith export_guard(sys.argv[1]):\n Path(sys.argv[2]).write_text("ready"); sys.stdin.buffer.read(1)'
        child = subprocess.Popen([sys.executable, '-c', code, str(self.folder), str(ready)],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(ready.exists())
            start = time.monotonic()
            for operation in (lambda: inspect_recovery(self.folder),
                              lambda: export_curation(self.service.db_path, self.folder)):
                with self.assertRaisesRegex(ValueError, 'sidecar_export_busy'):
                    operation()
            self.assertLess(time.monotonic() - start, .2)
            self.assertTrue((self.folder / LOCK).exists())
        finally:
            try:
                output, errors = child.communicate(b'x', timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
        self.assertEqual(child.returncode, 0, errors.decode(errors='replace'))
        self.assertEqual(self.recover()['status'], 'quarantined')

    def test_actual_export_process_death_leaves_verifiable_owned_resources(self):
        code = 'import os,sys; from unittest.mock import patch; from core.curation_sidecar import export_curation\nwith patch("core.curation_sidecar.os.replace", side_effect=lambda *a: os._exit(17)):\n export_curation(sys.argv[1],sys.argv[2])'
        result = subprocess.run([sys.executable, '-c', code, str(self.service.db_path), str(self.folder)],
            cwd=ROOT, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 17, result.stderr.decode(errors='replace'))
        snapshot = inspect_recovery(self.folder)
        self.assertEqual(snapshot['status'], 'recoverable')
        temporary = next(record['name'] for record in snapshot['files'] if record['name'].endswith('.tmp'))
        with closing(sqlite3.connect(f'file:{(self.folder / temporary).as_posix()}?mode=ro', uri=True)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='export_id'").fetchone()[0], snapshot['token'])
        self.assertEqual(self.recover()['status'], 'quarantined')
        self.assert_intact()
        export_curation(self.service.db_path, self.folder)
        self.assertFalse((self.folder / LOCK).exists())

    def test_actual_process_death_during_sql_copy_preserves_previous_publication(self):
        self.service.submit('edit_curation', self.path.as_posix(), add_tags=['current'], rating=4).result(3)
        self.central = self.service.read('get_curation_restore_state', self.path.as_posix())
        code = 'import os,sys; from unittest.mock import patch; from core.curation_sidecar import export_curation\nwith patch("core.curation_sidecar._tag", side_effect=lambda *a: os._exit(18)):\n export_curation(sys.argv[1],sys.argv[2])'
        result = subprocess.run([sys.executable, '-c', code, str(self.service.db_path), str(self.folder)],
            cwd=ROOT, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 18, result.stderr.decode(errors='replace'))
        snapshot = inspect_recovery(self.folder)
        self.assertEqual(snapshot['status'], 'recoverable')
        self.assertTrue(any(record['name'].endswith('-journal') for record in snapshot['files']))
        self.assertEqual(self.recover()['status'], 'quarantined')
        self.assert_intact()

    def test_cli_inspection_and_quarantine_require_reviewed_fingerprint(self):
        self.stale()
        command = [sys.executable, str(ROOT / 'scripts/curation_sidecar.py')]
        inspected = subprocess.run(command + ['inspect-recovery', '--folder', str(self.folder)], capture_output=True, timeout=10)
        self.assertEqual(inspected.returncode, 0, inspected.stderr.decode(errors='replace'))
        snapshot = json.loads(inspected.stdout)
        missing = subprocess.run(command + ['quarantine-recovery', '--folder', str(self.folder)], capture_output=True, timeout=10)
        self.assertEqual(missing.returncode, 2)
        result = subprocess.run(command + ['quarantine-recovery', '--folder', str(self.folder),
            '--expected-fingerprint', snapshot['fingerprint']], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(json.loads(result.stdout)['status'], 'quarantined')
        self.assert_intact()

    def test_invalid_approval_and_folder_do_not_create_resources(self):
        with self.assertRaisesRegex(ValueError, 'invalid_recovery_fingerprint'):
            quarantine_recovery(self.folder, expected_fingerprint='bad')
        with self.assertRaisesRegex(ValueError, 'invalid_export_folder'):
            inspect_recovery(self.folder / 'missing')
        self.assertEqual(list(self.folder.glob('.panopticon.curation.recovery-*')), [])

    def test_export_guard_errors_are_human_readable_in_both_languages(self):
        from types import SimpleNamespace
        from modules.librarian.module import LibrarianModule
        for language in ('en', 'es'):
            values = json.loads((ROOT / 'locales' / (language + '.json')).read_bytes())
            for reason in ('sidecar_export_busy', 'invalid_export_guard'):
                texts = []
                module = SimpleNamespace(_closed=False, _exporting='folder',
                    _update_export_controls=lambda: None,
                    lbl_export=SimpleNamespace(setText=texts.append),
                    tr=lambda key, default='': values.get(key, default))
                LibrarianModule._on_export_finished(module, None, None, ValueError(reason))
                self.assertEqual(len(texts), 1)
                self.assertNotIn(reason, texts[0])
                self.assertIn(values['lib.export_reason.' + reason], texts[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
