from PySide6.QtCore import QThread, Signal, QObject
import cv2
import numpy as np
from .recognition_engine import RecognitionEngine
from .profile_db import ProfileDB

class RecognitionWorker(QThread):
    # Signals
    # Updated: path, cv2_image, embedding, suggestion, bbox, confidence
    image_processed = Signal(str, object, object, str, object, float) 
    finished = Signal()
    progress = Signal(int, int) # current, total

    def __init__(self, paths):
        super().__init__()
        self.paths = paths
        self.is_running = True
        self.engine = RecognitionEngine()
        self.db = ProfileDB()
        self.current_idx = 0
        self.paused = True
        
    def run(self):
        print(f"DEBUG: Worker started with {len(self.paths)} items.")
        # 0. Ensure Engine Init (Downloads models if needed)
        self.engine.initialize()
        
        print(f"DEBUG: Engine initialized. Starting loop. IsRunning={self.is_running}")
        while self.is_running and self.current_idx < len(self.paths):
            if self.paused:
                # print("DEBUG: Paused...") # Verbose
                self.msleep(50)
                continue

            path = self.paths[self.current_idx]
            print(f"DEBUG: Processing index {self.current_idx}: {path}")
            
            try:
                # 1. Read Image
                # Use numpy fromfile for utf-8 path support on windows if cv2 fails
                stream = open(path, "rb")
                bytes = bytearray(stream.read())
                numpyarray = np.asarray(bytes, dtype=np.uint8)
                img = cv2.imdecode(numpyarray, cv2.IMREAD_COLOR)
                stream.close()
                
                if img is None:
                    print(f"DEBUG: Failed to load image content: {path}")
                    self.current_idx += 1
                    continue
                
                print(f"DEBUG: Image loaded. Analyzing dimensions: {img.shape}")

                # 2. Get Embedding + BBox + Score
                # result = (embedding, bbox, score)
                embedding, bbox, det_score = self.engine.analyze_image(img)
                print(f"DEBUG: Analysis complete. Score: {det_score}")
                
                # 3. Compare with DB
                suggestion = None
                max_score = 0
                
                if embedding is not None:
                    profiles = self.db.get_all_profiles()
                    for name, ref_emb in profiles:
                        score = self.engine.compare(embedding, ref_emb)
                        if score > max_score:
                            max_score = score
                            # Combine Detection Score + Recog Score?
                            # For now, just Recog Score for identity
                            if score > 0.6: # Threshold
                                suggestion = name
                
                # 4. Emit Result
                # Using max_score as the "Confidence" of the identity match
                # Or det_score if no match?
                # Let's emit the Recog Score if matched, else 0.0, BUT pass det_score for box color?
                # User wants box color based on identification confidence.
                final_confidence = max_score if suggestion else 0.0
                
                self.image_processed.emit(path, img, embedding, suggestion, bbox, final_confidence)
                
                # Wait for Ack / "Next" command from UI
                self.paused = True
                print("DEBUG: Waiting for user action (Paused)...")
                while self.paused and self.is_running:
                     self.msleep(50)
                
                print("DEBUG: Resuming worker...")
                self.current_idx += 1
                self.progress.emit(self.current_idx, len(self.paths))
                
            except Exception as e:
                print(f"Worker Error: {e}")
                self.current_idx += 1
                
            except Exception as e:
                print(f"Worker Error: {e}")
                self.current_idx += 1

        self.finished.emit()

    def request_next(self):
        self.paused = False
    
    def pause(self):
        self.paused = True
