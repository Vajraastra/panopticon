"""Estampado de temporales: contenedores PNG/JPEG/WebP sin recodificar píxeles.

No identifica originales Native ni escribe SQLite. El productor publica después;
editar un original indexado requiere el puente recuperable de Metadata Hub.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import xml.etree.ElementTree as ET
import zlib

from PIL import Image

MAX_FILE = 256*1024*1024
MAX_METADATA = 256*1024
MAX_CHUNKS = 100_000
PNG = b'\x89PNG\r\n\x1a\n'
PAN = '{http://panopticon/ns/}data'
RDF = '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}'
RAW_KEYS = ('parameters', 'prompt', 'workflow', 'Comment', 'Description', 'Software', 'Source')


def _json(data):
    result = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(result.encode('utf-8')) > MAX_METADATA: raise ValueError('stamp_metadata_limit')
    return result


def payload(bundle):
    data = bundle.to_dict()
    if (not isinstance(data['tags'], list) or len(data['tags']) > 4096 or
            any(not isinstance(tag, str) or len(tag) > 65536 or '\x00' in tag for tag in data['tags']) or
            type(data['rating']) is not int or not 0 <= data['rating'] <= 5 or
            type(data['quality_score']) is not int or not 0 <= data['quality_score'] <= 100 or
            not isinstance(data['loras'], list) or len(data['loras']) > 4096 or
            any(not isinstance(value, str) or len(value) > 65536 or '\x00' in value for value in data['loras'])):
        raise ValueError('invalid_stamp_curation')
    for key in ('positive_prompt', 'negative_prompt', 'model', 'seed', 'steps', 'cfg', 'sampler', 'vae', 'tool'):
        if not isinstance(data[key], str) or len(data[key]) > 65536 or '\x00' in data[key]:
            raise ValueError('invalid_stamp_generation')
    data.pop('source_format')
    data['software'] = 'Panopticon'
    data['raw_generation'] = {key: bundle.raw[key] for key in RAW_KEYS if key in bundle.raw}
    if any(not isinstance(value, str) or '\x00' in value for value in data['raw_generation'].values()):
        raise ValueError('invalid_stamp_raw')
    _json(data)
    return data


def _path(path):
    path = Path(path).absolute()
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
        raise ValueError('stamp_unsupported_file')
    for parent in (path, *path.parents):
        info = parent.stat(follow_symlinks=False)
        if parent.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('stamp_link_requires_review')
    path = path.resolve(strict=True)
    for parent in path.parents:
        if os.path.lexists(parent/'catalog.db') or os.path.lexists(parent/'panopticon.curation.db'):
            raise ValueError('stamp_cherry_protected')
    return path


def _identity(path):
    info = path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _digest(path):
    value = hashlib.sha256()
    total = 0
    with open(path, 'rb') as source:
        while block := source.read(1024*1024):
            total += len(block)
            if total > MAX_FILE: raise ValueError('stamp_file_limit')
            value.update(block)
    return value.digest()


def _copy(source, output, size):
    while size:
        block = source.read(min(size, 1024*1024))
        if not block: raise ValueError('stamp_truncated_container')
        output.write(block)
        size -= len(block)


def _inflate(raw):
    dec = zlib.decompressobj()
    result = dec.decompress(raw, MAX_METADATA+1)
    if len(result) > MAX_METADATA or not dec.eof or dec.unused_data or dec.unconsumed_tail:
        raise ValueError('stamp_png_text_limit')
    return result


def png_text(raw, kind):
    key, value = raw.split(b'\x00', 1)
    if not 1 <= len(key) <= 79: raise ValueError('stamp_invalid_png_keyword')
    if kind == b'tEXt': return key.decode('latin-1'), value.decode('latin-1')
    if kind == b'zTXt':
        if value[:1] != b'\x00': raise ValueError('stamp_invalid_png_text')
        return key.decode('latin-1'), _inflate(value[1:]).decode('latin-1')
    if len(value) < 2 or value[0] not in (0, 1) or value[1] != 0:
        raise ValueError('stamp_invalid_png_text')
    flag = value[0]
    _, _, value = value[2:].split(b'\x00', 2)
    return key.decode('latin-1'), (_inflate(value) if flag else value).decode('utf-8')


def png_chunks(source):
    size = os.fstat(source.fileno()).st_size
    if size > MAX_FILE or source.read(8) != PNG: raise ValueError('stamp_invalid_png')
    for _ in range(MAX_CHUNKS):
        header = source.read(8)
        if len(header) != 8: raise ValueError('stamp_truncated_png')
        length, kind = struct.unpack('>I4s', header)
        if source.tell()+length+4 > size: raise ValueError('stamp_truncated_png')
        position = source.tell()
        yield kind, length, position
        source.seek(position+length+4)
        if kind == b'IEND':
            if length != 0 or source.tell() != size: raise ValueError('stamp_invalid_png_end')
            return
    raise ValueError('stamp_container_chunk_limit')


def read_png(path):
    result, total = {}, 0
    with open(path, 'rb') as source:
        for kind, length, _ in png_chunks(source):
            if kind not in (b'tEXt', b'iTXt', b'zTXt'): continue
            total += length
            if total > MAX_METADATA: raise ValueError('stamp_png_text_limit')
            raw = source.read(length)
            crc = source.read(4)
            if crc != struct.pack('>I', zlib.crc32(kind+raw) & 0xffffffff): raise ValueError('stamp_png_text_crc')
            key, value = png_text(raw, kind)
            total += max(0, len(value.encode('utf-8'))-length)
            if total > MAX_METADATA: raise ValueError('stamp_png_text_limit')
            if key in result: raise ValueError('stamp_duplicate_png_text')
            result[key] = value
    return result


def _chunk(output, kind, value):
    output.write(struct.pack('>I', len(value))+kind+value+struct.pack('>I', zlib.crc32(kind+value) & 0xffffffff))


def _parameters(data):
    if 'parameters' in data['raw_generation']: return data['raw_generation']['parameters']
    if not (data['positive_prompt'] or data['negative_prompt']): return None
    text = data['positive_prompt']+'\nNegative prompt: '+data['negative_prompt']
    technical = ', '.join(label+': '+data[key] for label, key in (
        ('Steps', 'steps'), ('Sampler', 'sampler'), ('CFG scale', 'cfg'), ('Seed', 'seed'),
        ('Model', 'model')) if data[key])
    return text+('\n'+technical if technical else '')


def _png(path, output, data):
    read_png(path)  # Fallar antes de copiar texto ambiguo/limitado.
    texts = dict(data['raw_generation'], panopticon_data=_json(data))
    if not any(key in texts for key in ('parameters', 'prompt', 'workflow')):
        parameters = _parameters(data)
        if parameters is not None: texts['parameters'] = parameters
    with open(path, 'rb') as source:
        output.write(PNG)
        for kind, length, position in png_chunks(source):
            if kind in (b'tEXt', b'iTXt', b'zTXt'):
                key, _ = png_text(source.read(length), kind)
                if key in texts: continue
            if kind == b'IEND':
                for key, value in texts.items():
                    _chunk(output, b'iTXt', key.encode('latin-1')+b'\x00\x00\x00\x00\x00'+value.encode('utf-8'))
            source.seek(position-8)
            _copy(source, output, length+12)


def _jpeg(path, output, data):
    with Image.open(path) as image:
        exif = image.getexif()
        # ImageDescription es ASCII en EXIF; JSON escapes conserva Unicode sin mojibake.
        exif[0x010e] = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
        parameters = _parameters(data)
        if parameters is not None:
            exif[0x9286] = b'ASCII\x00\x00\x00'+parameters.encode('utf-8')
        raw = exif.tobytes()
    if len(raw)+2 > 65535: raise ValueError('stamp_jpeg_exif_limit')
    with open(path, 'rb') as source:
        if source.read(2) != b'\xff\xd8': raise ValueError('stamp_invalid_jpeg')
        output.write(b'\xff\xd8\xff\xe1'+struct.pack('>H', len(raw)+2)+raw)
        for _ in range(MAX_CHUNKS):
            fill = bytearray(source.read(1))
            if fill != b'\xff': raise ValueError('stamp_invalid_jpeg_marker')
            marker = source.read(1)
            while marker == b'\xff':
                fill.extend(marker)
                if len(fill) > 65536: raise ValueError('stamp_jpeg_fill_limit')
                marker = source.read(1)
            if len(marker) != 1: raise ValueError('stamp_truncated_jpeg')
            if marker == b'\xd9': raise ValueError('stamp_jpeg_missing_scan')
            if marker in (b'\x01',) or 0xd0 <= marker[0] <= 0xd7:
                output.write(fill+marker)
                continue
            size = source.read(2)
            if len(size) != 2: raise ValueError('stamp_truncated_jpeg')
            length = struct.unpack('>H', size)[0]
            if length < 2: raise ValueError('stamp_invalid_jpeg_segment')
            segment = source.read(length-2)
            if len(segment) != length-2: raise ValueError('stamp_truncated_jpeg')
            if not (marker == b'\xe1' and segment.startswith(b'Exif\x00\x00')):
                output.write(fill+marker+size+segment)
            if marker == b'\xda':
                size = os.fstat(source.fileno()).st_size
                if size > MAX_FILE or size < source.tell(): raise ValueError('stamp_file_limit')
                _copy(source, output, size-source.tell())
                if source.read(1): raise ValueError('stamp_source_changed')
                return
        raise ValueError('stamp_container_chunk_limit')


def xmp_payload(existing, data):
    if existing:
        if isinstance(existing, str): existing = existing.encode('utf-8')
        if len(existing) > MAX_METADATA or b'<!DOCTYPE' in existing or b'<!ENTITY' in existing:
            raise ValueError('stamp_invalid_xmp')
        tree = ET.fromstring(existing)
    else:
        tree = ET.Element('{adobe:ns:meta/}xmpmeta')
        rdf = ET.SubElement(tree, RDF+'RDF')
        ET.SubElement(rdf, RDF+'Description', {RDF+'about': ''})
    for parent in tree.iter():
        for child in list(parent):
            if child.tag == PAN: parent.remove(child)
    description = tree.find('.//'+RDF+'Description')
    if description is None: raise ValueError('stamp_invalid_xmp_structure')
    ET.SubElement(description, PAN).text = _json(data)
    # El reader legado usa pan:data; fijar el prefijo explícito sin tocar otros.
    ET.register_namespace('pan', 'http://panopticon/ns/')
    result = ET.tostring(tree, encoding='utf-8')
    if len(result) > MAX_METADATA: raise ValueError('stamp_metadata_limit')
    return result


def _webp(path, output, data):
    with Image.open(path) as image:
        width, height = image.size
        alpha = 'A' in image.getbands() or 'transparency' in image.info
        xmp = xmp_payload(image.info.get('xmp'), data)
    records = []
    with open(path, 'rb') as source:
        header = source.read(12)
        size = os.fstat(source.fileno()).st_size
        if header[:4] != b'RIFF' or header[8:] != b'WEBP' or struct.unpack('<I', header[4:8])[0]+8 != size:
            raise ValueError('stamp_invalid_webp')
        for _ in range(MAX_CHUNKS):
            if source.tell() == size: break
            header = source.read(8)
            if len(header) != 8: raise ValueError('stamp_truncated_webp')
            kind, length = struct.unpack('<4sI', header)
            position = source.tell()
            if position+length+(length & 1) > size: raise ValueError('stamp_truncated_webp')
            records.append((kind, length, position))
            source.seek(position+length)
            if length & 1 and source.read(1) != b'\x00': raise ValueError('stamp_webp_padding')
        else: raise ValueError('stamp_container_chunk_limit')
        kinds = [kind for kind, _, _ in records]
        if any(kinds.count(kind) > 1 for kind in (b'VP8X', b'XMP ', b'EXIF', b'ICCP')) or any(kind in kinds for kind in (b'ANIM', b'ANMF')):
            raise ValueError('stamp_webp_requires_review')
        if sum(kind in (b'VP8 ', b'VP8L') for kind in kinds) != 1:
            raise ValueError('stamp_invalid_webp_image')
        if b'VP8X' in kinds:
            kind, length, position = records[kinds.index(b'VP8X')]
            if length != 10 or kinds[0] != b'VP8X': raise ValueError('stamp_invalid_webp_extended')
            source.seek(position)
            extended = bytearray(source.read(10))
            extended[0] |= 0x04
        else:
            flags = 0x04 | (0x10 if alpha else 0) | (0x08 if b'EXIF' in kinds else 0) | (0x20 if b'ICCP' in kinds else 0)
            extended = bytes((flags, 0, 0, 0))+(width-1).to_bytes(3, 'little')+(height-1).to_bytes(3, 'little')
        body_size = 4+18+sum(8+length+(length & 1) for kind, length, _ in records if kind not in (b'VP8X', b'XMP '))+8+len(xmp)+(len(xmp) & 1)
        if body_size+8 > MAX_FILE: raise ValueError('stamp_file_limit')
        output.write(b'RIFF'+struct.pack('<I', body_size)+b'WEBPVP8X'+struct.pack('<I', 10)+extended)
        for kind, length, position in records:
            if kind in (b'VP8X', b'XMP '): continue
            source.seek(position-8)
            _copy(source, output, 8+length+(length & 1))
        output.write(b'XMP '+struct.pack('<I', len(xmp))+xmp+(b'\x00' if len(xmp) & 1 else b''))


def read_payload(path):
    extension = Path(path).suffix.lower()
    if extension == '.png': return json.loads(read_png(path)['panopticon_data'])
    with Image.open(path) as image:
        if extension in ('.jpg', '.jpeg'):
            value = image.getexif()[0x010e]
            if isinstance(value, bytes): value = value.decode('utf-8')
            return json.loads(value)
        raw = image.info.get('xmp', b'')
        if len(raw) > MAX_METADATA: raise ValueError('stamp_metadata_limit')
        node = ET.fromstring(raw).find('.//'+PAN)
        if node is None: raise ValueError('stamp_payload_missing')
        return json.loads(node.text)


def verify(path, data):
    if read_payload(path) != data: raise ValueError('stamp_verification_failed')
    with Image.open(path) as image:
        if image.width*image.height > 20_000_000 or getattr(image, 'n_frames', 1) != 1:
            raise ValueError('stamp_image_limit')
        image.load()
    return True


def stamp_prepared(path, bundle):
    path = _path(path)
    before, source_digest = _identity(path), _digest(path)
    data = payload(bundle)
    with Image.open(path) as image:
        if image.width*image.height > 20_000_000 or getattr(image, 'n_frames', 1) != 1:
            raise ValueError('stamp_image_limit')
    transforms = {'.png': _png, '.jpg': _jpeg, '.jpeg': _jpeg, '.webp': _webp}
    transform = transforms.get(path.suffix.lower())
    if transform is None: raise ValueError('stamp_format_requires_encoder_options')
    fd, temporary = tempfile.mkstemp(prefix='.panopticon-stamp-', suffix=path.suffix, dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as output:
            transform(path, output, data)
            output.flush()
            if output.tell() > MAX_FILE: raise ValueError('stamp_file_limit')
            os.fsync(output.fileno())
        verify(temporary, data)
        if _path(path) != path or _identity(path) != before or _digest(path) != source_digest:
            raise ValueError('stamp_source_changed')
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
        return True
    finally:
        temporary.unlink(missing_ok=True)
