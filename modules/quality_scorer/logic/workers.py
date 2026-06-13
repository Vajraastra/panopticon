"""
Workers QThread para el Quality Scorer.

SlopFilterWorker   — Fase 1: clasifica imágenes en keeper / review / slop.
QualityRankWorker  — Fase 2: puntúa calidad técnica y ordena por score.
CalibrationWorker  — Modo calibración: analiza una imagen individual con
                     scores raw + pesos + umbrales del preset activo.
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
    finished_signal = Signal(dict)       # summary {keeper:N, review:N, slop:N}
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
        from .slop_filter import get_analyzer, classify
        from core.paths import CachePaths

        try:
            # Cacheado a nivel módulo: corridas repetidas no recargan CLIP
            analyzer = get_analyzer(
                CachePaths.get_models_root(),
                self.content_type,
                use_face      = self.use_face,
                use_body      = self.use_body,
                use_hands     = self.use_hands,
                use_aesthetic = self.use_aesthetic,
            )
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

        self.finished_signal.emit(counts)


class CalibrationWorker(QThread):
    """
    Analiza una imagen individual en modo calibración.
    Corre initialize() + analyze_calibration() en hilo de fondo.
    """
    status   = Signal(str)   # mensaje de progreso para la barra de estado
    finished_signal = Signal(dict)  # scores + _weights + _presets + _face_model
    error    = Signal(str)

    def __init__(self, image_path: str, content_type: str,
                 use_face: bool, use_body: bool,
                 use_hands: bool, use_aesthetic: bool):
        super().__init__()
        self.image_path    = image_path
        self.content_type  = content_type
        self.use_face      = use_face
        self.use_body      = use_body
        self.use_hands     = use_hands
        self.use_aesthetic = use_aesthetic

    def run(self):
        from .slop_filter import get_analyzer
        from core.paths import CachePaths

        self.status.emit("init")
        try:
            # Reutiliza el analyzer de la corrida anterior si la config coincide
            analyzer = get_analyzer(
                CachePaths.get_models_root(),
                self.content_type,
                use_face      = self.use_face,
                use_body      = self.use_body,
                use_hands     = self.use_hands,
                use_aesthetic = self.use_aesthetic,
            )
        except Exception as e:
            self.error.emit(str(e))
            return

        self.status.emit("scoring")
        img = cv2.imread(self.image_path)
        if img is None:
            self.error.emit(f"No se pudo leer la imagen: {self.image_path}")
            return

        try:
            scores = analyzer.analyze_calibration(img)
            self.finished_signal.emit(scores)
        except Exception as e:
            self.error.emit(str(e))


class QualityRankWorker(QThread):
    """
    Fase 2: Puntúa calidad técnica (nitidez, artefactos, resolución, color).
    Emite image_done() por cada imagen y al final una lista ordenada.
    """
    progress   = Signal(int, int, str)   # current, total, filename
    image_done = Signal(str, dict)       # path, scores
    finished_signal = Signal(list)       # lista de dicts ordenada por score desc
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
        self.finished_signal.emit(results)
