"""Discover character groups and compare them with user-approved CCIP references."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import file_digest
from io import BytesIO
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from PIL import Image, ImageOps

from core.ai.model_downloader import download_file
from core.paths import CachePaths
from .export import FileSignature, IMAGE_EXTENSIONS, file_signature, signature


log = logging.getLogger(__name__)
MAX_IMAGES = 1000
MAX_PIXELS = 50_000_000
SAME_CHARACTER = 0.17847511429108218
IDENTITY_MARGIN = 0.025
METRIC_BLOCK = 128
CLASSIFY_BLOCK = 128
SCORE_BYTES = 8 * 1024 * 1024
IMAGE_SIZE = 384
THUMBNAIL_SIZE = 176
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)[:, None, None]
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)[:, None, None]
MODEL_URL = (
    "https://huggingface.co/deepghs/ccip_onnx/resolve/eb2acdd/"
    "ccip-caformer-24-randaug-pruned"
)
MODEL_HASHES = {
    "model_feat.onnx": "4ea118d16496274f4f6e08d3afc768cc592389e8f7f32f8732ce2215c228ac5f",
    "model_metrics.onnx": "7e4646fdfbe369ad485b227ec47ac4092c98b8deef3af44e19fdbd8ba7bc25c1",
}
# The pinned CCIP metric maps normalized dot products to clipped distances.
METRIC_SCALE = 29.18642234802246
METRIC_WEIGHT = 29.186418533325195
METRIC_DENOMINATOR = 58.37284469604492
MIN_SIMILARITY = (METRIC_SCALE - METRIC_DENOMINATOR * SAME_CHARACTER) / METRIC_WEIGHT
SIMILARITY_MARGIN = METRIC_DENOMINATOR * IDENTITY_MARGIN / METRIC_WEIGHT


class ScanCancelled(Exception):
    """The user cancelled before the scan published a complete result."""


@dataclass(frozen=True)
class Sample:
    """File identity and its normalized CCIP embedding."""

    signature: FileSignature
    embedding: np.ndarray


@dataclass(frozen=True)
class ScanResult:
    paths: tuple[Path, ...]
    distances: np.ndarray
    groups: tuple[tuple[int, ...], ...]
    failed: tuple[Path, ...]
    signatures: tuple[FileSignature, ...]
    thumbnails: tuple[bytes, ...]
    excluded: int
    samples: tuple[Sample, ...]
    matches: dict[Path, str]


@dataclass(frozen=True)
class Classification:
    matches: dict[Path, str]
    signatures: dict[Path, FileSignature]
    failed: tuple[Path, ...]
    processed: int


def _check_cancelled(cancelled):
    if cancelled():
        raise ScanCancelled()


def _digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _verified_model(name):
    root = CachePaths.get_models_root() / "onnx" / "ccip"
    model = root / name
    expected = MODEL_HASHES[name]
    if model.is_file() and _digest(model) == expected:
        return model

    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=root) as temp:
        staged = Path(temp) / name
        download_file(f"{MODEL_URL}/{name}", staged, timeout=30)
        if _digest(staged) != expected:
            raise ValueError("model_checksum")
        os.replace(staged, model)
    return model


def _session(name, cancelled):
    _check_cancelled(cancelled)
    model = _verified_model(name)
    _check_cancelled(cancelled)
    import onnxruntime as ort

    return ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])


def _folder_images(folder, excluded_paths, cancelled):
    excluded = set(excluded_paths)
    paths = []
    with os.scandir(folder) as entries:
        for entry in entries:
            _check_cancelled(cancelled)
            path = Path(entry.path)
            if (
                path not in excluded
                and path.suffix.lower() in IMAGE_EXTENSIONS
                and entry.is_file(follow_symlinks=False)
            ):
                paths.append(path)
    paths.sort(key=lambda path: (path.name.casefold(), path.name))
    return paths


def _thumbnail(rgb):
    try:
        rgb.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.Resampling.BILINEAR)
        with BytesIO() as buffer:
            rgb.save(buffer, format="JPEG", quality=80)
            return buffer.getvalue()
    except OSError:
        return b""


def _read_image(path, before):
    with path.open("rb") as stream:
        if signature(os.fstat(stream.fileno())) != before:
            raise ValueError("source_changed")
        with Image.open(stream) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("image_size")
            rgb = ImageOps.exif_transpose(image).convert("RGB")
        if signature(os.fstat(stream.fileno())) != before:
            raise ValueError("source_changed")
    return rgb


def _model_input(rgb):
    resized = rgb.resize((IMAGE_SIZE, IMAGE_SIZE), Image.Resampling.BILINEAR)
    data = np.asarray(resized, dtype=np.float32).transpose(2, 0, 1) / 255.0
    return (data - MEAN) / STD


class _Sampler:
    def __init__(self, cancelled, cache):
        self.cancelled = cancelled
        self.cache = cache
        self.extractor = None

    def _extract(self, rgb):
        if self.extractor is None:
            self.extractor = _session("model_feat.onnx", self.cancelled)
        _check_cancelled(self.cancelled)
        data = _model_input(rgb)
        output, = self.extractor.run(["output"], {"input": data[None, ...]})
        if output.shape != (1, 768):
            raise ValueError("model_output")
        norm = float(np.linalg.norm(output[0]))
        if not np.isfinite(norm) or norm == 0:
            raise ValueError("model_output")
        embedding = output[0] / norm
        embedding.setflags(write=False)
        return embedding

    def read(self, path, *, thumbnail):
        _check_cancelled(self.cancelled)
        try:
            before = file_signature(path)
            sample = self.cache.get(path) if self.cache is not None else None
            if sample is not None and sample.signature != before:
                sample = None
            if sample is not None and not thumbnail:
                return sample, b""
            rgb = _read_image(path, before)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            log.debug("CCIP could not read %s", path, exc_info=True)
            return None

        if sample is None:
            sample = Sample(before, self._extract(rgb))
        preview = _thumbnail(rgb) if thumbnail else b""
        try:
            if file_signature(path) != before:
                return None
        except (OSError, ValueError):
            return None
        if self.cache is not None:
            self.cache[path] = sample
        return sample, preview


def _distances(metric, embeddings, cancelled):
    count = len(embeddings)
    distances = np.zeros((count, count), dtype=np.float32)
    for left in range(0, count, METRIC_BLOCK):
        left_end = min(left + METRIC_BLOCK, count)
        left_batch = embeddings[left:left_end]
        for right in range(left, count, METRIC_BLOCK):
            _check_cancelled(cancelled)
            right_end = min(right + METRIC_BLOCK, count)
            if left == right:
                inputs = left_batch
                start = 0
            else:
                inputs = np.concatenate((left_batch, embeddings[right:right_end]))
                start = len(left_batch)
            matrix, = metric.run(["output"], {"input": inputs})
            block = np.maximum(
                matrix[:len(left_batch), start:], matrix[start:, :len(left_batch)].T
            )
            distances[left:left_end, right:right_end] = block
            distances[right:right_end, left:left_end] = block.T

    if not np.isfinite(distances).all():
        raise ValueError("model_output")
    np.maximum(distances, 0, out=distances)
    np.fill_diagonal(distances, 0)
    distances.setflags(write=False)
    return distances


def _groups(distances):
    if len(distances) < 2:
        return ((0,),) if len(distances) else ()
    from sklearn.cluster import AgglomerativeClustering

    labels = AgglomerativeClustering(
        n_clusters=None,
        metric="precomputed",
        linkage="complete",
        distance_threshold=SAME_CHARACTER,
    ).fit_predict(distances)
    members = {}
    for index, label in enumerate(labels):
        members.setdefault(label, []).append(index)
    return tuple(tuple(group) for group in sorted(members.values(), key=lambda group: (-len(group), group[0])))


class _ReferenceSet:
    def __init__(self, references):
        names, vectors, starts = [], [], []
        self.anchors = {}
        for name, samples in references.items():
            if not name or not samples:
                raise ValueError("invalid_identity")
            starts.append(len(vectors))
            names.append(name)
            for sample in samples:
                if self.anchors.setdefault(sample.signature, name) != name:
                    raise ValueError("invalid_identity")
                vectors.append(sample.embedding)
        if not vectors:
            raise ValueError("invalid_identity")
        self.names = names
        self.vectors = np.stack(vectors)
        self.starts = np.asarray(starts, dtype=np.intp)
        # The similarity block and its per-identity maxima are both bounded.
        bytes_per_row = 2 * self.vectors.dtype.itemsize * len(vectors)
        self.batch_size = max(1, min(CLASSIFY_BLOCK, SCORE_BYTES // bytes_per_row))

    def match(self, paths, samples, cancelled):
        matches = {}
        for start in range(0, len(samples), self.batch_size):
            _check_cancelled(cancelled)
            batch = samples[start:start + self.batch_size]
            vectors = np.stack([sample.embedding for sample in batch])
            similarities = vectors @ self.vectors.T
            # Reduce by name before computing the runner-up. Two styles of
            # the same identity should never count as competing identities.
            scores = np.maximum.reduceat(similarities, self.starts, axis=1)
            winners = scores.argmax(axis=1)
            rows = np.arange(len(batch))
            best = scores[rows, winners]
            scores[rows, winners] = -np.inf
            clear = (best >= MIN_SIMILARITY) & (best - scores.max(axis=1) >= SIMILARITY_MARGIN)
            for index, sample in enumerate(batch):
                name = self.anchors.get(sample.signature)
                if name is not None or clear[index]:
                    matches[paths[start + index]] = name if name is not None else self.names[winners[index]]
        return matches


def scan(folder: Path, cancelled: Callable[[], bool], progress: Callable[[int, int], None], *,
         excluded_paths=(), references=None, cache=None) -> ScanResult:
    """Review one bounded batch and propose only identities not already matched."""
    paths, samples, failed, thumbnails = [], [], [], []
    candidates = _folder_images(folder, excluded_paths, cancelled)
    excluded = max(0, len(candidates) - MAX_IMAGES)
    candidates = candidates[:MAX_IMAGES]
    sampler = _Sampler(cancelled, cache)
    progress(0, len(candidates))
    for index, path in enumerate(candidates, start=1):
        result = sampler.read(path, thumbnail=True)
        if result is None:
            failed.append(path)
        else:
            sample, thumbnail = result
            paths.append(path)
            samples.append(sample)
            thumbnails.append(thumbnail)
        progress(index, len(candidates))

    _check_cancelled(cancelled)
    matches = _ReferenceSet(references).match(paths, samples, cancelled) if references else {}
    unknown = [index for index, path in enumerate(paths) if path not in matches]
    if len(samples) > 1:
        vectors = np.stack([sample.embedding for sample in samples])
        distances = _distances(_session("model_metrics.onnx", cancelled), vectors, cancelled)
    else:
        distances = np.zeros((len(paths), len(paths)), dtype=np.float32)
        distances.setflags(write=False)
    unknown_distances = distances[np.ix_(unknown, unknown)] if matches else distances
    groups = tuple(tuple(unknown[index] for index in group) for group in _groups(unknown_distances))
    _check_cancelled(cancelled)
    return ScanResult(
        paths=tuple(paths),
        distances=distances,
        groups=groups,
        failed=tuple(failed),
        signatures=tuple(sample.signature for sample in samples),
        thumbnails=tuple(thumbnails),
        excluded=excluded,
        samples=tuple(samples),
        matches=matches,
    )


def classify(folder: Path, references: dict[str, tuple[Sample, ...]], excluded_paths,
             cancelled: Callable[[], bool], progress: Callable[[int, int], None], *,
             cache: dict[Path, Sample] | None = None) -> Classification:
    """Compare every eligible file against approved references without discovery."""
    _check_cancelled(cancelled)
    approved = _ReferenceSet(references)
    candidates = _folder_images(folder, excluded_paths, cancelled)
    sampler = _Sampler(cancelled, cache)
    matches, signatures, failed = {}, {}, []
    total = len(candidates)
    progress(0, total)
    for start in range(0, total, approved.batch_size):
        paths, samples = [], []
        batch = candidates[start:start + approved.batch_size]
        for position, path in enumerate(batch, start=start + 1):
            result = sampler.read(path, thumbnail=False)
            if result is None:
                failed.append(path)
            else:
                sample, _ = result
                paths.append(path)
                samples.append(sample)
                signatures[path] = sample.signature
            progress(position, total)
        matches.update(approved.match(paths, samples, cancelled))
    _check_cancelled(cancelled)
    return Classification(matches, signatures, tuple(failed), total)


def representatives(result: ScanResult, group: Sequence[int], limit: int = 4) -> tuple[int, ...]:
    """Choose a central example, then show variety among the remaining examples."""
    if not group or limit < 1:
        return ()
    remaining = list(group)
    medoid = min(remaining, key=lambda index: (result.distances[index, remaining].mean(), index))
    selected = [medoid]
    remaining.remove(medoid)
    while remaining and len(selected) < limit:
        candidate = max(remaining, key=lambda index: (result.distances[index, selected].min(), -index))
        selected.append(candidate)
        remaining.remove(candidate)
    return tuple(selected)
