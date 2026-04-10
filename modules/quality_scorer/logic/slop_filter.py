"""
Slop Filter — Fase 1 del Quality Scorer.

Evalúa calidad anatómica y estética de imágenes generadas por IA:
- Rostro   : YuNet (confianza + landmarks)
- Cuerpo   : YOLOv8-pose (confianza de keypoints COCO)
- Manos    : MediaPipe Hands (conteo de dedos + ángulos)
- Estética : CLIP ViT-L/14 + MLP lineal (LAION aesthetic predictor)

Todos los scorers retornan 0.0–1.0.
La clasificación final es ternaria: keeper / review / slop.
"""
import logging
import cv2
import numpy as np
from pathlib import Path

log = logging.getLogger(__name__)

# Pesos para colecciones character-centered (siempre hay persona)
SCORE_WEIGHTS = {
    "face":      0.35,
    "body":      0.25,
    "hands":     0.25,
    "aesthetic": 0.15,
}

PRESETS = {
    "strict":   {"keeper": 0.70, "review": 0.50},
    "balanced": {"keeper": 0.55, "review": 0.35},
    "lenient":  {"keeper": 0.35, "review": 0.20},
}

LABEL_KEEPER = "keeper"
LABEL_REVIEW = "review"
LABEL_SLOP   = "slop"


# ============================================================================
# ANALIZADOR PRINCIPAL
# ============================================================================

