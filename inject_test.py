
import struct
import zlib
import os

def inject_metadata(input_path, output_path, metadata):
    try:
        with open(input_path, 'rb') as f:
            data = f.read()
        
        if data[:8] != b'\x89PNG\r\n\x1a\n':
            print("Error: Input is not a PNG")
            return

        new_data = data[:33] # Sig + IHDR
        pos = 33
        
        for key, value in metadata.items():
            # Simple tEXt chunk: key\x00value
            content = key.encode('latin-1') + b'\x00' + value.encode('latin-1')
            length = len(content)
            chunk = struct.pack('>I', length) + b'tEXt' + content
            crc = zlib.crc32(b'tEXt' + content) & 0xffffffff
            new_data += chunk + struct.pack('>I', crc)
        
        new_data += data[33:]
        
        with open(output_path, 'wb') as f:
            f.write(new_data)
        print(f"Success! Created {output_path}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    img1 = r'C:\Users\nemph.PANOPTICON\.gemini\antigravity\brain\02f42156-6ed2-4c34-b6e7-13843326639e\uploaded_image_1766659963683.png'
    # Forge sample with Lora tags and VAE
    forge_val = """score_9, <lora:cool_style:0.8>, <lora:face_details:0.5> (masterpiece) beautiful lighting
Negative prompt: score_4, score_5, lowres
Steps: 30, Sampler: Euler a, Schedule type: Karras, CFG scale: 6, Seed: 349403070, Size: 512x1024, Model: waiNSFWIllustrious_v150, VAE: anime_vae.safetensors, Lora hashes: "cool_style: fb107, face_details: 4b54" """
    inject_metadata(img1, "test_forge.png", {"parameters": forge_val})
    
    img2 = r'C:\Users\nemph.PANOPTICON\.gemini\antigravity\brain\02f42156-6ed2-4c34-b6e7-13843326639e\uploaded_image_1766660863782.png'
    # Comfy sample with VAELoader and LoraLoader
    comfy_val = '{"4": {"inputs": {"ckpt_name": "waiIllustriousSDXL_v160.safetensors"}, "class_type": "CheckpointLoaderSimple"}, "5": {"inputs": {"text": "photorealistic, mountain"}, "class_type": "CLIPTextEncode", "_meta": {"title": "Positive"}}, "10": {"inputs": {"vae_name": "realism_vae.pt"}, "class_type": "VAELoader"}, "11": {"inputs": {"lora_name": "detail_lora.safetensors", "strength_model": 0.75}, "class_type": "LoraLoader"}, "28": {"inputs": {"seed": 431433362471142, "steps": 20, "cfg": 8.0, "sampler_name": "euler"}, "class_type": "KSampler"}}'
    inject_metadata(img2, "test_comfy.png", {"prompt": comfy_val})
