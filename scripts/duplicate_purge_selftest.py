"""Purga real sólo en fixtures Windows; CAS, handles, fallos y recibos intactos."""
from concurrent.futures import Future
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from core.duplicate_purge import approve, decision
from core.duplicate_policy import DuplicatePolicy
from modules.duplicate_finder.logic.pinned_file import PinnedFile
from modules.duplicate_finder.logic.recovery_purge import PurgePreview, RecoveryPurge
from modules.duplicate_finder.logic.recovery_storage import write_journal
from modules.duplicate_finder.logic.purge_worker import RecoveryPurgeWorker
from curation_snapshot_selftest import pump
import duplicate_recovery_selftest as prior


class Purge(prior.Recovery):
    def prepare(self):
        self.raw, self.keeper = self.pair()
        self.receipt = self.root/'catalog.db'
        self.receipt.write_bytes(b'Cherry hashes/URLs/alias receipts')
        self.retire(self.raw)
        review = self.audit()
        move = review['moves'][0]
        copy = next(c for c in review['copies'] if c['copy_id'] == move['copy_id'])
        self.destination = Path(move['destination'])
        self.arguments = dict(journal=str(self.journal), copy_id=copy['copy_id'],
            journal_fingerprint=review['journal_fingerprint'], expected_fingerprint=copy['fingerprint'])
        self.original = self.journal.read_bytes()
        self.before = self.service.read('get_duplicate_recovery_state', [c['copy_id'] for c in review['copies']])
        self.preview = self.preview_now()
        self.assertTrue(self.preview['complete'], self.preview)
        return self.preview

    def preview_now(self):
        return PurgePreview(self.service, **self.arguments).run()

    def purge(self, event=None):
        return RecoveryPurge(self.service, **self.arguments,
            preview_fingerprint=self.preview['preview_fingerprint']).run(event)

    def unchanged(self):
        self.assertEqual(self.journal.read_bytes(), self.original)
        self.assertEqual(self.receipt.read_bytes(), b'Cherry hashes/URLs/alias receipts')
        self.assertEqual(self.keeper.read_bytes(), b'same bytes')
        self.assertEqual(self.service.read('get_duplicate_recovery_state', [c['copy_id'] for c in self.before]), self.before)

    def test_preview_reads_only_and_purge_keeps_all_native_states_receipts_and_journals(self):
        self.prepare()
        with patch.object(self.service, 'submit', side_effect=AssertionError('preview wrote')):
            result = self.preview_now()
        self.assertEqual(result['preview_fingerprint'], self.preview['preview_fingerprint'])
        self.assertTrue(self.destination.exists())
        result = self.purge()
        self.assertTrue(result['complete'], result)
        self.assertTrue(result['approved']); self.assertTrue(result['deleted']); self.assertTrue(result['delete_marked'])
        self.assertFalse(self.destination.exists()); self.assertFalse(self.raw.exists())
        self.unchanged()
        journal = json.loads(Path(result['operation_journal']).read_bytes())
        self.assertEqual(journal['phase'], 'complete'); self.assertTrue(journal['deleted'])
        again = self.purge(); self.assertFalse(again['complete']); self.assertFalse(again['deleted'])
        self.unchanged()

    def test_windows_pinned_handles_block_writes_renames_deletes_until_close(self):
        self.prepare()
        identity = self.preview['evidence']['keeper_identity']
        with PinnedFile(self.keeper) as kept, PinnedFile(self.destination, delete=True) as victim:
            kept.verify(self.value['digest'], identity)
            for path in (self.keeper, self.destination):
                with self.assertRaises(OSError):
                    path.write_bytes(b'changed')
                with self.assertRaises(OSError):
                    path.rename(path.with_name('renamed.png'))
                with self.assertRaises(OSError):
                    path.unlink()
            with self.assertRaises(OSError):
                self.destination.open('rb')
            victim.verify(self.value['digest'], self.preview['evidence']['identity'])
        self.assertTrue(self.destination.exists()); self.unchanged()

    def test_changed_bytes_or_identical_replacement_requires_new_review(self):
        self.prepare()
        self.destination.write_bytes(b'changed bytes')
        result = self.purge(); self.assertFalse(result['deleted']); self.assertFalse(result['approved'])
        self.assertEqual(self.destination.read_bytes(), b'changed bytes')

    def test_hardlinked_victim_is_rejected_and_survives(self):
        self.prepare()
        extra = self.destination.parent/'hardlink.png'
        os.link(self.destination, extra)
        result = self.purge(); self.assertFalse(result['deleted']); self.assertTrue(extra.exists())
        self.unchanged()

    def test_late_caption_or_sidecar_rejects_before_effects(self):
        self.prepare()
        self.destination.with_suffix('.txt').write_text('caption')
        result = self.purge(); self.assertFalse(result['deleted']); self.assertTrue(self.destination.exists())
        self.unchanged()

    def test_new_source_copy_or_new_watched_scope_rejects_preview(self):
        self.prepare()
        self.raw.write_bytes(b'new original')
        result = self.purge(); self.assertFalse(result['deleted'])
        self.assertEqual(self.raw.read_bytes(), b'new original')
        self.raw.unlink()
        self.service.submit('add_watched_folder', self.recovery.as_posix()).result(3)
        result = self.purge(); self.assertFalse(result['deleted']); self.assertTrue(self.destination.exists())

    def test_journal_write_before_approval_failure_never_deletes(self):
        self.prepare()
        with patch('modules.duplicate_finder.logic.recovery_purge.write_journal', side_effect=OSError('fixture')):
            result = self.purge()
        self.assertFalse(result['approved']); self.assertFalse(result['deleted']); self.assertTrue(self.destination.exists())
        self.unchanged()

    def test_owner_cas_rejects_late_curation_change_and_preserves_bytes(self):
        self.prepare()
        submit = self.service.submit
        def race(operation, *args, **kwargs):
            submit('edit_curation', self.keeper.as_posix(), add_tags=['late']).result(3)
            return submit(operation, *args, **kwargs)
        with patch.object(self.service, 'submit', side_effect=race):
            result = self.purge()
        self.assertFalse(result['approved']); self.assertFalse(result['deleted']); self.assertTrue(self.destination.exists())

    def test_false_or_exception_future_cannot_delete_even_when_cancelled(self):
        self.prepare()
        for failure in (False, ValueError('owner refused')):
            event = threading.Event()
            future = Future()
            if failure is False: future.set_result(False)
            else: future.set_exception(failure)
            def submit(*args, **kwargs): event.set(); return future
            with patch.object(self.service, 'submit', side_effect=submit): result = self.purge(event)
            self.assertFalse(result['deleted']); self.assertFalse(result['cancelled']); self.assertIsNotNone(result['error'])
        self.unchanged()

    def test_cancel_before_admission_and_while_owner_pending_preserves_victim(self):
        self.prepare()
        event = threading.Event(); event.set()
        result = self.purge(event); self.assertTrue(result['cancelled']); self.assertFalse(result['deleted'])
        event.clear()
        submit = self.service.submit
        def cancelled(*args, **kwargs):
            future = submit(*args, **kwargs); event.set(); return future
        with patch.object(self.service, 'submit', side_effect=cancelled): result = self.purge(event)
        self.assertTrue(result['approved']); self.assertTrue(result['cancelled']); self.assertFalse(result['deleted'])
        self.assertTrue(self.destination.exists()); self.unchanged()

    def test_failed_delete_and_final_journal_failure_report_true_effects(self):
        self.prepare()
        with patch.object(PinnedFile, 'mark_delete', side_effect=OSError('fixture locked')):
            result = self.purge()
        self.assertTrue(result['approved']); self.assertFalse(result['deleted']); self.assertTrue(self.destination.exists())
        def fail_final(path, value):
            if value['deleted']: raise OSError('final journal')
            return write_journal(path, value)
        with patch('modules.duplicate_finder.logic.recovery_purge.write_journal', side_effect=fail_final): result = self.purge()
        self.assertTrue(result['deleted']); self.assertFalse(result['complete']); self.assertIsNotNone(result['error'])
        self.assertFalse(self.destination.exists()); self.unchanged()

    def test_after_approval_journal_failure_or_late_caption_still_preserves_bytes(self):
        self.prepare()
        def fail_approved(path, value):
            if value['approved']: raise OSError('approved journal failed')
            return write_journal(path, value)
        with patch('modules.duplicate_finder.logic.recovery_purge.write_journal', side_effect=fail_approved):
            result = self.purge()
        self.assertTrue(result['approved']); self.assertFalse(result['deleted']); self.assertTrue(self.destination.exists())
        submit = self.service.submit
        def caption(*args, **kwargs):
            future = submit(*args, **kwargs); future.result(3)
            self.keeper.with_suffix('.txt').write_text('late keeper documentation')
            return future
        with patch.object(self.service, 'submit', side_effect=caption): result = self.purge()
        self.assertTrue(result['approved']); self.assertFalse(result['deleted']); self.assertTrue(self.destination.exists())
        self.unchanged()

    def test_stale_preview_cannot_authorize_new_scope_even_if_target_is_still_outside(self):
        self.prepare()
        elsewhere = Path(self.temp.name)/'another'; elsewhere.mkdir()
        self.service.submit('add_watched_folder', elsewhere.as_posix()).result(3)
        result = self.purge()
        self.assertEqual(result['error'], 'duplicate_purge_preview_changed')
        self.assertFalse(result['approved']); self.assertTrue(self.destination.exists())

    def test_owner_approval_has_no_dml_and_full_state_and_scope_cas(self):
        self.prepare()
        e = self.preview['evidence']
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            changes = conn.total_changes
            trace = []; conn.set_trace_callback(trace.append)
            self.assertTrue(approve(conn, e['approved'], digest=e['digest'], policy=e['policy'], expected_scope=e['scope'], normalize=lambda p:p))
            self.assertEqual(conn.total_changes, changes)
            self.assertFalse(any(s.lstrip().split()[0] in ('UPDATE', 'DELETE', 'INSERT', 'CREATE') for s in trace))
            invalid = deepcopy(e['approved']); invalid[0]['fingerprint'] = '0'*64
            with self.assertRaisesRegex(ValueError, 'identity_changed'):
                approve(conn, invalid, digest=e['digest'], policy=e['policy'], expected_scope=e['scope'], normalize=lambda p:p)
            with self.assertRaises(ValueError):
                approve(conn, e['approved'], digest=e['digest'], policy=e['policy'], expected_scope=['changed'], normalize=lambda p:p)
            conn.rollback()
        self.unchanged()

    def test_zero_weights_still_preserve_curated_and_conflicts_never_discard(self):
        self.prepare()
        e = self.preview['evidence']; weights = dict.fromkeys(vars(DuplicatePolicy()), 0)
        decision(e['before'], e['digest'], weights)
        records = deepcopy(e['before']); records[0]['state']['tags'] = [dict(name='unique', source='manual', category=None)]
        with self.assertRaisesRegex(ValueError, 'information_requires_review'):
            decision(records, e['digest'], weights)

    def test_preview_and_purge_workers_have_one_terminal_signal_and_stop(self):
        self.prepare()
        for operation in ('preview', 'purge'):
            args = dict(self.arguments)
            if operation == 'purge': args['preview_fingerprint'] = self.preview['preview_fingerprint']
            worker = RecoveryPurgeWorker(self.service, operation=operation, token=77, **args)
            results = []; worker.finished_signal.connect(results.append); worker.stop(); worker.start()
            try:
                pump(lambda: len(results) == 1)
                self.assertTrue(results[0]['cancelled']); self.assertEqual(results[0]['token'], 77)
                self.assertEqual(results[0]['operation'], operation)
            finally: worker.wait(5000)
        self.assertTrue(self.destination.exists()); self.unchanged()


if __name__ == '__main__':
    app = QApplication.instance() or QApplication([])
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('Purge.'+name, sys.modules[__name__])
        for name in Purge.__dict__ if name.startswith('test_'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
