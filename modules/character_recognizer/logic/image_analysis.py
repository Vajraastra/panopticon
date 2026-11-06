"""Decode and match the same image inputs in all Recognizer entry points."""

import logging

import cv2
import numpy as np
from PIL.Image import DecompressionBombError

from core.ai import ccip
from core.ai.arcface import MATCH_THRESHOLD
from core.ai.image_input import read_rgb


log = logging.getLogger(__name__)


class RecognitionCancelled(Exception):
    pass


def analyze_file(path, engine, mode, *, preview=False, cancel_event=None):
    if mode == "ccip":
        def check_cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise RecognitionCancelled

        check_cancelled()
        try:
            rgb = read_rgb(path)
        except (OSError, ValueError, SyntaxError, DecompressionBombError) as error:
            log.debug("Could not read recognition image %s: %s", path, error)
            return None, None, None, 0.0
        with rgb:
            embedding = engine.embed(rgb, check_cancelled)
            image = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR) if preview else None
        return image, embedding, None, 0.0

    with open(path, "rb") as stream:
        image = cv2.imdecode(np.frombuffer(stream.read(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None, None, None, 0.0
    if mode == "illustration":
        embedding, box, confidence = engine.analyze_image(image)
    else:
        embedding, box, confidence = engine.analyze_image(image, mode=mode)
    return image, embedding, box, confidence


def match_embedding(engine, embedding, profiles, mode):
    if embedding is None:
        return None, 0.0
    if mode == "ccip":
        scores = {}
        for name, reference in profiles:
            if reference.shape != embedding.shape:
                continue
            # Profiles may contain a mean of several normalized vectors.
            norm = float(np.linalg.norm(reference))
            if np.isfinite(norm) and norm > 0:
                similarity = float(np.dot(embedding, reference) / norm)
                scores[name] = max(scores.get(name, -np.inf), similarity)
        if scores:
            winner = max(scores, key=scores.get)
            score = scores.pop(winner)
            runner_up = max(scores.values(), default=-np.inf)
            if score >= ccip.MIN_SIMILARITY and score - runner_up >= ccip.SIMILARITY_MARGIN:
                return winner, score
        return None, 0.0

    winner, score = None, MATCH_THRESHOLD
    for name, reference in profiles:
        similarity = engine.compare(embedding, reference)
        if similarity > score:
            winner, score = name, similarity
    return winner, score if winner is not None else 0.0
