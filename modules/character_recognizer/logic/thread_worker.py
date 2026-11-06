import logging
import threading
from PySide6.QtCore import QThread, Signal
from core.ai.ccip import CCIPEncoder
from .recognition_engine import RecognitionEngine
from .profile_db import ProfileDB
from .copy_bindings import capture, normalized
from .image_analysis import RecognitionCancelled, analyze_file, match_embedding

log = logging.getLogger(__name__)


class RecognitionWorker(QThread):
    image_processed = Signal(str, object, object, str, object, float, object)
    finished_signal = Signal()
    progress = Signal(int, int)
    error = Signal(str)

    def __init__(self, paths, mode='ccip', *, library_service=None):
        super().__init__()
        self.paths = paths
        self.mode = mode
        self.library_service = library_service
        self._cancel = threading.Event()
        self.is_running = True
        self.engine = CCIPEncoder() if mode == 'ccip' else RecognitionEngine()
        self.db = ProfileDB(mode='ccip') if mode == 'ccip' else ProfileDB()
        self.current_idx = 0
        self.paused = True
        self._override_next_idx = None  # for go_back navigation

    def run(self):
        try:
            self._run()
        except Exception as exc:
            self.error.emit(str(exc) or type(exc).__name__)
        finally:
            self.finished_signal.emit()

    def _run(self):
        if not self.is_running:
            return
        log.debug(f"Worker started with {len(self.paths)} items.")
        try:
            if self.mode != 'ccip':
                self.engine.initialize()
        except Exception as e:
            self.error.emit(str(e))
            return
        log.debug("Starting recognition loop in %s mode.", self.mode)

        while self.is_running and self.current_idx < len(self.paths):
            if self.paused:
                self.msleep(50)
                continue

            path = self.paths[self.current_idx]
            log.debug(f"Processing index {self.current_idx}: {path}")

            # Fallo de perfiles termina visiblemente; no saltar imágenes como si no hubiera match.
            bindings = capture(self.library_service,[path],running=lambda:self.is_running,
                cancel_event=self._cancel) if self.library_service else None
            if not self.is_running:
                return
            binding = bindings[normalized(path)] if bindings is not None else None
            profiles = self.db.get_all_profiles()

            try:
                img, embedding, bbox, det_score = analyze_file(
                    path, self.engine, self.mode, preview=True, cancel_event=self._cancel,
                )

                if img is None:
                    log.warning(f"Failed to load image: {path}")
                    self._advance()
                    continue

                log.debug(f"Analysis complete. Score: {det_score}")

                suggestion, final_confidence = match_embedding(self.engine, embedding, profiles, self.mode)
                self.image_processed.emit(path, img, embedding, suggestion, bbox, final_confidence, binding)

                self.paused = True
                while self.paused and self.is_running:
                    self.msleep(50)

                self._advance()
                self.progress.emit(self.current_idx, len(self.paths))

            except RecognitionCancelled:
                return
            except Exception as e:
                if self.mode == 'ccip':
                    raise
                log.error(f"Worker error on {path}: {e}")
                self._advance()


    def _advance(self):
        """Avanza al siguiente índice, respetando go_back si fue solicitado."""
        if self._override_next_idx is not None:
            self.current_idx = self._override_next_idx
            self._override_next_idx = None
        else:
            self.current_idx += 1

    def request_next(self):
        self.paused = False

    def stop(self):
        self.is_running = False
        self._cancel.set()

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
    finished_signal = Signal()
    error = Signal(str)
    bindings_ready = Signal(object)

    DOMINANCE_THRESHOLD = 0.60  # 60% de las imágenes deben coincidir

    def __init__(self, paths, mode='ccip', *, library_service=None):
        super().__init__()
        self.paths = paths
        self.mode = mode
        self.library_service = library_service
        self._cancel = threading.Event()
        self.engine = CCIPEncoder() if mode == 'ccip' else RecognitionEngine()
        self.db = ProfileDB(mode='ccip') if mode == 'ccip' else ProfileDB()
        self.is_running = True

    def run(self):
        try:
            self._run()
        except Exception as exc:
            self.error.emit(str(exc) or type(exc).__name__)
        finally:
            self.finished_signal.emit()

    def _run(self):
        bindings = capture(self.library_service,self.paths,running=lambda:self.is_running,
            cancel_event=self._cancel) if self.library_service else None
        if not self.is_running:
            return
        self.bindings_ready.emit(bindings or {})
        try:
            if self.mode != 'ccip':
                self.engine.initialize()
        except Exception as e:
            self.error.emit(str(e))
            return
        profiles = self.db.get_all_profiles()
        total = len(self.paths)
        tally = {}  # name -> count

        for i, path in enumerate(self.paths):
            if not self.is_running:
                break
            self.progress.emit(i + 1, total)
            try:
                _, embedding, _, _ = analyze_file(path, self.engine, self.mode, cancel_event=self._cancel)
                best_name, _ = match_embedding(self.engine, embedding, profiles, self.mode)
                if best_name:
                    tally[best_name] = tally.get(best_name, 0) + 1

            except RecognitionCancelled:
                return
            except Exception as e:
                if self.mode == 'ccip':
                    raise
                log.error(f"AutoScan error on {path}: {e}")

        if not self.is_running:
            return
        if tally:
            top_name = max(tally, key=tally.get)
            top_count = tally[top_name]
            pct = top_count / total
            if pct >= self.DOMINANCE_THRESHOLD:
                self.suggestion.emit(top_name, pct, top_count)
                return

        self.no_match.emit()

    def stop(self):
        self.is_running = False
        self._cancel.set()
