"""
Slop Filter — Fase 1 del Quality Scorer.

Evalúa calidad anatómica y estética de imágenes generadas por IA.
Soporta tres tipos de contenido con pesos y umbrales distintos:

  photorealistic  — YuNet + YOLOv8-pose + MediaPipe + CLIP
  3d_render       — YuNet (umbral relajado) + YOLOv8 + MediaPipe + CLIP
  illustration    — lbpcascade_animeface + YOLOv8 (peso bajo) + MediaPipe + CLIP (dominante)

Todos los scorers retornan 0.0–1.0.
Clasificación final ternaria: keeper / review / slop.
"""
import logging
import cv2
import numpy as np
from pathlib import Path

log = logging.getLogger(__name__)

LABEL_KEEPER = "keeper"
LABEL_REVIEW = "review"
LABEL_SLOP   = "slop"

# ============================================================================
# CONFIGURACIÓN POR TIPO DE CONTENIDO
# Cada entrada define pesos de los 4 scorers y umbrales por preset.
# Los umbrales son más bajos para estilos donde los modelos (entrenados con
# fotos reales) producen scores naturalmente menores.
# ============================================================================
CONTENT_TYPES = {
    "photorealistic": {
        "label":      "Fotorrealista",
        "face_model": "yunet",   # detector realista preciso
        "weights": {
            "face":      0.35,
            "body":      0.25,
            "hands":     0.25,
            "aesthetic": 0.15,
        },
        "presets": {
            "strict":   {"keeper": 0.70, "review": 0.50},
            "balanced": {"keeper": 0.55, "review": 0.35},
            "lenient":  {"keeper": 0.35, "review": 0.20},
        },
    },
    "3d_render": {
        "label":      "3D Render",
        # lbpcascade reconoce bien rostros de renders estilizados (confirmado por usuario)
        "face_model": "anime",
        "weights": {
            "face":      0.30,
            "body":      0.25,
            "hands":     0.20,
            "aesthetic": 0.25,   # más peso estético — renders suelen ser visualmente ricos
        },
        "presets": {
            # Umbrales ~10% menores: modelos ven renders como "casi reales"
            "strict":   {"keeper": 0.62, "review": 0.44},
            "balanced": {"keeper": 0.48, "review": 0.30},
            "lenient":  {"keeper": 0.28, "review": 0.16},
        },
    },
    "illustration": {
        "label":      "Ilustración / Anime",
        "face_model": "anime",   # lbpcascade_animeface — entrenado para estilos 2D
        "weights": {
            "face":      0.25,   # detector de anime no da confianza numérica
            "body":      0.15,   # YOLOv8 falla en cuerpos estilizados → peso bajo
            "hands":     0.20,   # MediaPipe funciona parcialmente
            "aesthetic": 0.40,   # CLIP es el scorer más fiable para ilustraciones
        },
        "presets": {
            # Umbrales ~25% menores: modelos photo-trained penalizan estilos 2D
            "strict":   {"keeper": 0.55, "review": 0.38},
            "balanced": {"keeper": 0.40, "review": 0.25},
            "lenient":  {"keeper": 0.24, "review": 0.14},
        },
    },
}

DEFAULT_CONTENT_TYPE = "illustration"


# ============================================================================
# ANALIZADOR PRINCIPAL
# ============================================================================

