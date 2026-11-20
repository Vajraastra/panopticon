"""Windows no-overwrite moves by retained handle, with durable worker receipts.

No SQL, path-based source rename/delete, copy fallback or overwrite capability.
"""
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from core.path_identity import local_path
from core.pinned_file import PinnedFile, plain_chain


class RenameInfo(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('RootDirectory', wintypes.HANDLE),
                ('FileNameLength', wintypes.DWORD), ('FileName', wintypes.WCHAR * 1)]


class FileIdInfo(ctypes.Structure):
    _fields_ = [('Volume', ctypes.c_ulonglong), ('Id', ctypes.c_ubyte * 16)]


class AttributeInfo(ctypes.Structure):
    _fields_ = [('Attributes', wintypes.DWORD), ('ReparseTag', wintypes.DWORD)]


def api():
    if os.name != 'nt': raise ValueError('windows_required')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
        ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]; kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]; kernel.GetDriveTypeW.restype = wintypes.UINT
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE,wintypes.LPWSTR,wintypes.DWORD,wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    return kernel


def final_path(kernel, handle):
    buffer = ctypes.create_unicode_buffer(32768)
    count = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if not 0 < count < len(buffer): raise ctypes.WinError(ctypes.get_last_error())
    return Path(buffer.value.removeprefix('\\\\?\\'))


