"""Real Windows toy fixtures only; no Native service, SQL or library files."""
from contextlib import ExitStack
import ctypes
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import subprocess
import unittest
from unittest.mock import patch

import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
PROTOTYPE=ROOT/'.work'/'pinned-move-selftest'
from core.pinned_move import (DirectoryPins, MoveJournal, MovableFileContext,
                              PendingMove, api, local_move_path)


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def ack(evidence,operator):
    if operator.stream.closed: raise AssertionError('source handle closed before acknowledgement')
    return dict(confirmed=True,step_id=evidence['step_id'],private_fixture=True)


class Moves(unittest.TestCase):
    def setUp(self):
        work=PROTOTYPE/'fixtures'; work.mkdir(parents=True,exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=work); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name); assert self.root.resolve().is_relative_to(PROTOTYPE)
        self.source=self.root/'original'; self.stage=self.root/'stage'
        self.source.mkdir(); self.stage.mkdir()
        self.file=self.source/'image.png'; self.file.write_bytes(b'private unchanged image')
        self.digest=sha(self.file)

    def blocked(self,operation):
        with self.assertRaises(OSError): operation()

    def test_cycle_101_preserves_all_proofs_and_acks_before_close(self):
        paths=[]
        for number in range(101):
            path=self.source/f'file-{number:03d}.bin'
            path.write_bytes(f'private cycle bytes {number}'.encode()); paths.append(path)
        with ExitStack() as stack:
            directories=stack.enter_context(DirectoryPins())
            journal=stack.enter_context(MoveJournal(self.root/'cycle.jsonl'))
            pins=[stack.enter_context(MovableFileContext(path,sha(path),directories=directories)) for path in paths]
            before=[pin.current for pin in pins]
            def forbidden(*args,**kwargs): raise AssertionError('source mutation by path is forbidden')
            with patch('os.rename',forbidden),patch.object(Path,'rename',forbidden), \
                 patch.object(Path,'replace',forbidden),patch.object(Path,'unlink',forbidden):
                for number,pin in enumerate(pins):
                    result=pin.move_to(self.stage/f'entry-{number:03d}.bin',journal=journal,acknowledge=ack)
                    self.assertEqual(result['evidence']['after']['file_id'],before[number].file_id)
                for number,pin in enumerate(pins):
                    pin.move_to(self.source/f'file-{(number+1)%101:03d}.bin',journal=journal,acknowledge=ack)
                    self.assertEqual(pin.current.invariant(),before[number].invariant())
                    self.assertFalse(pin.stream.closed)
        records=[json.loads(line) for line in (self.root/'cycle.jsonl').read_bytes().splitlines()]
        self.assertEqual(sum(r['phase']=='move_acknowledged' for r in records),202)
        self.assertEqual(sum(r['phase']=='move_physical_complete' for r in records),202)

    def test_source_write_delete_open_rename_and_parent_rename_are_blocked(self):
        with MovableFileContext(self.file,self.digest) as pin,MoveJournal(self.root/'locks.jsonl') as journal:
            kernel=api()
            for desired in (0x40000000,0x10000):
                handle=kernel.CreateFileW(str(self.file),desired,7,None,3,0,None)
                self.assertEqual(handle,ctypes.c_void_p(-1).value)
                self.assertEqual(ctypes.get_last_error(),32)
            self.blocked(lambda:os.rename(self.file,self.source/'foreign.png'))
            self.blocked(lambda:os.rename(self.source,self.root/'moved-parent'))
            def check_destination(evidence,operator):
                self.blocked(lambda:os.rename(self.stage,self.root/'moved-stage'))
                return ack(evidence,operator)
            pin.move_to(self.stage/'safe.png',journal=journal,acknowledge=check_destination)

    def test_occupied_destination_is_preserved(self):
        target=self.stage/'occupied.png'; target.write_bytes(b'foreign existing file')
        with MovableFileContext(self.file,self.digest) as pin,MoveJournal(self.root/'occupied.jsonl') as journal:
            with self.assertRaises(FileExistsError):pin.move_to(target,journal=journal,acknowledge=ack)
            self.assertIsNone(pin.pending)
        self.assertEqual(target.read_bytes(),b'foreign existing file'); self.assertEqual(sha(self.file),self.digest)

    def test_destination_race_is_rejected_by_api_without_overwrite(self):
        target=self.stage/'raced.png'
        with MovableFileContext(self.file,self.digest) as pin,MoveJournal(self.root/'race.jsonl') as journal:
            record=journal.record
            def raced(phase,**details):
                record(phase,**details)
                if phase=='move_intent':target.write_bytes(b'foreign raced file')
            journal.record=raced
            with self.assertRaises(OSError):pin.move_to(target,journal=journal,acknowledge=ack)
            self.assertIsNone(pin.pending)
        self.assertEqual(target.read_bytes(),b'foreign raced file'); self.assertEqual(sha(self.file),self.digest)

    def test_hardlinked_source_requires_review(self):
        os.link(self.file,self.source/'second-link.png')
        with self.assertRaisesRegex(ValueError,'hardlinked'):
            with MovableFileContext(self.file,self.digest):pass
        self.assertEqual(sha(self.file),self.digest)

    def test_source_and_destination_reparse_paths_are_rejected(self):
        # Junction creation does not require the symlink privilege. Both ends
        # stay inside this verified private fixture; no library path is used.
        source_link=self.root/'source-link'; directory_link=self.root/'directory-link'
        for link,target in ((source_link,self.source),(directory_link,self.stage)):
            assert link.parent.resolve().is_relative_to(PROTOTYPE)
            assert target.resolve().is_relative_to(PROTOTYPE)
            result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)],
                capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode,0,result.stderr)
        with self.assertRaisesRegex(ValueError,'linked_path'):
            with MovableFileContext(source_link/self.file.name,self.digest):pass
        with MovableFileContext(self.file,self.digest) as pin,MoveJournal(self.root/'link.jsonl') as journal:
            with self.assertRaisesRegex(ValueError,'linked_path'):
                pin.move_to(directory_link/'target.png',journal=journal,acknowledge=ack)

    def test_failed_ack_keeps_operator_and_directories_open_until_explicit_finish(self):
        operator=None
        try:
            with DirectoryPins() as directories,MoveJournal(self.root/'pending.jsonl') as journal:
                with MovableFileContext(self.file,self.digest,directories=directories) as pin:
                    operator=pin
                    def fail(evidence,operator):raise RuntimeError('injected Native acknowledgement failure')
                    pin.move_to(self.stage/'pending.png',journal=journal,acknowledge=fail)
        except PendingMove as error:
            self.assertIs(error.operator,operator)
            self.assertFalse(operator.stream.closed); self.assertTrue(directories.active)
            self.assertFalse(journal.stream.closed)
            try:
                self.blocked(lambda:os.rename(self.stage,self.root/'foreign-stage'))
                operator.finish_pending(ack)
                operator.close()
            finally:
                if operator.stream is not None:operator.release_for_recovery()
        else:self.fail('PendingMove required')
        self.assertFalse(directories.active); self.assertTrue(journal.stream.closed)

    def test_pending_release_is_durable_and_preserves_moved_bytes(self):
        with MoveJournal(self.root/'release.jsonl') as journal:
            pin=MovableFileContext(self.file,self.digest).__enter__()
            try:
                def fail(evidence,operator):raise RuntimeError('injected failure')
                with self.assertRaises(PendingMove):pin.move_to(self.stage/'retained.png',journal=journal,acknowledge=fail)
                pin.release_for_recovery()
            finally:
                if pin.stream is not None:
                    if pin.pending:pin.release_for_recovery()
                    else:pin.close()
        self.assertEqual(sha(self.stage/'retained.png'),self.digest)
        phases=[json.loads(line)['phase'] for line in (self.root/'release.jsonl').read_bytes().splitlines()]
        self.assertEqual(phases[-1],'operator_released_for_recovery')

    def test_cancellation_after_physical_move_still_finishes_ack(self):
        stop=threading.Event()
        with MovableFileContext(self.file,self.digest) as pin,MoveJournal(self.root/'cancel.jsonl') as journal:
            def admitted(evidence,operator):
                stop.set();return ack(evidence,operator)
            result=pin.move_to(self.stage/'cancelled.png',journal=journal,acknowledge=admitted,cancel=stop)
            self.assertTrue(result['cancelled_after_ack']);self.assertIsNone(pin.pending)
            with self.assertRaisesRegex(RuntimeError,'cancelled_before_move'):
                pin.move_to(self.file,journal=journal,acknowledge=ack,cancel=stop)

    def test_local_move_path_rejects_ambiguous_names_before_disk_access(self):
        for value in ('relative.png','C:/valid/../ambiguous.png','C:/valid/a.png:stream','//server/share/a.png'):
            with self.subTest(value=value),self.assertRaises(ValueError):local_move_path(value,existing=False)
        self.assertEqual(sha(self.file),self.digest)

    def test_cancel_event_interrupts_hash_between_chunks_before_any_rename(self):
        self.file.write_bytes(b'x'*(4*1024*1024));digest=sha(self.file)
        class ChunkCancel:
            calls=0
            def is_set(self):
                self.calls+=1
                return self.calls>=3
        event=ChunkCancel()
        with self.assertRaisesRegex(RuntimeError,'cancel'):
            with MovableFileContext(self.file,digest,cancel_event=event):pass
        self.assertEqual(event.calls,3);self.assertTrue(self.file.exists());self.assertEqual(sha(self.file),digest)

    def test_cancel_event_after_physical_move_still_drains_receipt(self):
        event=threading.Event()
        with MovableFileContext(self.file,self.digest,cancel_event=event) as pin,MoveJournal(self.root/'drain-cancel.jsonl') as journal:
            def complete(evidence,operator):event.set();return ack(evidence,operator)
            result=pin.move_to(self.stage/'drained.png',journal=journal,acknowledge=complete,cancel=event)
            self.assertIsNone(pin.pending);self.assertTrue(result['cancelled_after_ack'])


if __name__=='__main__':unittest.main(verbosity=2)
