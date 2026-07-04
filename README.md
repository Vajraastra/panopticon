# Panopticon

A modular desktop toolkit for image collection curation — built for dataset preparation for diffusion model training, but equally useful for maintaining and editing any large image library.

Panopticon is not a single tool. It is a **visual orchestration framework**: a dashboard from which you launch specialized modules, each focused on one task, each independent from the others.

---

## Modules

| Module | What it does |
|---|---|
| **Cherry-DL** | Mass downloader for artist collections from Kemono, Patreon, and Pixiv. Builds the raw library the other modules curate. |
| **Librarian** | Central library manager. Scans folders, builds an indexed collection, provides thumbnails and lazy loading for large sets. |
| **Gallery** | Visual browser for your collection. Paginates and displays images with filtering and sorting. |
| **Character Recognizer** | Identifies and tags characters within a set using face recognition (ArcFace + YuNet) with landmark alignment. Supports real photos, 3D renders, and illustration/anime. |
| **Smart Cropper** | Batch-crops images to target aspect ratios. Supports standard dataset ratios, monitor formats, and mobile sizes. |
| **Duplicate Finder** | Detects duplicate and near-duplicate images using perceptual hashing. |
| **Format Scanner** | Analyzes the distribution of image formats across a collection for statistical review. |
| **Image Optimizer** | Compresses and re-encodes images in batch. Supports AVIF, PNG (via oxipng), and standard formats. |
| **Format Converter** | Converts between image formats in batch, including PSD and AVIF support. |
| **Watermarker** | Batch watermarking with configurable position, opacity, and scale. Preserves original metadata in output files via StampLib. |
| **Metadata Hub** | Inspect and edit embedded metadata (EXIF, tags, captions) across your collection. |
| **Dataset Tagger** | Auto-captions image folders with a local or online vision LLM plus a WD tagger, producing kohya-style `.txt` sidecars for training datasets (booru tags and natural language, per-model templates). |
| **Pickler** | Manual culling tool — quickly keep or discard images with two error-proof modes, wired into the Librarian for directed re-indexing. |

---

## Architecture

Panopticon is built on three design principles:

- **Atomic modules** — each module does one thing well and can be dropped into the system by placing a folder in `/modules`. The core discovers it automatically.
- **No inter-module coupling** — modules never import each other. All communication goes through a central `EventBus`.
- **Non-blocking UI** — all heavy processing (AI inference, batch operations) runs in background `QThread` workers. The UI never freezes.

Every module that transforms files uses `StampLib` to embed original metadata into the output, so no provenance is lost.

---

## Requirements

- **Windows 10/11** — the primary, tested target. Linux support is best-effort.
- Python 3.12 — downloaded and managed automatically by [`uv`](https://astral.sh/uv); no system Python required.
- PySide6 (installed from `requirements.txt`).
- GPU recommended for AI modules (YOLOv8 auto-bbox, WD tagger, face recognition).

## Running

Panopticon ships a self-contained, portable environment driven by `uv`.

**Windows (recommended)** — just run the launcher. It installs `uv` if missing, creates/repairs the venv with the managed Python 3.12, installs dependencies, and starts the app:

```
run.bat
```

**Linux (best-effort, untested):**

```bash
chmod +x run.sh && ./run.sh
```

**Manual, with `uv`:**

```bash
git clone https://github.com/Vajraastra/panopticon.git
cd panopticon
uv venv --python 3.12
uv pip install -r requirements.txt
uv run python main.py
```

---

## License

Business Source License 1.1 — free for non-commercial use.  
Commercial use requires a separate license. See [LICENSE](LICENSE) for details.  
Converts to MIT on 2030-04-18.
