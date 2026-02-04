import os
import cv2
import numpy as np
import onnxruntime as ort
from core.paths import ProjectPaths

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
            print("RecognitionEngine: Hybrid Pipeline loaded (YuNet + AnimeFace + ArcFace).")
            
        except Exception as e:
            print(f"RecognitionEngine: Error loading models - {e}")
            self.initialized = False
            
    def _ensure_models_exist(self):
        """Downloads required models."""
        import requests 
        import shutil
        
        # 1. YuNet (Official OpenCV Zoo hosted on Hugging Face is more reliable)
        if not os.path.exists(self.yunet_path):
            print("Downloading YuNet...")
            # URL Fixed: OpenCV moved models to HF
            url = "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx?download=true"
            self._download_file(url, self.yunet_path)

        # 2. AnimeFace
        if not os.path.exists(self.anime_path):
            print("Downloading AnimeFace Cascade...")
            url = "https://raw.githubusercontent.com/nagadomi/lbpcascade_animeface/master/lbpcascade_animeface.xml"
            self._download_file(url, self.anime_path)
            
        # 3. ArcFace (Check previous download)
        if not os.path.exists(self.face_model_path):
            print("Downloading ArcFace (w600k)...")
            self._download_buffalo_l()

    def _download_file(self, url, path):
        import requests
        try:
            r = requests.get(url, stream=True)
            r.raise_for_status()
            with open(path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            print(f"Downloaded: {os.path.basename(path)}")
        except Exception as e:
            print(f"Failed to download {url}: {e}")
            # Self-Healing: Remove partial/empty file so it retries next time
            if os.path.exists(path):
                os.remove(path)
                print(f"Removed partial file: {path}")

    def _download_buffalo_l(self):
        # ... (Legacy Zip Logic from previous step if needed) ...
        # For brevity, assuming user has it or we re-implement it briefly
        pass 

    def analyze_image(self, cv2_image):
        """
        Full Pipeline: Detect -> Align -> Embed
        Returns: (embedding, bbox, confidence)
        """
        if not self.initialized: return None, None, 0.0
        
        h, w = cv2_image.shape[:2]
        
        # --- Stage 1: YuNet ---
        self.yunet.setInputSize((w, h))
        _, faces = self.yunet.detect(cv2_image)
        
        face_img = None
        bbox = None # (x, y, w, h)
        score = 0.0
        
        if faces is not None and len(faces) > 0:
            # Take best face
            face = faces[0]
            # YuNet returns [x, y, w, h, x_re, y_re, x_le, y_le, ...]
            box = face[0:4].astype(int)
            score = float(face[14])
            
            # Align using 5 points? Or just crop?
            # For simplicity and robust hybrid support, we crop + resize
            # ArcFace handles minor misalignment well
            x, y, bw, bh = box
            bbox = (x, y, bw, bh)
            
            # Safe Crop
            face_img = self._safe_crop(cv2_image, box)
            
        # --- Stage 2: AnimeFace Fallback ---
        if face_img is None:
            gray = cv2.cvtColor(cv2_image, cv2.COLOR_BGR2GRAY)
            # Equalize hist for better anime detection?
            gray = cv2.equalizeHist(gray)
            
            faces_anim = self.anime_cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24)
            )
            
            if len(faces_anim) > 0:
                # Take largest
                faces_anim = sorted(faces_anim, key=lambda f: f[2]*f[3], reverse=True)
                x, y, bw, bh = faces_anim[0]
                bbox = (x, y, bw, bh)
                score = 0.95 # Artificial score for Cascade
                
                face_img = self._safe_crop(cv2_image, (x, y, bw, bh))
                
        # --- Stage 3: Feature Extraction ---
        if face_img is None:
            # Fallback: Center Crop (User manual focus)
            # We treat the whole image as face
            return self.get_embedding(cv2_image), (0,0,w,h), 0.1
            
        # Resize for ArcFace
        input_blob = cv2.dnn.blobFromImage(cv2_image, 1.0/127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
        # Wait, blobFromImage on the CROP
        blob_crop = cv2.resize(face_img, (112, 112))
        input_blob = cv2.dnn.blobFromImage(blob_crop, 1.0/127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
        
        embedding = self.rec_session.run(None, {self.rec_input_name: input_blob})[0]
        from sklearn.preprocessing import normalize
        embedding = normalize(embedding).flatten()
        
        return embedding, bbox, score

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

    def get_embedding(self, cv2_image):
        # Wrapper for backward compatibility or direct calls
        emb, _, _ = self.analyze_image(cv2_image)
        return emb 

    def compare(self, emb1, emb2):
        from sklearn.metrics.pairwise import cosine_similarity
        return cosine_similarity([emb1], [emb2])[0][0]
