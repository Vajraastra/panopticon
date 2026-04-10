"""
Workers QThread para el Quality Scorer.

SlopFilterWorker  — Fase 1: clasifica imágenes en keeper / review / slop.
QualityRankWorker — Fase 2: puntúa calidad técnica y ordena por score.
"""
import logging
import cv2
from pathlib import Path
from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)


class SlopFilterWorker(QThread):
    """
    Fase 1: Analiza anatomía y estética de cada imagen.
    Emite image_done() por cada imagen procesada.
    """
    progress   = Signal(int, int, str)   # current, total, filename
    image_done = Signal(str, str, dict)  # path, label, scores
    finished   = Signal(dict)            # summary {keeper:N, review:N, slop:N}
    error      = Signal(str)

    def __init__(self, paths: list, preset: str, content_type: str,
                 use_face: bool, use_body: bool,
                 use_hands: bool, use_aesthetic: bool):
        super().__init__()
        self.paths         = paths
        self.preset        = preset
        self.content_type  = content_type
        self.use_face      = use_face
        self.use_body      = use_body
        self.use_hands     = use_hands
        self.use_aesthetic = use_aesthetic
        self._running      = True

    def stop(self):
        self._running = False

    def run(self):
        from .slop_filter import SlopAnalyzer, classify
        from core.paths import CachePaths

        models_dir = CachePaths.get_models_root()
        analyzer   = SlopAnalyzer(
            models_dir,
            content_type  = self.content_type,
            use_face      = self.use_face,
            use_body      = self.use_body,
            use_hands     = self.use_hands,
            use_aesthetic = self.use_aesthetic,
        )

        try:
            analyzer.initialize()
        except Exception as e:
            log.error(f"[SlopWorker] Error inicializando modelos: {e}")
            self.error.emit(str(e))
            return

        total  = len(self.paths)
        counts = {"keeper": 0, "review": 0, "slop": 0}

        for i, path in enumerate(self.paths):
            if not self._running:
                break
            try:
                img = cv2.imread(str(path))
                if img is None:
                    log.debug(f"[SlopWorker] No se pudo leer: {path}")
                    continue

                scores = analyzer.analyze(img)
                label  = classify(scores, self.preset, self.content_type)
                counts[label] += 1

                self.image_done.emit(str(path), label, scores)
                self.progress.emit(i + 1, total, Path(path).name)

            except Exception as e:
                log.warning(f"[SlopWorker] Error en {path}: {e}")

        self.finished.emit(counts)


class QualityRankWorker(QThread):
    """
    Fase 2: Puntúa calidad técnica (nitidez, artefactos, resolución, color).
    Emite image_done() por cada imagen y al final una lista ordenada.
    """
    progress   = Signal(int, int, str)   # current, total, filename
    image_done = Signal(str, dict)       # path, scores
    finished   = Signal(list)            # lista de dicts ordenada por score desc
    error      = Signal(str)

    def __init__(self, paths: list, profile: str):
        super().__init__()
        self.paths    = paths
        self.profile  = profile
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        from .quality_scorer import score_image

        total   = len(self.paths)
        results = []

        for i, path in enumerate(self.paths):
            if not self._running:
                break
            try:
                img_path = str(path)
                scores   = score_image(img_path, self.profile)
                results.append(scores)
                self.image_done.emit(img_path, scores)
                self.progress.emit(i + 1, total, Path(path).name)
            except Exception as e:
                log.warning(f"[RankWorker] Error en {path}: {e}")

        results.sort(key=lambda x: x.get("composite_score", 0), reverse=True)
        self.finished.emit(results)
