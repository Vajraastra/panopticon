"""Read one bounded editor source and its draft without owning GUI objects."""
from PIL import Image
from PySide6.QtGui import QImage
from core.image_preview import MAX_SOURCE_BYTES, _identity
from .datamodel import WorkDoc, STATUS_DRAFT

MAX_EDITOR_PIXELS = 20_000_000
MAX_DRAFT_BYTES = 8 * 1024 * 1024


def load(image_path, pano_path, style_defaults, cancel):
    if cancel():
        return None
    before = _identity(image_path)
    if before is None or before[2] > MAX_SOURCE_BYTES:
        raise ValueError('preview_file_limit')
    with Image.open(image_path) as source:
        w, h = source.size
        if w <= 0 or h <= 0 or w * h > MAX_EDITOR_PIXELS:
            raise ValueError('preview_pixel_limit')
        if getattr(source, 'n_frames', 1) != 1:
            raise ValueError('editor_single_frame_required')
        image = source.convert('RGB')
    if cancel():
        image.close()
        return None
    try:
        draft_before = _identity(pano_path)
        if draft_before is not None:
            if draft_before[2] > MAX_DRAFT_BYTES:
                raise ValueError('editor_draft_limit')
            doc = WorkDoc.load(pano_path)
            if (doc.image, doc.image_w, doc.image_h) != (image_path.name, w, h):
                raise ValueError('editor_draft_source_mismatch')
        else:
            doc = WorkDoc(image=image_path.name, image_w=w, image_h=h, status=STATUS_DRAFT)
            for key in ('art_style', 'aesthetics', 'lighting'):
                setattr(doc.style, key, style_defaults.get(key, '') or getattr(doc.style, key))
        if cancel():
            image.close()
            return None
        pixels = image.tobytes()
        qimage = QImage(pixels, w, h, w * 3, QImage.Format_RGB888).copy()
        if _identity(image_path) != before or _identity(pano_path) != draft_before:
            raise ValueError('preview_source_changed')
        return dict(image_path=image_path, pano_path=pano_path, image=image,
                    qimage=qimage, doc=doc, source_identity=before, draft_identity=draft_before)
    except Exception:
        image.close()
        raise
