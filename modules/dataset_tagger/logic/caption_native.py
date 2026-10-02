"""Verified Tagger copies through the Native owner; no SQL or GUI objects."""
import os
from pathlib import Path
from PIL import Image
from core.curation_bindings import valid
from core.derived_publication import DerivedPublication, write
from core.metadata import MetadataExtractor
from core.output_paths import safe_path, path_variants


def normalized(path):
    return os.path.normpath(path).replace('\\', '/')


def signature(path):
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


class CaptionNative:
    def __init__(self, service, source, pair, event, journal_root=None, prepare_schema=None):
        self.service, self.pair, self.event = service, pair, event
        self.journal_root = journal_root
        self.prepare_schema = prepare_schema
        self.publication = None
        self.existing = None
        source = normalized(source)
        rows = service.read('get_curation_snapshot', [source], include_revision=True, cancel_event=event)
        if len(rows) != 1 or not valid(rows[0], source):
            raise ValueError('tagger_native_source_binding_requires_review')
        self.source = rows[0]
        if safe_path(Path(source)) != pair.source:
            raise ValueError('tagger_native_source_path_changed')
        if self.source['id'] is None:
            occupied = service.read('get_metadata_path_status', path_variants(pair.image, pair.image),
                                    cancel_event=event)['occupied']
            if occupied:
                raise ValueError('tagger_external_output_has_native_history')
        elif pair.image_pin is not None:
            rows = service.read('get_curation_snapshot', [str(pair.image)], include_revision=True, cancel_event=event)
            if len(rows) != 1 or not valid(rows[0], normalized(pair.image)):
                raise ValueError('tagger_native_output_binding_requires_review')
            if rows[0]['id'] is not None:
                self.existing = rows[0]
                if self.existing['id'] == self.source['id']:
                    raise ValueError('tagger_native_original_protected')
                self._check_existing()

    def _check_existing(self):
        value = self.service.read('get_copy_version_state', self.existing['id'], cancel_event=self.event)
        row = value['state']['file']
        known = row['hash_mod'] or row['hash_original']
        if (value['fingerprint'] != self.existing['fingerprint'] or row['is_offline'] != 0
                or known != (self.pair.expected_image or self.pair.source_hash)):
            raise ValueError('tagger_native_output_changed; review before exporting')

    def prepare(self, image_temp):
        pair = self.pair
        if self.source['id'] is None:
            if self.service.read('get_metadata_path_status', path_variants(pair.image, pair.image),
                                 cancel_event=self.event)['occupied']:
                raise ValueError('tagger_external_output_has_native_history')
            return
        if self.existing is not None:
            self._check_existing()
            return  # Caption only: preserve this copy's independent Native curation.
        if self.prepare_schema is not None:
            self.prepare_schema()
        else:
            write(self.service, 'prepare_derived_copies', event=self.event)
        prepared = image_temp or pair.image
        with Image.open(prepared) as image:
            if image.width * image.height > 20_000_000 or getattr(image, 'n_frames', 1) != 1:
                raise ValueError('metadata_image_limit')
            dimensions = image.size
            image.load()
        observed = MetadataExtractor.extract(prepared, strict=True)
        request = dict(expected_fingerprint=self.source['fingerprint'], source_path=self.source['path'],
                       path=str(pair.image), destination_paths=path_variants(pair.image, pair.image),
                       producer='dataset_tagger', source_digest=pair.source_hash, result_digest=pair.source_hash,
                       generation=dict(width=dimensions[0], height=dimensions[1], tool=observed.tool,
                                       model=observed.model, positive=observed.positive_prompt,
                                       negative=observed.negative_prompt, seed=str(observed.seed)),
                       observation=dict(filename=pair.image.name, size=prepared.stat().st_size,
                                        created=prepared.stat().st_ctime))
        self.publication = DerivedPublication(self.service, self.source['id'], request,
                                               journal_root=self.journal_root)
        self.publication.ready(signature(pair.source), signature(prepared), prepared, self.event)
        pair.record['native_journal'] = str(self.publication.path)
        pair._record()
        self.publication.scope(self.event)

    def commit(self):
        if self.publication is None:
            return
        pair = self.pair
        # A physical publication has happened; cancellation cannot imply SQL rollback.
        pair.pin.verify(pair.source_hash, pair.source_identity)
        pair.image_pin.verify(pair.source_hash, pair.image_identity)
        info = pair.image.stat()
        self.publication.register(dict(filename=pair.image.name, size=info.st_size, created=info.st_ctime),
                                  signature(pair.image))
        self.publication.complete()
        pair.record['native_copy_id'] = self.publication.receipt['copy_id']
        pair._record()

    def failed(self, error):
        if self.publication is not None:
            self.publication.failed(str(error), self.pair.published_image or
                                     self.pair.published_caption or self.pair.image_pin is not None)
