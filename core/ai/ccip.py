"""Shared CCIP encoder for EXIF-oriented RGB images and its pinned metric."""

from hashlib import file_digest
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

import numpy as np
from PIL import Image

from core.ai.embeddings import cached_embedding
from core.ai.model_downloader import download_file
from core.paths import CachePaths


IMAGE_SIZE = 384
DIMENSIONS = 768
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
_MODEL_LOCKS = {name: Lock() for name in MODEL_HASHES}
SPACE = f"ccip:{MODEL_HASHES['model_feat.onnx']}:rgb384-l2-v1"
# The pinned metric maps normalized dot products to clipped distances.
SAME_CHARACTER = 0.17847511429108218
_METRIC_SCALE = 29.18642234802246
_METRIC_WEIGHT = 29.186418533325195
_METRIC_DENOMINATOR = 58.37284469604492
MIN_SIMILARITY = (_METRIC_SCALE - _METRIC_DENOMINATOR * SAME_CHARACTER) / _METRIC_WEIGHT
SIMILARITY_MARGIN = _METRIC_DENOMINATOR * 0.025 / _METRIC_WEIGHT


def similarity_surplus(distance):
    """Convert a CCIP metric distance to cosine surplus above the matching threshold."""
    return (SAME_CHARACTER - distance) * _METRIC_DENOMINATOR / _METRIC_WEIGHT


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


def _session(name, check_cancelled):
    with _MODEL_LOCKS[name]:
        check_cancelled()
        model = _verified_model(name)
    check_cancelled()
    import onnxruntime as ort

    return ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])


def metric_session(check_cancelled):
    return _session("model_metrics.onnx", check_cancelled)


class CCIPEncoder:
    """Worker-local encoder. Exact model inputs reuse the shared cache across callers."""

    def __init__(self):
        self.extractor = None

    def embed(self, rgb, check_cancelled):
        check_cancelled()
        resized = rgb.resize((IMAGE_SIZE, IMAGE_SIZE), Image.Resampling.BILINEAR)
        data = np.asarray(resized, dtype=np.float32).transpose(2, 0, 1) / 255.0
        tensor = ((data - MEAN) / STD)[None, ...]

        def infer():
            if self.extractor is None:
                self.extractor = _session("model_feat.onnx", check_cancelled)
            check_cancelled()
            output, = self.extractor.run(["output"], {"input": tensor})
            if output.shape != (1, DIMENSIONS):
                raise ValueError("model_output")
            norm = float(np.linalg.norm(output[0]))
            if not np.isfinite(norm) or norm == 0:
                raise ValueError("model_output")
            check_cancelled()
            return output[0] / norm

        embedding = cached_embedding(SPACE, DIMENSIONS, tensor, infer)
        check_cancelled()
        embedding.setflags(write=False)
        return embedding
