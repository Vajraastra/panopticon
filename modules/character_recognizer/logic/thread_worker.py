import logging
from PySide6.QtCore import QThread, Signal
import cv2
import numpy as np
from .recognition_engine import RecognitionEngine
from .profile_db import ProfileDB

log = logging.getLogger(__name__)


class RecognitionWorker(QThread):
    image_processed = Signal(str, object, object, str, object, float)
    finished = Signal()
    progress = Signal(int, int)

    def __init__(self, paths, mode='illustration'):
        super().__init__()
        self.paths = paths
        self.mode = mode
        self.is_running = True
        self.engine = RecognitionEngine()
        self.db = ProfileDB()
        self.current_idx = 0
        self.paused = True
        self._override_next_idx = None  # for go_back navigation

    def run(self):
        log.debug(f"Worker started with {len(self.paths)} items.")
        self.engine.initialize()
        log.debug("Engine initialized. Starting loop.")

        while self.is_running and self.current_idx < len(self.paths):
            if self.paused:
                self.msleep(50)
                continue

            path = self.paths[self.current_idx]
            log.debug(f"Processing index {self.current_idx}: {path}")

            try:
                with open(path, "rb") as stream:
                    numpyarray = np.asarray(bytearray(stream.read()), dtype=np.uint8)
                img = cv2.imdecode(numpyarray, cv2.IMREAD_COLOR)

                if img is None:
                    log.warning(f"Failed to load image: {path}")
                    self._advance()
                    continue

                embedding, bbox, det_score = self.engine.analyze_image(img, mode=self.mode)
                log.debug(f"Analysis complete. Score: {det_score}")

                suggestion = None
                max_score = 0

                if embedding is not None:
                    profiles = self.db.get_all_profiles()
                    for name, ref_emb in profiles:
                        score = self.engine.compare(embedding, ref_emb)
                        if score > max_score:
                            max_score = score
                            if score > 0.6:
                                suggestion = name

                final_confidence = max_score if suggestion else 0.0
                self.image_processed.emit(path, img, embedding, suggestion, bbox, final_confidence)

                self.paused = True
                while self.paused and self.is_running:
                    self.msleep(50)

                self._advance()
                self.progress.emit(self.current_idx, len(self.paths))

            except Exception as e:
                log.error(f"Worker error on {path}: {e}")
                self._advance()

        self.finished.emit()

    def _advance(self):
        """Avanza al siguiente índice, respetando go_back si fue solicitado."""
        if self._override_next_idx is not None:
            self.current_idx = self._override_next_idx
            self._override_next_idx = None
        else:
            self.current_idx += 1

    def request_next(self):
        self.paused = False

    def pause(self):
        self.paused = True

    def go_back(self):
        """Retrocede a la imagen anterior."""
        # current_idx apunta a la imagen actual (worker pausado)
        # _advance() se llama tras el unpause: si override = N-1, queda en N-1
        target = max(0, self.current_idx - 1)
        self._override_next_idx = target
        self.paused = False


class AutoScanWorker(QThread):
    """Pre-escanea todas las imágenes y detecta si hay un personaje dominante."""
    progress = Signal(int, int)                  # current, total
    suggestion = Signal(str, float, int)         # name, pct (0-1), count
    no_match = Signal()
    finished = Signal()

    DOMINANCE_THRESHOLD = 0.60  # 60% de las imágenes deben coincidir

    def __init__(self, paths, mode='illustration'):
        super().__init__()
        self.paths = paths
        self.mode = mode
        self.engine = RecognitionEngine()
        self.db = ProfileDB()
        self.is_running = True

    def run(self):
        self.engine.initialize()
        profiles = self.db.get_all_profiles()
        total = len(self.paths)
        tally = {}  # name -> count

        for i, path in enumerate(self.paths):
            if not self.is_running:
                break
            self.progress.emit(i + 1, total)
            try:
                with open(path, "rb") as f:
                    arr = np.asarray(bytearray(f.read()), dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is None:
                    continue

                embedding, _, _ = self.engine.analyze_image(img, mode=self.mode)
                if embedding is None:
                    continue

                best_name, best_score = None, 0.0
                for name, ref_emb in profiles:
                    score = self.engine.compare(embedding, ref_emb)
                    if score > best_score:
                        best_score = score
                        if score > 0.6:
                            best_name = name

                if best_name:
                    tally[best_name] = tally.get(best_name, 0) + 1

            except Exception as e:
                log.error(f"AutoScan error on {path}: {e}")

        if tally:
            top_name = max(tally, key=tally.get)
            top_count = tally[top_name]
            pct = top_count / total
            if pct >= self.DOMINANCE_THRESHOLD:
                self.suggestion.emit(top_name, pct, top_count)
                self.finished.emit()
                return

        self.no_match.emit()
        self.finished.emit()
