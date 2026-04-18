# Panopticon

A modular desktop toolkit for image collection curation — built for dataset preparation for diffusion model training, but equally useful for maintaining and editing any large image library.

Panopticon is not a single tool. It is a **visual orchestration framework**: a dashboard from which you launch specialized modules, each focused on one task, each independent from the others.

---

## Modules

| Module | What it does |
|---|---|
| **Librarian** | Central library manager. Scans folders, builds an indexed collection, provides thumbnails and lazy loading for large sets. |
| **Gallery** | Visual browser for your collection. Paginates and displays images with filtering and sorting. |
| **Quality Scorer** | Two-phase AI-powered filter. Phase 1 (Slop Filter) removes anatomically broken or low-quality images using YOLOv8-pose, MediaPipe Hands, YuNet, and CLIP aesthetic scoring. Phase 2 ranks survivors by technical quality (sharpness, artifacts, resolution, color, composition). |
| **Character Recognizer** | Identifies and tags characters within a set using face recognition (ArcFace + YuNet) with landmark alignment. Supports real photos, 3D renders, and illustration/anime. |
| **Smart Cropper** | Batch-crops images to target aspect ratios. Supports standard dataset ratios, monitor formats, and mobile sizes. |
| **Duplicate Finder** | Detects duplicate and near-duplicate images using perceptual hashing. |
| **Image Optimizer** | Compresses and re-encodes images in batch. Supports AVIF, PNG (via oxipng), and standard formats. |
| **Watermarker** | Batch watermarking with configurable position, opacity, and scale. Preserves original metadata in output files via StampLib. |
| **Metadata Hub** | Inspect and edit embedded metadata (EXIF, tags, captions) across your collection. |
| **Format Converter** | Converts between image formats in batch, including PSD and AVIF support. |

---

## Architecture

Panopticon is built on three design principles:

- **Atomic modules** — each module does one thing well and can be dropped into the system by placing a folder in `/modules`. The core discovers it automatically.
- **No inter-module coupling** — modules never import each other. All communication goes through a central `EventBus`.
- **Non-blocking UI** — all heavy processing (AI inference, batch operations) runs in background `QThread` workers. The UI never freezes.

Every module that transforms files uses `StampLib` to embed original metadata into the output, so no provenance is lost.

---

## Requirements

- Python 3.10+
- PySide6
- GPU recommended for Quality Scorer (YOLOv8, CLIP, MediaPipe)

```bash
git clone https://github.com/Vajraastra/panopticon.git
cd panopticon
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

Or use the included `run.sh`:

```bash
chmod +x run.sh && ./run.sh
```

---

## License

Business Source License 1.1 — free for non-commercial use.  
Commercial use requires a separate license. See [LICENSE](LICENSE) for details.  
Converts to MIT on 2030-04-18.
