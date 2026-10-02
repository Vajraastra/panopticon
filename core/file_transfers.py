"""Verified Windows file transfers shared by modules; no SQL connections."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from uuid import uuid4

from core.curation_apply import _identity
from core.derived_publication import DerivedPublication, write
from core.library_transfers import request as validate_request
from core.output_paths import safe_path, path_variants
from core.path_identity import local_path, observe_path
from core.paths import CachePaths
from core.pinned_file import PinnedFile

EXTENSIONS = {'.jpg','.jpeg','.png','.webp','.gif','.bmp','.tiff','.tif','.avif'}


def cancelled(event):
    if event is not None and event.is_set():
        raise ValueError('pickler_cancelled')


def digest(pin, event=None):
    before = pin.check()
    pin.stream.seek(0)
    hasher = hashlib.sha256()
    while block := pin.stream.read(1024*1024):
        cancelled(event); hasher.update(block)
    if pin.check() != before:
        raise ValueError('pickler_source_changed')
    return hasher.hexdigest()


class Journal(DerivedPublication):
    # Reuse only the common safe directory/atomic journal writer.
    def __init__(self, service, request, root=None):
        self.service, self.request = service, request
        self.root = Path(root) if root is not None else CachePaths.get_cache_root()/'pickler_recovery'
        self.path = None
        self.data = dict(kind='pickler_transfer_v1',version=1,request=request,phase='prepared',
                         operation_id=request.get('operation_id'),published=False)

    def ready(self, event):
        self.root = self.scope(event)
        self.root.mkdir(exist_ok=True)
        safe_path(self.root,portable=True)
        folder = self.root/self.request['operation_id']
        folder.mkdir()
        self.path = folder/'journal.json'
        self.save()

    def phase(self, phase, **details):
        self.data.update(phase=phase, **details); self.save()


def source_state(service, path, event, *, register_missing=True):
    resolution = service.resolve_path_identity(str(path),cancel_event=event)
    if resolution['status'] == 'unique':
        return service.read('get_copy_version_state',resolution['file_id'],cancel_event=event)
    if resolution['status'] not in ('missing','not_prepared'):
        raise ValueError('path_identity_review_required')
    if resolution['status'] in ('missing','not_prepared'):
        rows = service.read('get_transfer_sources',path_variants(path,path),cancel_event=event)['records']
        if rows:
            if len(rows) != 1 or rows[0][1]:
                raise ValueError('path_identity_review_required')
            current = service.read('get_copy_version_state',rows[0][0],cancel_event=event)
            saved = observe_path(current['state']['file']['path'],cancel_event=event)
            incoming = observe_path(str(path),cancel_event=event)
            if saved.status != 'resolved' or saved.canonical != incoming.canonical or saved.physical != incoming.physical:
                raise ValueError('path_identity_review_required')
            return current
    if not register_missing:raise ValueError("transfer_source_requires_review")
    info = path.stat()
    service.register_files_observed([dict(path=str(path),filename=path.name,size=info.st_size,
                                           created=info.st_ctime)],cancel_event=event)
    resolution = service.resolve_path_identity(str(path),cancel_event=event)
    if resolution['status'] == 'unique':
        key = resolution['file_id']
    elif resolution['status'] == 'not_prepared':
        rows = service.read('get_transfer_sources',path_variants(path,path),cancel_event=event)['records']
        if len(rows) != 1 or rows[0][1]:raise ValueError('path_identity_review_required')
        key = rows[0][0]
    else:
        raise ValueError('path_identity_review_required')
    return service.read('get_copy_version_state',key,cancel_event=event)


def destination(source, directory, *, service=None, event=None, exact=False):
    directory = Path(local_path(str(Path(directory).absolute())))
    parent = directory
    while not os.path.lexists(parent):parent = parent.parent
    safe_path(parent)
    directory.mkdir(parents=True,exist_ok=True)
    directory = safe_path(directory)
    observation = observe_path(str(directory))
    if observation.status != 'resolved' or observation.kind != 'directory':
        raise ValueError('pickler_destination_unavailable')
    directory = Path(observation.canonical)
    for number in range(10000):
        cancelled(event)
        name = source.name if not number else f'{source.stem}_{number}{source.suffix}'
        path = directory/name
        if not os.path.lexists(path) and not os.path.lexists(path.with_suffix('.txt')):
            if not exact and service is not None and service.read('get_metadata_path_status',
                    path_variants(path,path),cancel_event=event)['occupied']:
                continue
            return path
        if exact:raise ValueError('pickler_undo_destination_occupied')
    raise ValueError('pickler_destination_collision_limit')


def caption_for(source):
    if source.suffix.lower() not in EXTENSIONS:return None
    caption = source.with_suffix('.txt')
    if not os.path.lexists(caption):return None
    if any(ext != source.suffix.lower() and os.path.lexists(source.with_suffix(ext)) for ext in EXTENSIONS):
        raise ValueError('pickler_shared_caption_requires_review')
    return safe_path(caption)


def prepare_output(pin, target, journal, key, event):
    fd, name = tempfile.mkstemp(prefix='.pickler-'+journal.request['operation_id']+'-',suffix='.tmp',dir=target.parent)
    temporary = Path(name)
    journal.data.setdefault('temporary',{})[key] = str(temporary)
    try:
        pin.stream.seek(0)
        with os.fdopen(fd,'wb') as output:
            journal.save()
            while block := pin.stream.read(1024*1024):
                cancelled(event); output.write(block)
            output.flush(); os.fsync(output.fileno())
        info = os.fstat(pin.stream.fileno())
        os.utime(temporary,ns=(info.st_atime_ns,info.st_mtime_ns))
        with PinnedFile(temporary) as result:
            expected = journal.request['digest' if key == 'image' else 'caption_digest']
            result.verify(expected,result.check(),event)
            identity = result.check()
        journal.data.setdefault('temporary_identities',{})[key] = identity
        journal.save()
        return temporary
    except Exception:
        # Keep the owned prepared bytes/journal for review; never unlink by guess.
        raise


def published_pin(stack, temporary, target, expected, identity):
    # Native Windows rename cannot overwrite an existing file. A temporary is
    # prepared on the destination volume, including cross-volume transfers.
    safe_path(target,exists=False)
    os.rename(temporary,target)
    pin = stack.enter_context(PinnedFile(target))
    if pin.check()[:2] != tuple(identity[:2]):raise ValueError('pickler_published_identity_changed')
    pin.verify(expected,pin.check())
    return pin


def confirm(service, value):
    saved = service.read('get_transfer_receipt',value['operation_id'])['receipt']
    if saved is None or saved['request_fingerprint'] != validate_request(value)[1] or saved['phase'] != 'complete':
        raise ValueError('pickler_native_confirmation_missing')
    return saved


def commit_native(service, value, pin):
    observation = observe_path(str(pin.actual))
    if observation.status != 'resolved' or observation.kind != 'file' or observation.canonical != value['result_path']:
        raise ValueError('pickler_published_identity_changed')
    try:
        write(service,'commit_transfer',value,{'size':pin.check()[2]},
              {'canonical':observation.canonical,'physical':observation.physical})
    except Exception as exc:
        try:return confirm(service,value)
        except Exception:raise exc
    return confirm(service,value)


def transfer(service, source, directory, action, *, event=None, journal_root=None,
             exact=False, expected_digest=None, expected_identity=None, expected_caption=None, target_name=None, register_missing=True, expected_sha=None):
    if service is None:raise ValueError('pickler_native_service_unavailable')
    if os.name != 'nt':raise ValueError('pickler_requires_windows')
    if action not in ('copy','move','discard'):raise ValueError('invalid_transfer_action')
    event = event or threading.Event()
    cancelled(event)
    source = safe_path(source)
    current = source_state(service,source,event,register_missing=register_missing)
    source_file = current['state']['file']
    output = destination(source if target_name is None else source.with_name(target_name),directory,
                         service=service,event=event,exact=exact)
    caption = caption_for(source)
    target_caption = output.with_suffix('.txt') if caption is not None else None
    source_before = _identity(source.stat())
    journal = None
    published = False
    try:
        with ExitStack() as inputs, ExitStack() as outputs:
            image = inputs.enter_context(PinnedFile(source,delete=action != 'copy'))
            if image.check() != source_before:raise ValueError('pickler_source_changed')
            sha = digest(image,event)
            if expected_sha is not None and sha != expected_sha:
                raise ValueError("cherry_source_digest_changed")
            if expected_digest is not None and (sha != expected_digest or image.check() != tuple(expected_identity)):
                raise ValueError('pickler_undo_image_changed')
            text = inputs.enter_context(PinnedFile(caption,delete=action != 'copy')) if caption is not None else None
            text_sha = digest(text,event) if text else None
            if exact and expected_digest is not None and text_sha != expected_caption:raise ValueError('pickler_undo_caption_changed')
            value = dict(operation_id=str(uuid4()),source_copy_id=source_file['id'],action=action,
                source_path=source_file['path'],result_path=output.as_posix(),digest=sha,caption_digest=text_sha,
                source_fingerprint=current['fingerprint'],
                source_paths=sorted(set([source_file['path'],*[local_path(p) for p in path_variants(source,source)]])),
                destination_paths=[local_path(p) for p in path_variants(output,output)])
            validate_request(value)
            journal = Journal(service,value,journal_root)
            journal.data.update(source_identity=image.check(),caption_identity=text.check() if text else None)
            journal.ready(event)
            write(service,'prepare_transfers',event=event)
            write(service,'approve_transfer',value,event=event)
            journal.phase('approved')
            temporary = prepare_output(image,output,journal,'image',event)
            caption_tmp = prepare_output(text,target_caption,journal,'caption',event) if text else None
            cancelled(event)
            image.verify(sha,image.check(),event)
            if text:text.verify(text_sha,text.check(),event)
            journal.phase('ready_to_publish')
            # After the first publication finish the admitted pair/commit, even
            # when closing cancels later work. No GUI or SQLite transaction waits.
            published = True
            result = published_pin(outputs,temporary,output,sha,journal.data['temporary_identities']['image'])
            caption_result = published_pin(outputs,caption_tmp,target_caption,text_sha,
                journal.data['temporary_identities']['caption']) if text else None
            journal.phase('published',published=True,result_identity=result.check(),
                          result_caption_identity=caption_result.check() if caption_result else None)
            if action != 'copy':
                # Verified durable destination pinned before deleting the opened
                # originals. The Native reservation protects this non-atomic gap.
                if text:text.mark_delete()
                image.mark_delete()
                inputs.close()
                journal.phase('source_removed')
            receipt = commit_native(service,value,result)
            journal.phase('complete',receipt=receipt)
            return dict(request=value,receipt=receipt,journal=str(journal.path),original=value['source_path'],
                current=value['result_path'],action=action,result_identity=result.check(),
                caption_identity=caption_result.check() if caption_result else None)
    except Exception as exc:
        if journal is not None and journal.path is not None:
            if not published:
                try:
                    saved=service.read('get_transfer_receipt',journal.request['operation_id'])['receipt']
                    if saved is not None and saved['phase']=='approved':
                        write(service,'abort_transfer',journal.request['operation_id'],
                              expected_fingerprint=validate_request(journal.request)[1])
                except Exception:pass
            try:journal.phase('review_required' if published else 'not_published',error=str(exc),published=published)
            except Exception:pass
            raise RuntimeError(f'{exc}\n{journal.path}') from exc
        raise


def undo(service, previous, *, event=None, journal_root=None):
    cancelled(event)
    value = previous['request']
    saved = confirm(service,value)
    if value['action'] != 'copy':
        original = Path(value['source_path'])
        return transfer(service,previous['current'],original.parent,'move',event=event,journal_root=journal_root,
            exact=True,expected_digest=value['digest'],expected_identity=previous['result_identity'],
            expected_caption=value['caption_digest'],target_name=original.name)
    source, output = Path(value['source_path']),Path(value['result_path'])
    journal = Journal(service,value,journal_root)
    journal.path = safe_path(previous['journal'],portable=True)
    with journal.path.open('rb') as stream:raw = stream.read(512*1024+1)
    if len(raw)>512*1024:raise ValueError('pickler_journal_limit')
    journal.data = json.loads(raw)
    if journal.data.get('request') != value:raise ValueError('pickler_journal_changed')
    with ExitStack() as stack:
        keeper = stack.enter_context(PinnedFile(source))
        keeper.verify(value['digest'],journal.data['source_identity'],event)
        resuming = journal.data['phase'] == 'undo_ready'
        victim = stack.enter_context(PinnedFile(output,delete=True)) if os.path.lexists(output) else None
        if victim is None and not resuming:raise ValueError('pickler_undo_image_missing')
        if victim:victim.verify(value['digest'],previous['result_identity'],event)
        source_caption, output_caption = source.with_suffix('.txt'),output.with_suffix('.txt')
        text_victim = None
        if value['caption_digest'] is not None:
            text_keeper = stack.enter_context(PinnedFile(source_caption))
            text_keeper.verify(value['caption_digest'],journal.data['caption_identity'],event)
            text_victim = stack.enter_context(PinnedFile(output_caption,delete=True)) if os.path.lexists(output_caption) else None
            if text_victim is None and not resuming:raise ValueError('pickler_undo_caption_missing')
            if text_victim:text_victim.verify(value['caption_digest'],previous['caption_identity'],event)
        elif source.suffix.lower() in EXTENSIONS and (os.path.lexists(source_caption) or os.path.lexists(output_caption)):
            raise ValueError('pickler_undo_caption_changed')
        cancelled(event)
        journal.phase('undo_ready')
        write(service,'retire_transfer_copy',value['operation_id'],event=event)
        # Once archived settle the already verified deletion; IDs/curation remain.
        if text_victim:text_victim.mark_delete()
        if victim:victim.mark_delete()
        stack.close()
        journal.phase('undone',receipt=saved)
    return dict(original=str(source),current=str(output),action='undo_copy')


def read_journal(path, service, root, event):
    path = safe_path(path,portable=True)
    journal = Journal(service,{},root)
    allowed = journal.scope(event)
    if path.name != 'journal.json' or path.parent.parent != allowed:
        raise ValueError('pickler_journal_scope_requires_review')
    with path.open('rb') as stream:raw = stream.read(512*1024+1)
    if len(raw)>512*1024:raise ValueError('pickler_journal_limit')
    def pairs(items):
        result = {}
        for key,value in items:
            if key in result:raise ValueError('pickler_journal_duplicate_key')
            result[key] = value
        return result
    data = json.loads(raw,object_pairs_hook=pairs)
    if data.get('kind') != 'pickler_transfer_v1' or data.get('version') != 1:
        raise ValueError('pickler_journal_requires_review')
    value,_ = validate_request(data['request'])
    if data.get('operation_id') != value['operation_id'] or path.parent.name != value['operation_id']:
        raise ValueError('pickler_journal_requires_review')
    journal.request,journal.data,journal.path = value,data,path
    return journal


def restore_file(provider, target, journal, key, expected, outputs):
    safe_path(target,exists=False)
    fd,name = tempfile.mkstemp(prefix='.pickler-restore-',suffix='.tmp',dir=target.parent)
    temporary = Path(name)
    journal.data.setdefault('restoration_temporary',{})[key] = str(temporary)
    with os.fdopen(fd,'wb') as stream:
        journal.save()
        provider.stream.seek(0)
        while block := provider.stream.read(1024*1024):stream.write(block)
        stream.flush(); os.fsync(stream.fileno())
    info = os.fstat(provider.stream.fileno()); os.utime(temporary,ns=(info.st_atime_ns,info.st_mtime_ns))
    with PinnedFile(temporary) as prepared:
        prepared.verify(expected,prepared.check()); identity = prepared.check()
    pin = published_pin(outputs,temporary,target,expected,identity)
    journal.data.setdefault('restored_identities',{})[key] = pin.check(); journal.save()
    return pin


def recover(service, path, *, event=None, journal_root=None):
    """Explicitly restore a pending source; never roll back a Native commit."""
    cancelled(event)
    journal = read_journal(path,service,journal_root,event)
    value = journal.request
    saved = service.read('get_transfer_receipt',value['operation_id'],cancel_event=event)['receipt']
    if saved is None or saved['phase'] == 'aborted':
        return dict(status='not_pending',request=value)
    if saved['request_fingerprint'] != validate_request(value)[1]:raise ValueError('pickler_journal_changed')
    if saved['phase'] == 'complete':
        if journal.data.get('phase') == 'undo_ready':
            previous = dict(request=value,journal=str(journal.path),current=value['result_path'],
                result_identity=journal.data['result_identity'],caption_identity=journal.data['result_caption_identity'])
            undo(service,previous,event=event,journal_root=journal_root)
            return dict(status='undo_completed',request=value)
        journal.phase('complete',receipt=saved)
        return dict(status='already_confirmed',request=value)
    source,output = Path(value['source_path']),Path(value['result_path'])
    with ExitStack() as stack:
        restored = {}; publications = []
        for key,target,dest,sha in [('image',source,output,value['digest']),
                ('caption',source.with_suffix('.txt'),output.with_suffix('.txt'),value['caption_digest'])]:
            if key == 'caption' and source.suffix.lower() not in EXTENSIONS:continue
            if sha is None:
                if os.path.lexists(target) or os.path.lexists(dest):raise ValueError('pickler_recovery_caption_changed')
                continue
            provider = None
            if os.path.lexists(dest):
                candidate = stack.enter_context(PinnedFile(dest,delete=True))
                expected = journal.data.get('result_identity' if key=='image' else 'result_caption_identity')
                initial = journal.data.get('temporary_identities',{}).get(key)
                # A collision is somebody else's file; never delete it. The
                # original can still be retained and the reservation cancelled.
                own = initial is not None and candidate.check()[:2] == tuple(initial[:2])
                if own:
                    candidate.verify(sha,expected if expected is not None else candidate.check())
                    publications.append(candidate); provider = candidate
            if os.path.lexists(target):
                pin = stack.enter_context(PinnedFile(target))
                identity = journal.data.get('restored_identities',{}).get(key,
                    journal.data['source_identity' if key=='image' else 'caption_identity'])
                pin.verify(sha,identity,event); restored[key] = pin
            else:
                if provider is None:
                    temporary = Path(journal.data.get('temporary',{}).get(key,''))
                    if (temporary.parent != output.parent or not temporary.name.startswith('.pickler-'+value['operation_id']+'-')
                            or temporary.suffix != '.tmp'):
                        raise ValueError('pickler_recovery_bytes_unavailable')
                    provider = stack.enter_context(PinnedFile(temporary))
                    provider.verify(sha,journal.data['temporary_identities'][key],event)
                restored[key] = restore_file(provider,target,journal,key,sha,stack)
        cancelled(event)
        actual = observe_path(str(restored['image'].actual))
        if actual.status != 'resolved' or actual.kind != 'file':raise ValueError('pickler_recovery_source_changed')
        current = service.read('get_copy_version_state',value['source_copy_id'],cancel_event=event)
        write(service,'approve_transfer_restore',value,current['fingerprint'],
            {'canonical':actual.canonical,'physical':actual.physical},event=event)
        journal.phase('restored_source')
        # Keep Native guards and pinned restored bytes until removing only owned
        # unregistered publications. No deletion of collisions or modified files.
        for pin in publications:pin.mark_delete()
        stack.close()
        write(service,'abort_transfer',value['operation_id'],expected_fingerprint=validate_request(value)[1])
        journal.phase('restored_and_aborted')
    return dict(status='source_restored',request=value)


def scan_folder(folder, event):
    folder=safe_path(folder)
    paths=[]; budget=0
    with os.scandir(folder) as entries:
        for entry in entries:
            cancelled(event)
            if Path(entry.name).suffix.lower() not in EXTENSIONS or not entry.is_file(follow_symlinks=False):continue
            path=Path(entry.path); budget+=len(str(path).encode('utf-8'))
            if len(paths)>=500000 or budget>64*1024*1024:raise ValueError('pickler_selection_limit')
            paths.append(path)
    return dict(images=sorted(paths,key=lambda path:path.name.lower()),folder=folder)


def load_images(paths,event):
    images=[]; budget=0
    for value in paths:
        cancelled(event)
        if not isinstance(value,(str,Path)):raise ValueError('pickler_invalid_selection')
        path=Path(value)
        if len(str(path))>4096:raise ValueError('pickler_invalid_selection')
        if path.suffix.lower() not in EXTENSIONS:continue
        budget+=len(str(path).encode('utf-8'))
        if len(images)>=500000 or budget>64*1024*1024:raise ValueError('pickler_selection_limit')
        images.append(path)
    return dict(images=images,folder=None)


def list_recovery(service, *, event=None, journal_root=None):
    if service is None:raise ValueError('pickler_native_service_unavailable')
    journal=Journal(service,{},journal_root)
    root=journal.scope(event)
    if not root.exists():return dict(records=[],truncated=False)
    records=[]; checked=0; truncated=False
    with os.scandir(root) as entries:
        for entry in entries:
            cancelled(event); checked+=1
            if checked>10000 or len(records)>=100:
                truncated=True; break
            if not entry.is_dir(follow_symlinks=False):continue
            path=Path(entry.path)/'journal.json'
            try:
                data=read_journal(path,service,root,event)
                phase=data.data.get('phase')
                if phase in ('complete','undone','restored_and_aborted'):continue
                saved=service.read('get_transfer_receipt',data.request['operation_id'],cancel_event=event)['receipt']
                if saved is None or saved['phase']=='aborted':continue
                if saved['request_fingerprint'] != validate_request(data.request)[1]:raise ValueError('pickler_journal_changed')
                records.append(dict(path=str(path),request=data.request,phase=phase))
            except Exception as exc:
                cancelled(event)
                records.append(dict(path=str(path),error=str(exc)))
    return dict(records=records,truncated=truncated)
