"""Bounded BBox draft publication; originals and occupied image outputs are protected."""
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from PIL import Image
from core.image_preview import _identity
from core.output_paths import safe_path
from core.pinned_file import PinnedFile
from .datamodel import WorkDoc


def digest(path):
    value=hashlib.sha256()
    size = 0
    with open(path,'rb') as stream:
        while block:=stream.read(1024*1024):
            size += len(block)
            if size > 8*1024*1024:raise ValueError('editor_draft_limit')
            value.update(block)
    return value.hexdigest()


def save(source, pano, out_dir, data, image, composited, source_identity,
         draft_identity, draft_digest, source_digest, pinned_identity, cancel):
    """An image already published stays for review if committing the draft fails."""
    result=dict(error=None,cancelled=False,published=None,recovery=None,draft_committed=None,draft_identity=None,draft_digest=None)
    image_temp=draft_temp=guard=None
    guard_identity=guard_token=None
    try:
        if cancel():result['cancelled']=True;return result
        source=safe_path(source)
        pano=Path(pano); out_dir=Path(out_dir)
        # Validate existing parent chains before creating anything under them.
        for folder in (pano.parent,out_dir):
            ancestor=folder
            while not ancestor.exists():ancestor=ancestor.parent
            safe_path(ancestor,portable=True)
            folder.mkdir(parents=True,exist_ok=True)
        pano=safe_path(pano,exists=pano.exists(),portable=True)
        output=safe_path(out_dir/source.name,exists=False,portable=True)
        guard_path=pano.with_name(pano.name+'.bbox.guard')
        guard_token=uuid.uuid4().hex
        # One publication per draft across windows/processes. Stale guards require review.
        fd=os.open(guard_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        guard=guard_path
        with os.fdopen(fd,'w',encoding='ascii') as stream:
            stream.write(guard_token);stream.flush();os.fsync(stream.fileno())
        guard_identity=_identity(guard)
        def draft_current():
            if _identity(pano)!=draft_identity or (draft_identity is not None and digest(pano)!=draft_digest):
                raise ValueError('editor_draft_changed; reload before saving')
        with PinnedFile(source) as pin:
            if _identity(source)!=source_identity:
                raise ValueError('preview_source_changed')
            pin.verify(source_digest, pinned_identity)
            draft_current()
            data = dict(data, source_sha256=source_digest)
            if not composited and data.get('output_sha256'):
                with PinnedFile(safe_path(output)) as existing:
                    existing.verify(data['output_sha256'], existing.check())
            if composited:
                if os.path.lexists(output):
                    raise ValueError('editor_requires_new_output; existing images are preserved')
                if output.suffix.lower() not in ('.png','.jpg','.jpeg','.webp'):
                    raise ValueError('editor_output_format')
                fd,name=tempfile.mkstemp(prefix='.panopticon-bbox-image-',suffix=output.suffix,dir=output.parent)
                os.close(fd);image_temp=Path(name)
                image.save(image_temp)
                with Image.open(image_temp) as verified:
                    verified.load()
                    if verified.size!=image.size:raise ValueError('editor_output_dimensions')
                # Windows CRT fsync requires a writable descriptor.
                with open(image_temp,'r+b') as stream:os.fsync(stream.fileno())
                with open(image_temp, 'rb') as stream:
                    data['output_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
            fd,name=tempfile.mkstemp(prefix='.panopticon-bbox-draft-',suffix='.json',dir=pano.parent)
            draft_temp=Path(name)
            with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
                json.dump(data,stream,ensure_ascii=False,indent=2)
                stream.flush();os.fsync(stream.fileno())
            if draft_temp.stat().st_size > 8*1024*1024:raise ValueError('editor_draft_limit')
            WorkDoc.load(draft_temp)
            expected_digest=digest(draft_temp)
            if cancel():result['cancelled']=True;return result
            pin.check();draft_current()
            if image_temp is not None:
                # Windows rename fails if another process wins the destination.
                if os.name!='nt':raise ValueError('editor_publication_requires_windows')
                os.rename(image_temp,output);image_temp=None
                result['published']=str(output)
            # Finish an accepted publication even if closing requests cancellation.
            draft_current()
            if draft_identity is None:
                if os.name!='nt':raise ValueError('editor_publication_requires_windows')
                os.rename(draft_temp,pano)
            else:
                os.replace(draft_temp,pano)
            draft_temp=None
            result['draft_committed']=str(pano)
            final_identity=_identity(pano)
            if digest(pano)!=expected_digest or _identity(pano)!=final_identity:
                raise ValueError('editor_draft_changed_after_commit')
            result.update(draft_identity=final_identity,draft_digest=expected_digest,
                          source_sha256=data['source_sha256'], output_sha256=data.get('output_sha256'))
    except Exception as error:
        result['error']=str(error)
        if cancel() and result['published'] is None and result['draft_committed'] is None:
            result['error']=None;result['cancelled']=True
        if result['published'] is not None and draft_temp is not None:
            result['recovery']=str(draft_temp)
            draft_temp=None
    finally:
        for path in (image_temp,draft_temp):
            if path is not None:
                try:path.unlink(missing_ok=True)
                except OSError as error:
                    result['error']=(result['error'] or '')+'; temporary cleanup failed: '+str(error)
        if guard is not None:
            try:
                if (_identity(guard)!=guard_identity or guard.read_text(encoding='ascii')!=guard_token):
                    raise ValueError('editor_guard_changed; retained for review')
                guard.unlink()
            except (OSError,ValueError) as error:
                result['error']=(result['error'] or '')+'; '+str(error)
    return result
