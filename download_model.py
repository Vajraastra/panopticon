
import requests
import os

url = "https://github.com/lindevs/yolov8-face/releases/latest/download/yolov8n-face-lindevs.pt"
target = "yolov8n-face.pt"

print(f"Downloading {target} from {url}...")

try:
    response = requests.get(url, stream=True)
    response.raise_for_status()
    
    with open(target, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
            
    print(f"[OK] Download complete: {os.path.abspath(target)}")
    
except Exception as e:
    print(f"[FAIL] Download failed: {e}")