class SlopAnalyzer:
    """
    Gestiona la carga y ejecución de los modelos de detección.
    Uso: instanciar → initialize() en el worker thread → analyze() por imagen.
    """

    def __init__(self, models_dir: Path,
                 content_type: str  = DEFAULT_CONTENT_TYPE,
                 use_face: bool     = True,
                 use_body: bool     = True,
                 use_hands: bool    = True,
                 use_aesthetic: bool = True):

        self.models_dir   = Path(models_dir)
        self.content_type = content_type if content_type in CONTENT_TYPES else DEFAULT_CONTENT_TYPE
        self.use_face     = use_face
        self.use_body     = use_body
        self.use_hands    = use_hands
        self.use_aesthetic = use_aesthetic

        self._cfg          = CONTENT_TYPES[self.content_type]
        self._yunet        = None
        self._anime_cascade = None
        self._yolo         = None
        self._mp_hands     = None
        self._clip_model   = None
        self._clip_prep    = None
        self._aesthetic    = None
        self._device       = "cpu"

    # ------------------------------------------------------------------ #
    # Carga de modelos
    # ------------------------------------------------------------------ #

    def initialize(self):
        """Carga todos los modelos habilitados. Llamar desde el hilo worker."""
        try:
            import torch
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            self._device = "cpu"
        log.info(f"[SlopFilter] Device: {self._device} | Contenido: {self.content_type}")

        if self.use_face:
            face_model = self._cfg["face_model"]
            if face_model == "yunet":
                self._init_yunet()
            elif face_model == "anime":
                self._init_anime_cascade()

        if self.use_body:
            self._init_yolo()
        if self.use_hands:
            self._init_mediapipe()
        if self.use_aesthetic:
            self._init_clip()

    def _init_yunet(self):
        yunet_path = self.models_dir / "onnx" / "face_detection_yunet_2023mar.onnx"
        if not yunet_path.exists():
            url = ("https://huggingface.co/opencv/face_detection_yunet"
                   "/resolve/main/face_detection_yunet_2023mar.onnx?download=true")
            self._download(url, yunet_path)
        try:
            self._yunet = cv2.FaceDetectorYN.create(
                str(yunet_path), "", (320, 320), 0.60, 0.3, 5000
            )
            log.info("[SlopFilter] YuNet cargado.")
        except Exception as e:
            log.warning(f"[SlopFilter] YuNet no disponible: {e}")
            self.use_face = False

    def _init_anime_cascade(self):
        cascade_path = self.models_dir / "onnx" / "lbpcascade_animeface.xml"
        if not cascade_path.exists():
            url = ("https://raw.githubusercontent.com/nagadomi/lbpcascade_animeface"
                   "/master/lbpcascade_animeface.xml")
            self._download(url, cascade_path)
        try:
            cascade = cv2.CascadeClassifier(str(cascade_path))
            if cascade.empty():
                raise RuntimeError("Cascade vacía")
            self._anime_cascade = cascade
            log.info("[SlopFilter] lbpcascade_animeface cargado.")
        except Exception as e:
            log.warning(f"[SlopFilter] Anime cascade no disponible: {e}")
            self.use_face = False

    def _init_yolo(self):
        try:
            from ultralytics import YOLO
            yolo_path = self.models_dir / "yolo" / "yolov8n-pose.pt"
            yolo_path.parent.mkdir(parents=True, exist_ok=True)
            self._yolo = YOLO(str(yolo_path))
            log.info("[SlopFilter] YOLOv8-pose cargado.")
        except Exception as e:
            log.warning(f"[SlopFilter] YOLOv8-pose no disponible: {e}")
            self.use_body = False

    def _init_mediapipe(self):
        try:
            import mediapipe as mp
            self._mp_hands = mp.solutions.hands.Hands(
                static_image_mode=True,
                max_num_hands=2,
                min_detection_confidence=0.5,
                model_complexity=1,
            )
            log.info("[SlopFilter] MediaPipe Hands cargado.")
        except ImportError:
            log.warning("[SlopFilter] mediapipe no instalado.")
            self.use_hands = False
        except Exception as e:
            log.warning(f"[SlopFilter] MediaPipe no disponible: {e}")
            self.use_hands = False

    def _init_clip(self):
        try:
            import torch
            import torch.nn as nn
            import open_clip

            clip_cache = self.models_dir / "clip"
            clip_cache.mkdir(parents=True, exist_ok=True)
            model, _, preprocess = open_clip.create_model_and_transforms(
                "ViT-L-14", pretrained="openai", cache_dir=str(clip_cache)
            )
            model = model.to(self._device).eval()
            self._clip_model = model
            self._clip_prep  = preprocess

            mlp_path = self.models_dir / "aesthetic" / "sac_logos_ava_l14_linear.pth"
            mlp_path.parent.mkdir(parents=True, exist_ok=True)
            if not mlp_path.exists():
                self._download_aesthetic_mlp(mlp_path)

            mlp = nn.Linear(768, 1)
            state = torch.load(str(mlp_path), map_location=self._device, weights_only=True)
            mlp.load_state_dict(state)
            self._aesthetic = mlp.to(self._device).eval()
            log.info("[SlopFilter] CLIP aesthetic predictor cargado.")
        except ImportError:
            log.warning("[SlopFilter] open-clip-torch no instalado.")
            self.use_aesthetic = False
        except Exception as e:
            log.warning(f"[SlopFilter] CLIP no disponible: {e}")
            self.use_aesthetic = False

    def _download_aesthetic_mlp(self, dest: Path):
        from huggingface_hub import hf_hub_download
        import shutil
        log.info("[SlopFilter] Descargando pesos del aesthetic predictor…")
        tmp = hf_hub_download(
            repo_id="shunk031/aesthetics-predictor",
            filename="sac+logos+ava1-l14-linearMSE.pth",
            local_dir=str(dest.parent),
        )
        shutil.copy(tmp, str(dest))
        log.info(f"[SlopFilter] Aesthetic MLP → {dest}")

    def _download(self, url: str, dest: Path):
        import requests
        dest.parent.mkdir(parents=True, exist_ok=True)
        log.info(f"[SlopFilter] Descargando {dest.name}…")
        r = requests.get(url, stream=True, timeout=60)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)

    # ------------------------------------------------------------------ #
    # Scorers individuales (0.0 – 1.0)
    # ------------------------------------------------------------------ #

    def score_face(self, img_bgr: np.ndarray) -> float:
        """Dispatcher: YuNet para fotorrealista, lbpcascade para ilustración y 3D render."""
        face_model = self._cfg["face_model"]
        if face_model == "yunet":
            return self._score_face_yunet(img_bgr)
        if face_model == "anime":
            return self._score_face_anime(img_bgr)
        return 0.5

    def _score_face_yunet(self, img_bgr: np.ndarray) -> float:
        """
        Cara realista via YuNet.
        Sin cara → 0.5.  Baja confianza → 0.1–0.4.  Alta → 0.7–1.0.
        """
        if not self.use_face or self._yunet is None:
            return 0.5
        try:
            h, w = img_bgr.shape[:2]
            self._yunet.setInputSize((w, h))
            _, faces = self._yunet.detect(img_bgr)
            if faces is None or len(faces) == 0:
                return 0.5
            best = max(faces, key=lambda f: f[2] * f[3])
            confidence = float(best[14])
            multi_penalty = 0.1 * min(len(faces) - 1, 3) if len(faces) > 1 else 0.0
            return max(0.0, min(1.0, confidence - multi_penalty))
        except Exception as e:
            log.debug(f"[SlopFilter] score_face_yunet: {e}")
            return 0.5

    def _score_face_anime(self, img_bgr: np.ndarray) -> float:
        """
        Cara de anime via lbpcascade.
        El cascade no da confianza: usamos tamaño relativo como proxy de calidad.
        Sin cara → 0.5.  Cara grande → 0.9.  Cara pequeña → 0.65.
        """
        if not self.use_face or self._anime_cascade is None:
            return 0.5
        try:
            gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            h, w = gray.shape
            faces = self._anime_cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24)
            )
            if len(faces) == 0:
                return 0.5

            best      = max(faces, key=lambda f: f[2] * f[3])
            face_area = best[2] * best[3]
            img_area  = h * w
            # Cara que ocupa ≥10% de la imagen = buena presencia
            size_score = min(face_area / (img_area * 0.10), 1.0)
            base       = 0.55 + size_score * 0.35   # rango 0.55–0.90
            multi_pen  = 0.08 * min(len(faces) - 1, 3) if len(faces) > 1 else 0.0
            return max(0.0, min(1.0, base - multi_pen))
        except Exception as e:
            log.debug(f"[SlopFilter] score_face_anime: {e}")
            return 0.5

    def score_body(self, img_bgr: np.ndarray) -> float:
        """
        Coherencia corporal via YOLOv8-pose (keypoints COCO).
        Sin persona → 0.5.  Keypoints de torso confiables → 0.7–1.0.
        Para ilustraciones, el peso de este scorer es bajo (0.15).
        """
        if not self.use_body or self._yolo is None:
            return 0.5
        try:
            results = self._yolo(img_bgr, verbose=False)
            if not results or results[0].keypoints is None:
                return 0.5
            kps_conf = results[0].keypoints.conf
            if kps_conf is None or len(kps_conf) == 0:
                return 0.5
            person    = kps_conf[0].cpu().numpy()
            torso     = person[[5, 6, 11, 12]]
            extremities = person[[7, 8, 9, 10, 13, 14, 15, 16]]
            vis_t = torso[torso > 0.5]
            vis_e = extremities[extremities > 0.5]
            t_score = float(np.mean(vis_t)) if len(vis_t) > 0 else 0.5
            e_score = float(np.mean(vis_e)) if len(vis_e) > 0 else 0.5
            return t_score * 0.6 + e_score * 0.4
        except Exception as e:
            log.debug(f"[SlopFilter] score_body: {e}")
            return 0.5

    def score_hands(self, img_bgr: np.ndarray) -> float:
        """
        Calidad de manos via MediaPipe Hands.
        Sin manos → 1.0 (sin problema).  5 dedos → 0.8–1.0.  Imposible → 0.1.
        """
        if not self.use_hands or self._mp_hands is None:
            return 1.0
        try:
            img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            result  = self._mp_hands.process(img_rgb)
            if not result.multi_hand_landmarks:
                return 1.0
            scores = [self._eval_hand(lm) for lm in result.multi_hand_landmarks]
            return float(np.mean(scores))
        except Exception as e:
            log.debug(f"[SlopFilter] score_hands: {e}")
            return 1.0

    def _eval_hand(self, landmarks) -> float:
        lm   = landmarks.landmark
        tips = [4,  8, 12, 16, 20]
        pips = [3,  6, 10, 14, 18]
        extended = 0
        for tip_i, pip_i in zip(tips, pips):
            if tip_i == 4:
                if abs(lm[4].x - lm[2].x) > 0.05:
                    extended += 1
            else:
                if lm[tip_i].y < lm[pip_i].y:
                    extended += 1
        if extended < 1 or extended > 5:
            return 0.1
        if extended == 5:
            return 1.0
        if extended >= 3:
            return 0.75
        return 0.5

    def score_aesthetic(self, img_bgr: np.ndarray) -> float:
        """
        Score estético via CLIP ViT-L/14 + linear predictor.
        Score raw AVA ~4.5–7.5 → normalizado 0–1.
        Es el scorer más fiable para ilustraciones.
        """
        if not self.use_aesthetic or self._clip_model is None:
            return 0.5
        try:
            import torch
            from PIL import Image
            img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(img_rgb)
            tensor  = self._clip_prep(pil_img).unsqueeze(0).to(self._device)
            with torch.no_grad():
                features = self._clip_model.encode_image(tensor)
                features = features / features.norm(dim=-1, keepdim=True)
                raw = self._aesthetic(features.float()).squeeze().cpu().item()
            return max(0.0, min(1.0, (raw - 4.5) / 3.0))
        except Exception as e:
            log.debug(f"[SlopFilter] score_aesthetic: {e}")
            return 0.5

    # ------------------------------------------------------------------ #
    # Análisis completo
    # ------------------------------------------------------------------ #

    def analyze(self, img_bgr: np.ndarray) -> dict:
        """
        Corre todos los scorers y retorna scores individuales + combined.
        Los pesos aplicados dependen del content_type configurado.
        """
        weights = self._cfg["weights"]

        face      = self.score_face(img_bgr)      if self.use_face      else 0.5
        body      = self.score_body(img_bgr)      if self.use_body      else 0.5
        hands     = self.score_hands(img_bgr)     if self.use_hands     else 1.0
        aesthetic = self.score_aesthetic(img_bgr) if self.use_aesthetic else 0.5

        combined = (
            face      * weights["face"]      +
            body      * weights["body"]      +
            hands     * weights["hands"]     +
            aesthetic * weights["aesthetic"]
        )
        return {
            "face":         round(face,      3),
            "body":         round(body,      3),
            "hands":        round(hands,     3),
            "aesthetic":    round(aesthetic, 3),
            "combined":     round(combined,  3),
            "content_type": self.content_type,
        }

    def analyze_calibration(self, img_bgr: np.ndarray) -> dict:
        """
        Modo calibración: retorna scores raw de cada modelo sin ponderar.
        Útil para ajustar pesos y umbrales con imágenes de ejemplo.
        """
        result = self.analyze(img_bgr)
        cfg    = self._cfg
        result["_weights"]  = cfg["weights"]
        result["_presets"]  = cfg["presets"]
        result["_face_model"] = cfg["face_model"]
        return result


# ============================================================================
# CLASIFICACIÓN
# ============================================================================

def classify(scores: dict, preset: str = "balanced",
             content_type: str = DEFAULT_CONTENT_TYPE) -> str:
    """
    Clasifica una imagen como keeper / review / slop.
    Usa umbrales específicos del content_type.

    :param scores:       dict con clave 'combined' (0–1).
    :param preset:       'strict' / 'balanced' / 'lenient'.
    :param content_type: tipo de contenido para seleccionar umbrales.
    :return: 'keeper' / 'review' / 'slop'
    """
    cfg       = CONTENT_TYPES.get(content_type, CONTENT_TYPES[DEFAULT_CONTENT_TYPE])
    presets   = cfg["presets"]
    threshold = presets.get(preset, presets["balanced"])
    combined  = scores.get("combined", 0.0)

    if combined >= threshold["keeper"]:
        return LABEL_KEEPER
    if combined >= threshold["review"]:
        return LABEL_REVIEW
    return LABEL_SLOP