class SlopAnalyzer:
    """
    Gestiona la carga y ejecución de los modelos de detección.
    Uso: instanciar → initialize() en el worker thread → analyze() por imagen.
    """

    def __init__(self, models_dir: Path,
                 use_face=True, use_body=True,
                 use_hands=True, use_aesthetic=True):
        self.models_dir = Path(models_dir)
        self.use_face      = use_face
        self.use_body      = use_body
        self.use_hands     = use_hands
        self.use_aesthetic = use_aesthetic

        self._yunet       = None
        self._yolo        = None
        self._mp_hands    = None
        self._clip_model  = None
        self._clip_prep   = None
        self._aesthetic   = None   # nn.Linear(768, 1)
        self._device      = "cpu"

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
        log.info(f"[SlopFilter] Device: {self._device}")

        if self.use_face:
            self._init_yunet()
        if self.use_body:
            self._init_yolo()
        if self.use_hands:
            self._init_mediapipe()
        if self.use_aesthetic:
            self._init_clip()

    def _init_yunet(self):
        yunet_path = self.models_dir / "onnx" / "face_detection_yunet_2023mar.onnx"
        if not yunet_path.exists():
            # Descargar si no existe (mismo URL que character recognizer)
            url = ("https://huggingface.co/opencv/face_detection_yunet"
                   "/resolve/main/face_detection_yunet_2023mar.onnx?download=true")
            self._download(url, yunet_path)
        try:
            self._yunet = cv2.FaceDetectorYN.create(
                str(yunet_path), "", (320, 320), 0.6, 0.3, 5000
            )
            log.info("[SlopFilter] YuNet cargado.")
        except Exception as e:
            log.warning(f"[SlopFilter] YuNet no disponible: {e}")
            self.use_face = False

    def _init_yolo(self):
        try:
            from ultralytics import YOLO
            yolo_path = self.models_dir / "yolo" / "yolov8n-pose.pt"
            yolo_path.parent.mkdir(parents=True, exist_ok=True)
            # ultralytics auto-descarga si no existe en esa ruta
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
                model_complexity=1
            )
            log.info("[SlopFilter] MediaPipe Hands cargado.")
        except ImportError:
            log.warning("[SlopFilter] mediapipe no instalado — score de manos desactivado.")
            self.use_hands = False
        except Exception as e:
            log.warning(f"[SlopFilter] MediaPipe no disponible: {e}")
            self.use_hands = False

    def _init_clip(self):
        try:
            import torch
            import torch.nn as nn
            import open_clip

            # Modelo CLIP ViT-L/14 (pesos OpenAI, ~890MB, descarga única)
            clip_cache = self.models_dir / "clip"
            clip_cache.mkdir(parents=True, exist_ok=True)
            model, _, preprocess = open_clip.create_model_and_transforms(
                "ViT-L-14", pretrained="openai",
                cache_dir=str(clip_cache)
            )
            model = model.to(self._device).eval()
            self._clip_model = model
            self._clip_prep  = preprocess

            # MLP estético: nn.Linear(768, 1) — LAION aesthetic predictor
            mlp_path = self.models_dir / "aesthetic" / "sac_logos_ava_l14_linear.pth"
            mlp_path.parent.mkdir(parents=True, exist_ok=True)
            if not mlp_path.exists():
                self._download_aesthetic_mlp(mlp_path)

            mlp = nn.Linear(768, 1)
            state = torch.load(str(mlp_path), map_location=self._device,
                               weights_only=True)
            mlp.load_state_dict(state)
            mlp = mlp.to(self._device).eval()
            self._aesthetic = mlp
            log.info("[SlopFilter] CLIP aesthetic predictor cargado.")

        except ImportError:
            log.warning("[SlopFilter] open-clip-torch no instalado — score estético desactivado.")
            self.use_aesthetic = False
        except Exception as e:
            log.warning(f"[SlopFilter] CLIP no disponible: {e}")
            self.use_aesthetic = False

    def _download_aesthetic_mlp(self, dest: Path):
        """Descarga los pesos del MLP estético desde HuggingFace."""
        from huggingface_hub import hf_hub_download
        log.info("[SlopFilter] Descargando pesos del aesthetic predictor…")
        tmp = hf_hub_download(
            repo_id="shunk031/aesthetics-predictor",
            filename="sac+logos+ava1-l14-linearMSE.pth",
            local_dir=str(dest.parent)
        )
        import shutil
        shutil.copy(tmp, str(dest))
        log.info(f"[SlopFilter] Aesthetic MLP descargado → {dest}")

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
        """
        Evalúa calidad del rostro via YuNet.
        Sin cara → 0.5 (no evaluable, neutro).
        Baja confianza → 0.1–0.4.  Alta confianza → 0.7–1.0.
        Múltiples caras → penalización leve.
        """
        if not self.use_face or self._yunet is None:
            return 0.5
        try:
            h, w = img_bgr.shape[:2]
            self._yunet.setInputSize((w, h))
            _, faces = self._yunet.detect(img_bgr)
            if faces is None or len(faces) == 0:
                return 0.5

            # Cara más grande por área de bounding box
            best = max(faces, key=lambda f: f[2] * f[3])
            confidence = float(best[14])

            # Penalizar si hay más de una cara (colección de personaje = 1 esperada)
            multi_penalty = 0.1 * min(len(faces) - 1, 3) if len(faces) > 1 else 0.0
            return max(0.0, min(1.0, confidence - multi_penalty))

        except Exception as e:
            log.debug(f"[SlopFilter] score_face error: {e}")
            return 0.5

    def score_body(self, img_bgr: np.ndarray) -> float:
        """
        Evalúa coherencia corporal via YOLOv8-pose (17 keypoints COCO).
        Sin cuerpo → 0.5 (neutro).
        Keypoints de torso con alta confianza → 0.8–1.0.
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

            # Persona con mayor área (primer resultado de YOLO ya está ordenado)
            person = kps_conf[0].cpu().numpy()  # shape: (17,)

            # Torso: hombros (5,6) + caderas (11,12) — críticos para anatomía
            torso      = person[[5, 6, 11, 12]]
            extremities = person[[7, 8, 9, 10, 13, 14, 15, 16]]

            vis_torso = torso[torso > 0.5]
            vis_extr  = extremities[extremities > 0.5]

            t_score = float(np.mean(vis_torso)) if len(vis_torso) > 0 else 0.5
            e_score = float(np.mean(vis_extr))  if len(vis_extr)  > 0 else 0.5
            return t_score * 0.6 + e_score * 0.4

        except Exception as e:
            log.debug(f"[SlopFilter] score_body error: {e}")
            return 0.5

    def score_hands(self, img_bgr: np.ndarray) -> float:
        """
        Evalúa calidad de manos via MediaPipe Hands.
        Sin manos → 1.0 (sin problema que evaluar).
        5 dedos correctos → 0.8–1.0.  Dedos imposibles → 0.0–0.4.
        """
        if not self.use_hands or self._mp_hands is None:
            return 1.0
        try:
            img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            result  = self._mp_hands.process(img_rgb)
            if not result.multi_hand_landmarks:
                return 1.0  # sin manos detectadas = sin problema

            scores = [self._eval_hand(lm) for lm in result.multi_hand_landmarks]
            return float(np.mean(scores))

        except Exception as e:
            log.debug(f"[SlopFilter] score_hands error: {e}")
            return 1.0

    def _eval_hand(self, landmarks) -> float:
        """
        Cuenta dedos extendidos via landmarks de MediaPipe.
        Retorna 0.0–1.0 según anatomía detectada.
        """
        lm = landmarks.landmark
        # Tips y PIPs de cada dedo (pulgar, índice, medio, anular, meñique)
        tips = [4,  8, 12, 16, 20]
        pips = [3,  6, 10, 14, 18]

        extended = 0
        for tip_i, pip_i in zip(tips, pips):
            if tip_i == 4:  # Pulgar: comparar en eje X (horizontal)
                if abs(lm[4].x - lm[2].x) > 0.05:
                    extended += 1
            else:           # Resto: tip más arriba que pip en eje Y
                if lm[tip_i].y < lm[pip_i].y:
                    extended += 1

        # Score según número de dedos detectables (esperamos 1–5)
        if extended < 1 or extended > 5:
            return 0.1   # imposible
        if extended == 5:
            return 1.0
        if extended >= 3:
            return 0.75
        return 0.5

    def score_aesthetic(self, img_bgr: np.ndarray) -> float:
        """
        Evalúa calidad estética via CLIP ViT-L/14 + linear predictor.
        Score raw ~4.5–7.5 (escala AVA 1-10) → normalizado a 0–1.
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

            # Normalizar escala AVA: 4.5 = neutro, 7.5 = excelente
            return max(0.0, min(1.0, (raw - 4.5) / 3.0))

        except Exception as e:
            log.debug(f"[SlopFilter] score_aesthetic error: {e}")
            return 0.5

    # ------------------------------------------------------------------ #
    # Análisis completo
    # ------------------------------------------------------------------ #

    def analyze(self, img_bgr: np.ndarray) -> dict:
        """
        Corre todos los scorers habilitados.
        Retorna dict con scores individuales y 'combined' (0–1).
        """
        face      = self.score_face(img_bgr)
        body      = self.score_body(img_bgr)
        hands     = self.score_hands(img_bgr)
        aesthetic = self.score_aesthetic(img_bgr)

        combined = (
            face      * SCORE_WEIGHTS["face"]      +
            body      * SCORE_WEIGHTS["body"]      +
            hands     * SCORE_WEIGHTS["hands"]     +
            aesthetic * SCORE_WEIGHTS["aesthetic"]
        )
        return {
            "face":      round(face, 3),
            "body":      round(body, 3),
            "hands":     round(hands, 3),
            "aesthetic": round(aesthetic, 3),
            "combined":  round(combined, 3),
        }


# ============================================================================
# CLASIFICACIÓN
# ============================================================================

def classify(scores: dict, preset: str = "balanced") -> str:
    """
    Clasifica una imagen según su score combinado.

    :param scores: dict con clave 'combined' (0–1).
    :param preset: 'strict' / 'balanced' / 'lenient'.
    :return: 'keeper' / 'review' / 'slop'
    """
    thresholds = PRESETS.get(preset, PRESETS["balanced"])
    combined   = scores.get("combined", 0.0)
    if combined >= thresholds["keeper"]:
        return LABEL_KEEPER
    if combined >= thresholds["review"]:
        return LABEL_REVIEW
    return LABEL_SLOP
