import struct
import json
import os
from PIL import Image

class UniversalParser:
    @staticmethod
    def parse_image(path):
        ext = os.path.splitext(path)[1].lower()
        stats = UniversalParser._get_file_stats(path)
        
        result = {"stats": stats, "raw": {}, "positive": "", "negative": "", "tool": "Unknown"}
        
        if ext == '.png':
            png_data = UniversalParser._parse_png(path)
            result.update(png_data)
        elif ext in ('.jpg', '.jpeg', '.webp'):
            jpeg_data = UniversalParser._parse_jpeg(path)
            result.update(jpeg_data)
        
        return result

    @staticmethod
    def _get_file_stats(path):
        try:
            stat = os.stat(path)
            import datetime
            return {
                "size": f"{stat.st_size / 1024:.2f} KB",
                "created": datetime.datetime.fromtimestamp(stat.st_ctime).strftime('%Y-%m-%d %H:%M:%S'),
                "modified": datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                "format": os.path.splitext(path)[1][1:].upper()
            }
        except:
            return {}

    @staticmethod
    def _parse_png(path):
        metadata = {}
        try:
            with open(path, 'rb') as f:
                sig = f.read(8)
                if sig != b'\x89PNG\r\n\x1a\n':
                    return {"error": "Invalid PNG"}
                
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
            
            return UniversalParser._extract_prompts(metadata)
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _parse_jpeg(path):
        """Extracts EXIF metadata from JPEG/WEBP files, focusing on AI prompt data."""
        metadata = {}
        try:
            with Image.open(path) as img:
                exif = img.getexif()
                if exif:
                    # 0x9286 = UserComment (where A1111/NAI store prompts)
                    if 0x9286 in exif:
                        user_comment = exif[0x9286]
                        # Handle bytes vs string
                        if isinstance(user_comment, bytes):
                            user_comment = user_comment.decode('utf-8', errors='ignore')
                        metadata["parameters"] = user_comment
                    
                    # 0x010F = Make (sometimes used for tool info)
                    if 0x010F in exif:
                        metadata["Make"] = str(exif[0x010F])
                    
                    # 0x0131 = Software
                    if 0x0131 in exif:
                        metadata["Software"] = str(exif[0x0131])
                
                # Also check for XMP data (some tools embed there)
                if hasattr(img, 'info') and 'xmp' in img.info:
                    metadata["xmp"] = img.info['xmp']
                    
            return UniversalParser._extract_prompts(metadata)
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _extract_prompts(raw_metadata):
        processed = {
            "raw": raw_metadata, 
            "positive": "", 
            "negative": "", 
            "tool": "Unknown",
            "model": "Unknown",
            "seed": "Unknown",
            "sampler": "Unknown",
            "steps": "Unknown",
            "cfg": "Unknown",
            "vae": "Unknown",
            "loras": []
        }
        
        # Normalize keys to lowercase for searching
        norm_meta = {k.lower(): v for k, v in raw_metadata.items()}
        
        # 1. A1111 / Forge / Universal 'parameters' tag
        if "parameters" in norm_meta:
            processed["tool"] = "A1111 / Forge"
            params = norm_meta["parameters"]
            
            # Extract Prompts
            if "negative prompt:" in params.lower():
                import re
                parts = re.split(r'negative prompt:', params, flags=re.IGNORECASE)
                processed["positive"] = parts[0].strip()
                if len(parts) > 1:
                    # Look for the start of technical parameters (Steps:)
                    if "\nsteps:" in parts[1].lower():
                        neg_parts = re.split(r'\nsteps:', parts[1], flags=re.IGNORECASE)
                        processed["negative"] = neg_parts[0].strip()
                        # Parse technical line
                        tech_line = "Steps:" + neg_parts[1]
                        UniversalParser._parse_a1111_tech(tech_line, processed)
                    else:
                        processed["negative"] = parts[1].strip()
            else:
                # No negative prompt, check for tech line anyway
                if "\nsteps:" in params.lower():
                    import re
                    parts = re.split(r'\nsteps:', params, flags=re.IGNORECASE)
                    processed["positive"] = parts[0].strip()
                    tech_line = "Steps:" + parts[1]
                    UniversalParser._parse_a1111_tech(tech_line, processed)
                else:
                    processed["positive"] = params

            # Extract LoRAs from Positive Prompt (tags like <lora:name:weight>)
            import re
            lora_tags = re.findall(r'<lora:([^:]+):([^>]+)>', processed["positive"])
            for name, weight in lora_tags:
                processed["loras"].append(f"{name} ({weight})")
        
        # 2. ComfyUI 'prompt' tag (JSON Graph)
        elif "prompt" in norm_meta:
            processed["tool"] = "ComfyUI"
            try:
                prompt_json = json.loads(norm_meta["prompt"])
                pos_list = []
                neg_list = []
                
                for node_id, node in prompt_json.items():
                    class_type = node.get("class_type", "")
                    inputs = node.get("inputs", {})
                    meta = node.get("_meta", {})
                    title = meta.get("title", "").lower()
                    
                    # 1. Direct Prompt Extraction
                    if class_type == "CLIPTextEncode":
                        text = str(inputs.get("text", "")).strip()
                        if text:
                            if "negative" in title or "neg" in title:
                                neg_list.append(text)
                            else:
                                pos_list.append(text)
                    
                    # 2. Extract technical stats from KSampler nodes
                    if "KSampler" in class_type:
                        processed["seed"] = inputs.get("seed", processed["seed"])
                        processed["steps"] = inputs.get("steps", processed["steps"])
                        processed["cfg"] = inputs.get("cfg", processed["cfg"])
                        processed["sampler"] = inputs.get("sampler_name", processed["sampler"])

                    # 3. Model info from CheckpointLoader
                    if "CheckpointLoader" in class_type or class_type == "CheckpointLoaderSimple":
                        processed["model"] = inputs.get("ckpt_name", processed["model"])

                    # 4. VAE Info
                    if "VAELoader" in class_type:
                        processed["vae"] = inputs.get("vae_name", processed["vae"])
                    
                    # 5. LoRA Info
                    if "LoraLoader" in class_type:
                        lora_name = inputs.get("lora_name", "Unknown")
                        strength = inputs.get("strength_model", "1.0")
                        processed["loras"].append(f"{lora_name} ({strength})")

                processed["positive"] = "\n---\n".join(list(dict.fromkeys(pos_list)))
                processed["negative"] = "\n---\n".join(list(dict.fromkeys(neg_list)))
                processed["raw_json"] = prompt_json
            except:
                pass

        return processed

    @staticmethod
    def _parse_a1111_tech(line, processed):
        # Format: Steps: 30, Sampler: Euler a, Schedule type: Karras, CFG scale: 6, Seed: 349403070, ...
        # Use simple splits for speed
        parts = line.split(",")
        for p in parts:
            p = p.strip()
            if ":" not in p: continue
            k, v = p.split(":", 1)
            k = k.strip().lower()
            v = v.strip()
            
            if k == "steps": processed["steps"] = v
            elif k == "sampler": processed["sampler"] = v
            elif k == "cfg scale": processed["cfg"] = v
            elif k == "seed": processed["seed"] = v
            elif k == "model": processed["model"] = v
            elif k == "vae": processed["vae"] = v
            elif k == "lora hashes":
                # LoRA hashes often look like "name: hash, name2: hash2"
                # For now just keep the raw string if needed, or parse names
                pass
