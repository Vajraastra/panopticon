import os
import json
import struct
from PIL import Image, PngImagePlugin
# =========================
# METADATA UTILITIES
# =========================

def extract_png_metadata(path):
    """Extracts tEXt and iTXt chunks from a PNG file."""
    metadata = {}
    try:
        with open(path, 'rb') as f:
            sig = f.read(8)
            if sig != b'\x89PNG\r\n\x1a\n':
                return {}
            
            while True:
                length_bin = f.read(4)
                if not length_bin: break
                length = struct.unpack('>I', length_bin)[0]
                chunk_type = f.read(4)
                data = f.read(length)
                f.read(4) # CRC
                
                if chunk_type == b'tEXt':
                    parts = data.split(b'\x00', 1)
                    if len(parts) == 2:
                        metadata[parts[0].decode('latin-1', errors='ignore')] = parts[1].decode('latin-1', errors='ignore')
                elif chunk_type == b'iTXt':
                    parts = data.split(b'\x00', 5)
                    if len(parts) >= 6:
                        metadata[parts[0].decode('utf-8', errors='ignore')] = parts[5].decode('utf-8', errors='ignore')
        return metadata
    except:
        return {}

def extract_jpeg_metadata(path):
    """Extracts EXIF UserComment if present."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if exif and 0x9286 in exif:
                return {"UserComment": exif[0x9286]}
    except:
        pass
    return {}

# =========================
# ANALYSIS LOGIC
# =========================

def has_real_transparency(image):
    """Detects if an alpha channel actually contains transparent pixels."""
    if image.mode not in ('RGBA', 'LA') and 'transparency' not in image.info:
        return False
    
    if image.mode == 'P':
        image = image.convert('RGBA')
    
    alpha = image.split()[-1]
    # getextrema() returns (min, max) values of the channel
    extrema = alpha.getextrema()
    return extrema[0] < 255

def analyze_image(path):
    """Analyzes image complexity to suggest optimal format."""
    try:
        with Image.open(path) as img:
            has_alpha = has_real_transparency(img)
            
            # For complexity, we check unique colors (limited to 5000 for speed)
            colors = img.getcolors(5000)
            color_count = len(colors) if colors else 5001
            
            suggestion = "JPEG"
            reason = "High complexity/photo-like"
            
            if has_alpha:
                suggestion = "PNG"
                reason = "Has transparency"
            elif color_count < 256:
                suggestion = "PNG"
                reason = "Low color count (graphics/UI)"
            elif color_count < 4096:
                suggestion = "WebP"
                reason = "Medium complexity"
                
            return {
                "format": img.format,
                "mode": img.mode,
                "size": img.size,
                "has_transparency": has_alpha,
                "color_count": color_count if color_count <= 5000 else ">5000",
                "suggested_format": suggestion,
                "suggestion_reason": reason
            }
    except Exception as e:
        return {"error": str(e)}

# =========================
# PROCESSING PIPELINE
# =========================

def resize_image(image, max_side=None, width=None, height=None, lock_aspect=True):
    """Resizes image. If max_side is provided, scales the longest side to that value."""
    orig_w, orig_h = image.size
    
    if max_side:
        if orig_w > orig_h:
            ratio = max_side / orig_w
        else:
            ratio = max_side / orig_h
        new_w = int(orig_w * ratio)
        new_h = int(orig_h * ratio)
    elif width and height:
        if lock_aspect:
            ratio = min(width / orig_w, height / orig_h)
            new_w = int(orig_w * ratio)
            new_h = int(orig_h * ratio)
        else:
            new_w, new_h = width, height
    elif width:
        ratio = width / orig_w
        new_w = width
        new_h = int(orig_h * ratio)
    elif height:
        ratio = height / orig_h
        new_w = int(orig_w * ratio)
        new_h = height
    else:
        return image

    return image.resize((new_w, new_h), Image.Resampling.LANCZOS)

def optimize_image(source_path, output_path, 
                   format_override=None, 
                   quality=90, 
                   max_side=None,
                   resize_width=None, 
                   resize_height=None,
                   lock_aspect=True,
                   preserve_metadata=True):
    """
    Complete optimization pipeline:
    Extract Metadata -> Resize -> Convert -> Optimize -> Re-inject
    """
    try:
        # 1. Extract Metadata
        metadata = {}
        original_ext = os.path.splitext(source_path)[1].lower()
        if preserve_metadata:
            if original_ext == '.png':
                metadata = extract_png_metadata(source_path)
            elif original_ext in ('.jpg', '.jpeg'):
                metadata = extract_jpeg_metadata(source_path)

        # 2. Load Image
        img = Image.open(source_path)
        
        # 3. Resize
        if max_side or resize_width or resize_height:
            img = resize_image(img, max_side=max_side, width=resize_width, height=resize_height, lock_aspect=lock_aspect)

        # 4. Handle Format & Auto-conversion
        target_format = format_override.upper() if format_override else img.format
        if not target_format: target_format = "PNG" # Fallback
        
        # JPEG + AI Metadata -> Auto-convert to PNG
        is_ai_metadata = any(k in metadata for k in ("parameters", "prompt"))
        if target_format in ("JPEG", "JPG") and is_ai_metadata:
            target_format = "PNG"
            # Adjust output path extension
            base = os.path.splitext(output_path)[0]
            output_path = base + ".png"

        # 5. Prepare Output Path Extension
        ext_map = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
        actual_ext = ext_map.get(target_format, ".png")
        if not output_path.lower().endswith(actual_ext):
            output_path = os.path.splitext(output_path)[0] + actual_ext

        # 6. Save with Optimization
        save_args = {"optimize": True}
        
        if target_format == "PNG":
            save_args["compress_level"] = 9
            if preserve_metadata and metadata:
                pnginfo = PngImagePlugin.PngInfo()
                for k, v in metadata.items():
                    if k != "UserComment": # Don't inject JPEG comment into PNG directly
                        pnginfo.add_text(str(k), str(v))
                save_args["pnginfo"] = pnginfo
        
        elif target_format in ("JPEG", "JPG"):
            save_args["quality"] = quality
            # Ensure RGB mode for JPEG
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            if preserve_metadata and "UserComment" in metadata:
                exif = img.getexif()
                exif[0x9286] = metadata["UserComment"]
                save_args["exif"] = exif
                
        elif target_format == "WEBP":
            save_args["quality"] = quality
            save_args["method"] = 6
        
        img.save(output_path, **save_args)
        
        # 7. Stats
        orig_size = os.path.getsize(source_path)
        new_size = os.path.getsize(output_path)
        
        return {
            "success": True,
            "output_path": output_path,
            "original_size": orig_size,
            "new_size": new_size,
            "saved_bytes": orig_size - new_size,
            "saved_percent": ((orig_size - new_size) / orig_size) * 100 if orig_size > 0 else 0,
            "converted": target_format != (img.format if img.format else "PNG")
        }

    except Exception as e:
        return {"success": False, "error": str(e)}

def get_export_path(source_path, export_dir="optimized", suffix=""):
    """Generates an export path in the target directory. If suffix is empty, keeps original name."""
    if not os.path.exists(export_dir):
        os.makedirs(export_dir, exist_ok=True)
    filename = os.path.basename(source_path)
    if suffix:
        name, ext = os.path.splitext(filename)
        filename = f"{name}{suffix}{ext}"
    return os.path.join(export_dir, filename)
