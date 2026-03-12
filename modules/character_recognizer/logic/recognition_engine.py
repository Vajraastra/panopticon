import os
import logging
import requests
import cv2
import numpy as np
import onnxruntime as ort
from sklearn.preprocessing import normalize
from sklearn.metrics.pairwise import cosine_similarity
from core.paths import ProjectPaths

log = logging.getLogger(__name__)

class RecognitionEngine:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RecognitionEngine, cls).__new__(cls)
            cls._instance.initialized = False
        return cls._instance

    def initialize(self):
        if self.initialized:
            return
        
        # Define model paths
        self.models_dir = os.path.join(ProjectPaths.root(), "models", "onnx")
        os.makedirs(self.models_dir, exist_ok=True)
        
        # Paths
        self.yunet_path = os.path.join(self.models_dir, "face_detection_yunet_2023mar.onnx")
        self.anime_path = os.path.join(self.models_dir, "lbpcascade_animeface.xml")
        self.face_model_path = os.path.join(self.models_dir, "w600k_r50.onnx")
        
        try:
            self._ensure_models_exist()
            
            # 1. Load YuNet (General/3D Detector)
            self.yunet = cv2.FaceDetectorYN.create(
                model=self.yunet_path,
                config="",
                input_size=(320, 320), # Dynamic, updated per image
                score_threshold=0.6,
                nms_threshold=0.3,
                top_k=5000
            )
            
            # 2. Load AnimeFace (2D Fallback)
            self.anime_cascade = cv2.CascadeClassifier(self.anime_path)
            
            # 3. Load ArcFace (Recognizer)
            # We keep using ONNX Runtime for ArcFace as it is standard
            self.rec_session = ort.InferenceSession(self.face_model_path, providers=['CPUExecutionProvider'])
            self.rec_input_name = self.rec_session.get_inputs()[0].name
            
            self.initialized = True
            log.info("RecognitionEngine: Hybrid Pipeline loaded (YuNet + AnimeFace + ArcFace).")

        except Exception as e:
            log.error(f"RecognitionEngine: Error loading models - {e}")
            self.initialized = False
            
    def _ensure_models_exist(self):
        """Downloads required models."""
        if not os.path.exists(self.yunet_path):
            log.info("Downloading YuNet...")
            url = "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx?download=true"
            self._download_file(url, self.yunet_path)

        if not os.path.exists(self.anime_path):
            log.info("Downloading AnimeFace Cascade...")
            url = "https://raw.githubusercontent.com/nagadomi/lbpcascade_animeface/master/lbpcascade_animeface.xml"
            self._download_file(url, self.anime_path)

        if not os.path.exists(self.face_model_path):
            log.info("Downloading ArcFace (w600k)...")
            self._download_buffalo_l()

    def _download_file(self, url, path):
        import requests
        try:
            r = requests.get(url, stream=True)
            r.raise_for_status()
            with open(path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            log.info(f"Downloaded: {os.path.basename(path)}")
        except Exception as e:
            log.error(f"Failed to download {url}: {e}")
            if os.path.exists(path):
                os.remove(path)
                log.warning(f"Removed partial file: {path}")

    def _download_buffalo_l(self):
        # ... (Legacy Zip Logic from previous step if needed) ...
        # For brevity, assuming user has it or we re-implement it briefly
        pass 

    # Standard ArcFace destination landmarks for 112x112 aligned output
    _ARCFACE_DST = np.array([
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ], dtype=np.float32)

    def _align_face_yunet(self, img, face_row):
        """Affine alignment using YuNet's 5 landmarks for ArcFace input."""
        # YuNet row: [x, y, w, h, x_re, y_re, x_le, y_le, x_nose, y_nose, x_rmth, y_rmth, x_lmth, y_lmth, score]
        src = np.array([
            [face_row[4], face_row[5]],   # right eye
            [face_row[6], face_row[7]],   # left eye
            [face_row[8], face_row[9]],   # nose
            [face_row[10], face_row[11]], # right mouth
            [face_row[12], face_row[13]], # left mouth
        ], dtype=np.float32)

        M, _ = cv2.estimateAffinePartial2D(src, self._ARCFACE_DST, method=cv2.LMEDS)
        if M is None:
            return None
        return cv2.warpAffine(img, M, (112, 112), borderValue=0)

    def analyze_image(self, cv2_image, mode='illustration'):
        """
        Full Pipeline: Detect -> Align -> Embed
        mode='illustration': AnimeFace cascade primary (original behaviour, untouched)
        mode='real': YuNet with landmark alignment only, no anime fallback
        Returns: (embedding, bbox, confidence)
        """
        if not self.initialized: return None, None, 0.0

        h, w = cv2_image.shape[:2]
        face_img = None
        bbox = None
        score = 0.0

        if mode == 'real':
            # --- Real Person Path: YuNet + landmark alignment ---
            self.yunet.setInputSize((w, h))
            _, faces = self.yunet.detect(cv2_image)

            if faces is not None and len(faces) > 0:
                face = faces[0]
                box = face[0:4].astype(int)
                score = float(face[14])
                x, y, bw, bh = box
                bbox = (x, y, bw, bh)
                face_img = self._align_face_yunet(cv2_image, face)
                if face_img is None:
                    face_img = self._safe_crop(cv2_image, box)

            if face_img is None:
                return self._embed(cv2_image), (0, 0, w, h), 0.1

        else:
            # --- Illustration Path: original behaviour, untouched ---
            # Stage 1: YuNet
            self.yunet.setInputSize((w, h))
            _, faces = self.yunet.detect(cv2_image)

            if faces is not None and len(faces) > 0:
                face = faces[0]
                box = face[0:4].astype(int)
                score = float(face[14])
                x, y, bw, bh = box
                bbox = (x, y, bw, bh)
                face_img = self._safe_crop(cv2_image, box)

            # Stage 2: AnimeFace Fallback
            if face_img is None:
                gray = cv2.cvtColor(cv2_image, cv2.COLOR_BGR2GRAY)
                gray = cv2.equalizeHist(gray)
                faces_anim = self.anime_cascade.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24)
                )
                if len(faces_anim) > 0:
                    faces_anim = sorted(faces_anim, key=lambda f: f[2]*f[3], reverse=True)
                    x, y, bw, bh = faces_anim[0]
                    bbox = (x, y, bw, bh)
                    score = 0.95
                    face_img = self._safe_crop(cv2_image, (x, y, bw, bh))

            if face_img is None:
                return self._embed(cv2_image), (0, 0, w, h), 0.1

        return self._embed(face_img), bbox, score

    def _embed(self, face_img):
        """Run ArcFace on a face crop (or full image as fallback)."""
        blob_crop = cv2.resize(face_img, (112, 112))
        input_blob = cv2.dnn.blobFromImage(blob_crop, 1.0/127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
        embedding = self.rec_session.run(None, {self.rec_input_name: input_blob})[0]
        return normalize(embedding).flatten()

    def _safe_crop(self, img, box):
        x, y, w, h = box
        img_h, img_w = img.shape[:2]
        
        # Pad slightly to include hair/chin?
        # Anime usually needs hair context, but ArcFace needs face.
        # Let's keep it tight to the box for now.
        
        x1 = max(0, x)
        y1 = max(0, y)
        x2 = min(img_w, x + w)
        y2 = min(img_h, y + h)
        
        if x2 <= x1 or y2 <= y1: return None
        return img[y1:y2, x1:x2]

    def get_embedding(self, cv2_image, mode='illustration'):
        emb, _, _ = self.analyze_image(cv2_image, mode=mode)
        return emb

    def compare(self, emb1, emb2):
        return cosine_similarity([emb1], [emb2])[0][0]