def identity128(kernel, handle):
    info = FileIdInfo()
    if not kernel.GetFileInformationByHandleEx(handle,18,ctypes.byref(info),ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return info.Volume, bytes(info.Id).hex()


def local_move_path(path, *, existing):
    path = Path(local_path(str(path)))
    plain_chain(path if existing else path.parent)
    actual = path.resolve(strict=existing)
    return actual


class DirectoryPins:
    """Hold named ancestors without sharing DELETE; permit child publication."""
    def __init__(self):
        self.kernel = api(); self.stack = ExitStack(); self.records = {}; self.active = True
        self.users = 0; self.closing = False

    def acquire(self):
        if not self.active or self.closing: raise ValueError('directory_pins_closing')
        self.users += 1

    def release(self):
        self.users -= 1
        if self.closing and self.users == 0: self.close()

    def pin(self, folder):
        if not self.active: raise ValueError('directory_pins_closed')
        folder = local_move_path(folder, existing=True)
        if self.kernel.GetDriveTypeW(folder.anchor) not in (2,3): raise ValueError("local_volume_required")
        for path in (*reversed(folder.parents),folder):
            key = str(path)
            if key in self.records: continue
            plain_chain(path)
            handle = self.kernel.CreateFileW(str(path),0xA0,3,None,3,0x02000000|0x00200000,None)
            if handle == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
            self.stack.callback(self.kernel.CloseHandle,handle)
            attributes = AttributeInfo()
            if not self.kernel.GetFileInformationByHandleEx(handle,9,ctypes.byref(attributes),ctypes.sizeof(attributes)):
                raise ctypes.WinError(ctypes.get_last_error())
            if attributes.Attributes & 0x400 or not attributes.Attributes & 0x10:
                raise ValueError('linked_or_non_directory_ancestor')
            actual = final_path(self.kernel,handle)
            if os.path.normcase(str(actual)) != os.path.normcase(str(path.resolve(strict=True))):
                raise ValueError('directory_identity_changed')
            volume,file_id = identity128(self.kernel,handle)
            self.records[key] = dict(handle=handle,path=str(actual),volume=volume,file_id=file_id)
        return self.records[str(folder)]

    def close(self):
        self.closing = True
        if not self.users:
            self.active = False; self.stack.close()

    def __enter__(self): return self
    def __exit__(self,*args): self.close()


class MoveJournal:
    def __init__(self,path):
        self.path = local_move_path(path,existing=False)
        self.stream = self.path.open('xb')
        self.users = 0; self.closing = False

    def retain(self):
        if self.closing: raise ValueError('journal_closing')
        self.users += 1

    def release(self):
        self.users -= 1
        if not self.users and self.closing: self.stream.close()

    def record(self,phase,**details):
        raw = json.dumps(dict(phase=phase,**details),sort_keys=True,ensure_ascii=False,allow_nan=False).encode('utf-8')
        self.stream.write(raw+b'\n'); self.stream.flush(); os.fsync(self.stream.fileno())

    def close(self):
        self.closing = True
        if not self.users: self.stream.close()
    def __enter__(self): return self
    def __exit__(self,*args): self.close()


@dataclass(frozen=True)
class Proof:
    path: str
    volume: int
    file_id: str
    size: int
    mtime_ns: int
    birthtime_ns: int
    digest: str

    def invariant(self):
        return self.volume,self.file_id,self.size,self.mtime_ns,self.digest


class PendingMove(RuntimeError):
    def __init__(self,operator,cause):
        self.operator = operator
        self.evidence = operator.pending
        super().__init__('move_requires_ack_or_recovery: '+str(cause))


class MovableFileContext(PinnedFile):
    """PinnedFile verification plus a separate, non-deleting move capability.

PendingMove retains this operator/handle. Resolve with finish_pending(), or
explicitly release_for_recovery() after recording the durable boundary.
"""
    def __init__(self,path,expected_digest,*,directories=None,cancel_event=None):
        super().__init__(path,delete=False)
        if not re.fullmatch('[0-9a-f]{64}',expected_digest): raise ValueError('invalid_digest')
        self.expected_digest = expected_digest
        self.cancel_event = cancel_event
        self.directories = directories or DirectoryPins()
        self.owns_directories = directories is None
        self.pending = None; self.current = None; self.journal = None
        self.directory_lease = False

    def __enter__(self):
        import msvcrt
        if not self.directories.active: raise ValueError('directory_pins_closed')
        try:
            self.path = local_move_path(self.path,existing=True)
            self.directories.acquire(); self.directory_lease = True
            self.directories.pin(self.path.parent)
            self.actual = self.path.resolve(strict=True)
            self.api = api()
            if self.api.GetDriveTypeW(self.path.anchor) not in (2,3): raise ValueError('local_volume_required')
            self.handle = self.api.CreateFileW(str(self.path),0x80010000,1,None,3,0x00200000|0x08000000,None)
            if self.handle == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
            try:
                fd = msvcrt.open_osfhandle(self.handle,os.O_RDONLY|os.O_BINARY|os.O_NOINHERIT)
            except BaseException:
                self.api.CloseHandle(self.handle); raise
            try: self.stream = os.fdopen(fd,'rb',buffering=0)
            except BaseException:
                os.close(fd); raise
            self.current = self.proof()
            return self
        except BaseException:
            self._close_resources(); raise

    def check(self):
        result = super().check()
        if os.fstat(self.stream.fileno()).st_nlink != 1: raise ValueError('hardlinked_source_requires_review')
        return result

    def proof(self,*,event=None):
        effective=self.cancel_event if event is None else (None if event is False else event)
        identity = self.check(); self.verify(self.expected_digest,identity,event=effective)
        volume,file_id = identity128(self.api,self.handle)
        return Proof(str(final_path(self.api,self.handle)),volume,file_id,identity[2],identity[3],identity[4],self.expected_digest)

    def move_to(self,target,*,journal,acknowledge,step_id=None,cancel=None):
        if self.pending: raise PendingMove(self,'previous_step_not_acknowledged')
        if self.directories.closing: raise ValueError('directory_pins_closing')
        if cancel is not None and cancel.is_set(): raise RuntimeError('cancelled_before_move')
        target = local_move_path(target,existing=False)
        directory = self.directories.pin(target.parent)
        before = self.proof()
        if before != self.current: raise ValueError('source_proof_changed')
        if directory['volume'] != before.volume: raise ValueError('cross_volume_move_forbidden')
        if os.path.lexists(target): raise FileExistsError(str(target))
        step_id = step_id or str(uuid4())
        evidence = dict(step_id=step_id,source=before.path,target=str(target),before=asdict(before))
        journal.record('move_intent',**evidence)
        journal.retain()
        encoded = str(target).encode('utf-16-le')
        buffer = ctypes.create_string_buffer(ctypes.sizeof(RenameInfo)+len(encoded)+2)
        info = ctypes.cast(buffer,ctypes.POINTER(RenameInfo)).contents
        info.Flags=0; info.RootDirectory=None; info.FileNameLength=len(encoded)
        ctypes.memmove(ctypes.addressof(buffer)+RenameInfo.FileName.offset,encoded,len(encoded))
        self.pending=evidence; self.journal=journal
        if not self.api.SetFileInformationByHandle(self.handle,3,buffer,len(buffer)):
            error=ctypes.WinError(ctypes.get_last_error())
            try: unchanged = final_path(self.api,self.handle) == Path(before.path) and self.proof() == before
            except Exception: unchanged = False
            if unchanged:
                self.pending=None
                journal.release()
                journal.record('move_failed_before_publication',error=str(error),**evidence)
                raise error
            self.pending=evidence; self.journal=journal
            raise PendingMove(self,error)
        self.pending=evidence; self.journal=journal
        try:
            self.path=final_path(self.api,self.handle); self.actual=self.path
            after=self.proof(event=False)
            if after.invariant()!=before.invariant(): raise ValueError('move_invariant_changed')
            if os.path.normcase(after.path)!=os.path.normcase(str(target)) or self.path.name!=target.name:
                raise ValueError('published_name_changed')
            self.current=after; evidence['after']=asdict(after)
            journal.record('move_physical_complete',**evidence)
            receipt=self.finish_pending(acknowledge)
            return dict(evidence=evidence,receipt=receipt,cancelled_after_ack=bool(cancel and cancel.is_set()))
        except PendingMove: raise
        except BaseException as error: raise PendingMove(self,error) from error

    def finish_pending(self,acknowledge):
        if not self.pending: raise ValueError('no_pending_move')
        try:
            proof=self.proof(event=False)
            if asdict(proof)!=self.pending.get('after'): raise ValueError('pending_file_changed')
            receipt=acknowledge(dict(self.pending),self)
            if type(receipt) is not dict or receipt.get('confirmed') is not True or receipt.get('step_id')!=self.pending['step_id']:
                raise ValueError('acknowledgement_unconfirmed')
            if asdict(self.proof(event=False))!=self.pending['after']: raise ValueError('file_changed_during_acknowledgement')
            self.journal.record('move_acknowledged',receipt=receipt,**self.pending)
            self.pending=None
            self.journal.release()
            return receipt
        except BaseException as error:
            if isinstance(error,PendingMove): raise
            raise PendingMove(self,error) from error

    def release_for_recovery(self):
        if not self.pending: raise ValueError('no_pending_move')
        self.journal.record('operator_released_for_recovery',**self.pending)
        self.journal.release()
        self._close_resources()

    def _close_resources(self):
        super().close()
        if self.directory_lease:
            self.directory_lease = False; self.directories.release()
        if self.owns_directories: self.directories.close()

    def close(self):
        if self.pending and self.stream is not None: raise PendingMove(self,'cannot_close_pending_operator')
        self._close_resources()

    def __exit__(self,kind,error,traceback):
        if self.pending and self.stream is not None:
            if isinstance(error,PendingMove): return False
            raise PendingMove(self,error or 'context_left_before_ack')
        self._close_resources()

