import os
import shutil
from PIL import Image

# =========================
# CONFIGURATION
# =========================

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
DUMMY_SIZE = (32, 32)
DUMMY_COLOR = (128, 128, 128)  # Gray
ORIGINALS_DIR = "originals"
SIZE_THRESHOLD_KB = 10  # Files < 10KB are considered dummies

# =========================
# DUMMY DETECTION
# =========================

def is_dummy_file(filepath):
    """
    Detects if a file is a dummy by analyzing its size and content.
    
    Detection Strategy:
    1. Size-based: Files < 10KB are suspect
    2. Image-based: Check if 32x32 gray image
    3. Generic: Very small files (< 100 bytes) for non-images
    
    Returns:
        bool: True if file is detected as dummy
    """
    if not os.path.isfile(filepath):
        return False
    
    file_size = os.path.getsize(filepath)
    
    # Quick rejection: Large files can't be dummies
    if file_size > SIZE_THRESHOLD_KB * 1024:
        return False
    
    ext = os.path.splitext(filepath)[1].lower()
    
    # IMAGE DUMMY DETECTION
    if ext in IMAGE_EXTENSIONS:
        try:
            with Image.open(filepath) as img:
                # Check signature: 32x32 dimensions
                if img.size == DUMMY_SIZE:
                    # Optional: Verify pixel content is gray
                    # (Skip for performance - size is usually enough)
                    return True
        except Exception:
            # Corrupted or non-image file
            pass
    
    # GENERIC FILE DUMMY DETECTION
    # Non-image dummies are created as near-empty files
    if file_size < 100:
        return True
    
    return False

# =========================
# DUMMY CREATION
# =========================

def create_dummy_image(path, extension):
    """Creates a 32x32 gray dummy image."""
    img = Image.new("RGB", DUMMY_SIZE, DUMMY_COLOR)
    
    if extension in {".jpg", ".jpeg"}:
        img.save(path, "JPEG", quality=20, optimize=True)
    elif extension == ".png":
        img.save(path, "PNG", optimize=True)
    elif extension == ".webp":
        img.save(path, "WEBP", quality=20)
    elif extension == ".gif":
        img.save(path, "GIF")
    elif extension == ".bmp":
        img.save(path, "BMP")
    else:
        raise ValueError(f"Unsupported image format: {extension}")

def create_dummy_generic(path):
    """Creates a minimal dummy file for non-images."""
    with open(path, "wb") as f:
        f.write(b"X")  # 1 byte placeholder

# =========================
# PROCESSING LOGIC
# =========================

def process_folder(base_path, progress_callback=None):
    """
    Process a folder to create dummies for all non-dummy files.
    
    Smart Incremental Processing:
    - Detects existing dummies automatically (no manifest needed)
    - Only processes NEW files (non-dummies)
    - Supports re-runs after scraper adds new files
    
    Args:
        base_path (str): Folder to process
        progress_callback (callable): Optional callback(current, total, filename)
        
    Returns:
        dict: Statistics (processed, skipped, errors, space_saved)
    """
    if not os.path.isdir(base_path):
        raise ValueError(f"Invalid path: {base_path}")
    
    originals_path = os.path.join(base_path, ORIGINALS_DIR)
    os.makedirs(originals_path, exist_ok=True)
    
    stats = {
        "processed": 0,
        "skipped_dummies": 0,
        "skipped_originals": 0,
        "errors": 0,
        "space_saved_bytes": 0
    }
    
    # Scan all files in root directory
    all_files = [f for f in os.listdir(base_path) 
                 if os.path.isfile(os.path.join(base_path, f))]
    
    total_files = len(all_files)
    
    for idx, filename in enumerate(all_files):
        full_path = os.path.join(base_path, filename)
        original_target = os.path.join(originals_path, filename)
        
        # Report progress
        if progress_callback:
            progress_callback(idx + 1, total_files, filename)
        
        # Skip if already exists in originals/
        if os.path.exists(original_target):
            stats["skipped_originals"] += 1
            continue
        
        # SMART DETECTION: Skip if already a dummy
        if is_dummy_file(full_path):
            stats["skipped_dummies"] += 1
            continue
        
        # Process: Move to originals/ and create dummy
        try:
            original_size = os.path.getsize(full_path)
            
            # Move original
            shutil.move(full_path, original_target)
            
            # Create dummy replacement
            name, ext = os.path.splitext(filename)
            ext_lower = ext.lower()
            
            if ext_lower in IMAGE_EXTENSIONS:
                create_dummy_image(full_path, ext_lower)
            else:
                create_dummy_generic(full_path)
            
            # Calculate space saved
            dummy_size = os.path.getsize(full_path)
            stats["space_saved_bytes"] += (original_size - dummy_size)
            stats["processed"] += 1
            
        except Exception as e:
            stats["errors"] += 1
            print(f"[ERROR] Failed to process {filename}: {e}")
    
    return stats

# =========================
# UTILITY FUNCTIONS
# =========================

def get_folder_stats(base_path):
    """
    Analyze a folder to identify dummies vs originals.
    
    Returns:
        dict: Counts and space analysis
    """
    if not os.path.isdir(base_path):
        return None
    
    stats = {
        "total_files": 0,
        "dummies": 0,
        "originals": 0,
        "dummy_space_bytes": 0,
        "original_space_bytes": 0
    }
    
    for filename in os.listdir(base_path):
        full_path = os.path.join(base_path, filename)
        if not os.path.isfile(full_path):
            continue
        
        stats["total_files"] += 1
        file_size = os.path.getsize(full_path)
        
        if is_dummy_file(full_path):
            stats["dummies"] += 1
            stats["dummy_space_bytes"] += file_size
        else:
            stats["originals"] += 1
            stats["original_space_bytes"] += file_size
    
    return stats
