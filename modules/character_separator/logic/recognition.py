"""Discover character groups and compare them with user-approved CCIP references."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from io import BytesIO
import logging
import os
from pathlib import Path

import numpy as np
from PIL import Image

from core.ai.ccip import (
    CCIPEncoder, MIN_SIMILARITY, SAME_CHARACTER, SIMILARITY_MARGIN, metric_session, similarity_surplus,
)
from core.ai.image_input import read_rgb
from .export import FileSignature, IMAGE_EXTENSIONS, file_signature, signature


log = logging.getLogger(__name__)
MAX_IMAGES = 1000
METRIC_BLOCK = 128
CLASSIFY_BLOCK = 128
SCORE_BYTES = 8 * 1024 * 1024
THUMBNAIL_SIZE = 176


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
    match_strengths: dict[Path, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Classification:
    matches: dict[Path, str]
    signatures: dict[Path, FileSignature]
    failed: tuple[Path, ...]
    processed: int


def _check_cancelled(cancelled):
    if cancelled():
        raise ScanCancelled()


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


def read_image(path, before):
    """Read the oriented RGB image only if it matches the expected file signature."""
    with path.open("rb") as stream:
        if signature(os.fstat(stream.fileno())) != before:
            raise ValueError("source_changed")
        rgb = read_rgb(stream)
        if signature(os.fstat(stream.fileno())) != before:
            raise ValueError("source_changed")
    return rgb


class _Sampler:
    def __init__(self, cancelled, cache):
        self.cancelled = cancelled
        self.cache = cache
        self.encoder = CCIPEncoder()

    def read(self, path, *, thumbnail):
        _check_cancelled(self.cancelled)
        try:
            before = file_signature(path)
            sample = self.cache.get(path) if self.cache is not None else None
            if sample is not None and sample.signature != before:
                sample = None
            if sample is not None and not thumbnail:
                return sample, b""
            rgb = read_image(path, before)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            log.debug("CCIP could not read %s", path, exc_info=True)
            return None

        with rgb:
            if sample is None:
                sample = Sample(before, self.encoder.embed(rgb, lambda: _check_cancelled(self.cancelled)))
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

    def match(self, paths, samples, cancelled, *, strengths=None):
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
            gap = best - scores.max(axis=1)
            clear = (best >= MIN_SIMILARITY) & (gap >= SIMILARITY_MARGIN)
            if strengths is not None:
                surplus = np.minimum(best - MIN_SIMILARITY, gap - SIMILARITY_MARGIN)
            for index, sample in enumerate(batch):
                name = self.anchors.get(sample.signature)
                if name is not None or clear[index]:
                    path = paths[start + index]
                    matches[path] = name if name is not None else self.names[winners[index]]
                    if strengths is not None:
                        strengths[path] = float("inf") if name is not None else float(surplus[index])
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
    strengths = {}
    matches = _ReferenceSet(references).match(paths, samples, cancelled, strengths=strengths) if references else {}
    unknown = [index for index, path in enumerate(paths) if path not in matches]
    if len(samples) > 1:
        vectors = np.stack([sample.embedding for sample in samples])
        distances = _distances(metric_session(lambda: _check_cancelled(cancelled)), vectors, cancelled)
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
        match_strengths=strengths,
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


def ordered_review_groups(result: ScanResult) -> tuple[tuple[tuple[int, ...], str], ...]:
    """Review stronger groups first, using the least certain member or pair as evidence."""
    suggestions = {}
    for index, path in enumerate(result.paths):
        if path in result.matches:
            suggestions.setdefault(result.matches[path], []).append(index)

    ranked = []
    for name, members in suggestions.items():
        strength = min(result.match_strengths.get(result.paths[index], 0.0) for index in members)
        ranked.append((strength, tuple(members), name))
    for group in result.groups:
        strength = float("-inf")
        if len(group) > 1:
            worst_distance = float(result.distances[np.ix_(group, group)].max())
            strength = similarity_surplus(worst_distance)
        ranked.append((strength, group, ""))

    ranked.sort(key=lambda entry: (-entry[0], -len(entry[1]), entry[1][0]))
    return tuple((group, name) for _, group, name in ranked)


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
