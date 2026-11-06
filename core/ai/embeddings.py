"""Disposable embeddings keyed by their exact model input and encoder space."""

from collections.abc import Callable
from contextlib import ExitStack, closing
from hashlib import sha256
import logging
import sqlite3

import numpy as np

from core.paths import CachePaths


log = logging.getLogger(__name__)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_vectors_v1 (
    space TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    model_input BLOB NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (space, dimensions, model_input)
) WITHOUT ROWID
"""


def cached_embedding(space: str, dimensions: int, model_input: np.ndarray,
                     compute: Callable[[], np.ndarray]) -> np.ndarray:
    """Return a read-only float32 vector. Space identifies weights and preprocessing."""
    if (not isinstance(space, str) or not space or type(dimensions) is not int or dimensions < 1
            or not isinstance(model_input, np.ndarray) or model_input.dtype.hasobject):
        raise ValueError("invalid_embedding_key")
    fingerprint = sha256(repr((model_input.dtype.descr, model_input.shape)).encode("utf-8") + b"\0")
    fingerprint.update(model_input.ravel().view(np.uint8))
    key = space, dimensions, fingerprint.digest()

    with ExitStack() as resources:
        try:
            path = CachePaths.get_tool_cache("embeddings") / "vectors.db"
            connection = resources.enter_context(closing(
                sqlite3.connect(path, timeout=0.02, isolation_level=None),
            ))
            connection.execute(_SCHEMA)
            row = connection.execute(
                "SELECT vector FROM embedding_vectors_v1 WHERE space=? AND dimensions=? AND model_input=?", key,
            ).fetchone()
        except (OSError, sqlite3.Error) as error:
            log.debug("Embedding cache unavailable: %s", error)
            connection = row = None

        if row is not None and isinstance(row[0], bytes) and len(row[0]) == dimensions * 4:
            vector = np.frombuffer(row[0], dtype="<f4")
            if np.isfinite(vector).all():
                return vector

        vector = compute()
        if (not isinstance(vector, np.ndarray) or vector.dtype != np.float32
                or vector.shape != (dimensions,) or not np.isfinite(vector).all()):
            raise ValueError("invalid_embedding_vector")
        payload = vector.astype("<f4", copy=False).tobytes()
        if connection is not None:
            try:
                connection.execute(
                    "INSERT INTO embedding_vectors_v1 VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(space, dimensions, model_input) DO UPDATE SET vector=excluded.vector",
                    (*key, payload),
                )
            except sqlite3.Error as error:
                log.debug("Embedding cache write failed: %s", error)
        return np.frombuffer(payload, dtype="<f4")
