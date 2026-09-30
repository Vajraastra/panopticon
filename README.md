# Panopticon

A modular desktop toolkit for image collection curation — built for dataset preparation for diffusion model training, but equally useful for maintaining and editing any large image library.

Panopticon is not a single tool. It is a **visual orchestration framework**: a dashboard from which you launch specialized modules, each focused on one task, each independent from the others.

---

## Modules

| Module | What it does |
|---|---|
| **Cherry-DL** | Mass downloader for artist collections from Kemono, Patreon, and Pixiv. Create a project from a URL, queue several artists, and watch progress per worker while it syncs incrementally. Builds the raw library the other modules curate. |
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

Current Panopticon processes share one writer owner per central library. A second
instance refuses to open that database before migration; read-only tools can still
compare or export. The persistent `<database>.panopticon-writer.guard` stays beside
the database. Close the owner before running a tool that needs its own writer.
This protection coordinates current versions; older instances or direct SQLite tools
require separate coordination. Hard link aliases of the database require manual review.

Every module that transforms files uses `StampLib` to embed original metadata into the output, so no provenance is lost.

---

## Requirements

- **Windows 10/11** — the primary, tested target. Linux support is best-effort.
- Python 3.13 — downloaded and managed automatically by [`uv`](https://astral.sh/uv); no system Python required.
- PySide6 (installed from `requirements.txt`).
- GPU recommended for AI modules (YOLOv8 auto-bbox, WD tagger, face recognition).

## Running

Panopticon ships a self-contained, portable environment driven by `uv`.

**Windows (recommended)** — just run the launcher. It installs `uv` if missing, creates/repairs the venv with the managed Python 3.13, installs dependencies, and starts the app:

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
uv venv --python 3.13
uv pip install -r requirements.txt
uv run python main.py
```

---

## Cherry-DL: organize and extract archives

Select a project and click **Organize and extract ZIP/RAR** in its inspector.
This is a manual action, unavailable while downloads are running. Images stay in
the project root; other cataloged files move to `Comprimidos/`, `Proyectos/`, `Videos/`,
`Audio/`, `Documentos/` or `Otros/`. ZIP/RAR files in `Comprimidos/` are extracted and deduplicated
by SHA-256, using Cherry's existing numbering and catalog.

An archive is deleted only after every extracted file is verified. Its download
record and `.cherry-archives/` manifest remain, preventing a new download. Keep that
folder alongside `catalog.db`. Failed, password-protected or split archives stay
available for another manual attempt. Nested archives are available for the next
manual pass. Installation is automatic through `run.bat`; no system 7-Zip or
WinRAR is required.

## Portable curation export

In Librarian, select a folder, clear tag filters and click **Export folder curation**.
The export includes all indexed copies, offline versions and subfolders, regardless of the visible page.
Progress and errors appear beside the button. **Review folder curation** opens a
read-only comparison in pages of up to 100 copies. Select a row to see both states;
large text previews are marked incomplete. **Use exported curation** explicitly
replaces only the selected registered copy's tags, sources and rating. It requires
known identity and complete previews, verifies the current file SHA in a worker,
and rejects changes made since the displayed review. **Register and restore copy**
explicitly registers an absent path after verifying its current bytes. **Complete
identity and restore** is available only when both central hashes are unknown;
it preserves the reviewed record and rejects concurrent changes. Old paths and
other copies are retained. Unknown exported identity and incompatible shared tag
categories remain unresolved. Restart the
review if the export changes. Each page reflects the central database when read.
To export or compare from the command line:

```bat
venv\Scripts\python.exe scripts\curation_sidecar.py export --db panopticon.db --folder "G:\Collections\Artist"
venv\Scripts\python.exe scripts\curation_sidecar.py compare --db panopticon.db --folder "G:\Collections\Artist"
```

Review both states in the comparison output. Each copy includes `copy_id`, `relative_path`,
`export_id` and the fingerprint of the displayed central state. To explicitly choose its
exported curation, close the application owner and pass those reviewed values:

```bat
venv\Scripts\python.exe scripts\curation_sidecar.py apply --db panopticon.db --folder "G:\Collections\Artist" --mode restore --copy-id 1 --relative-path "image.png" --export-id "<reviewed export_id>" --expected-fingerprint "<reviewed fingerprint>"
```

Use `--mode register` only for a reviewed absent path, omitting the fingerprint. Use
`--mode adopt` only for a reviewed record with both central hashes unknown, including
its fingerprint. The mode is explicit; changes since review, unknown identity and shared
category conflicts remain unresolved. Each command verifies current bytes and applies
one copy through the central writer. `apply` requires an existing initialized library
already using WAL; it does not create, migrate or convert a database. Initialize or review
the library separately first. Comparison and recovery commands do not apply curation.

The export creates `panopticon.curation.db` alongside the folder's files, using relative
paths and cataloged hashes. Copies retain separate curation even when their hashes match.
Version 2 retains multiple versions of the same relative path and their offline status;
version 1 exports remain readable. Offline entries and paths with offline history require
review in the copy-version catalog before regular curation restoration.
Cherry's `catalog.db` and image files are preserved. The compare command reports both values
for conflicts; it does not apply changes. Hashes are cataloged identities, not a fresh check
of image bytes. Generation metadata and training captions are outside this export.

An interrupted export can leave a lock and temporary files. Inspect them, review the
reported file list, then quarantine that exact state using its returned fingerprint:

```bat
venv\Scripts\python.exe scripts\curation_sidecar.py inspect-recovery --folder "G:\Collections\Artist"
venv\Scripts\python.exe scripts\curation_sidecar.py quarantine-recovery --folder "G:\Collections\Artist" --expected-fingerprint "<fingerprint from inspection>"
```

Quarantine keeps the interrupted files and a recovery plan in a new folder. It preserves
the published sidecar, central database, Cherry catalogs and images. An active export
blocks recovery immediately. The persistent `.panopticon.curation.db.guard` is normal;
keep it with the folder. Recovery recognizes only current export manifests and matching
file identities. Legacy empty locks, unclaimed temporaries and changed identities require
manual review. A partially failed quarantine retains its plan and remaining lock; inspect
again before retrying. This is cooperative protection tested on local Windows storage.

In Librarian, **Recover interrupted export…** opens a folder chooser even when that
folder is not indexed. Its nonmodal window inspects in a worker and displays the full
resource list, export token and fingerprint. Choose **Quarantine listed resources** only
after reviewing that list; a changed state rejects the action and requires another
inspection. Errors preserve resources for review, including any partial recovery plan.
Closing the window cancels work that has not started and drops late results. A quarantine
already started continues in its worker; closing does not undo it. Application shutdown
does not yet guarantee draining every module worker.

Cherry catalogs retain download history even when a physical copy has been moved or
removed after deduplication. Alternate URLs confirmed as the same cataloged content are
saved in `catalog.db`, including URLs without a logical post identity. Profile merging
preserves these receipts and logical aliases before retiring the source profile. Keep
the catalog with its folder; deleting catalog history also deletes its download safeguard.
Preview records do not block an original download. Conflicting URL/content histories
stop the merge for review instead of replacing the earlier identity.

Download and merge names skip occupied paths if a counter is stale. This protects existing copies
under normal cooperative use; it does not reserve a destination against external programs
creating a file after the check.

## Offline copies and independent versions

Librarian's **Copy versions and offline catalog…** lists stored identities in pages of
100. Offline copies retain their tags, rating, provenance and generation metadata, but
are excluded from normal counts, searches, selections, previews and hash backfill.
Confirmed missing files and removed watched folders are archived rather than deleted.
An incomplete or unavailable inventory leaves records unchanged.

Select **All copy versions** to review active copies too. Select an ID and read its
complete stored state. **Keep offline** archives that identity without touching file
bytes. **Verify and make available** computes the current SHA-256 in a worker. If it
matches the stored current hash, the same ID and curation become available. Different
or unknown content creates a new ID with empty curation, provenance and generation
metadata while preserving the earlier version offline. An occupied active path or
changed reviewed state stops the action. Scanning alone never reactivates history.
The viewer binds edits to its displayed copy ID and rejects a replaced identity.

The initial native schema migration creates a SQLite backup beside the database before
rebuilding path uniqueness for active copies. Unsupported custom schemas are preserved
and rejected for review. Command-line curation application requires the migrated schema
and never performs that migration itself. Review supports at most 256 tags, 65,536
characters per stored field and 1 MiB serialized state; larger records remain stored
and require separate review. Closing the catalog cancels pre-admission verification;
an accepted database action continues, and the catalog can be reopened to see its result.
File verification and the SQLite commit are separate operations, without an atomic
snapshot spanning external file changes. These actions preserve Cherry catalogs and
their download receipts. Global preference for the curated duplicate remains under development.

## License

Business Source License 1.1 — free for non-commercial use.  
Commercial use requires a separate license. See [LICENSE](LICENSE) for details.  
Converts to MIT on 2030-04-18.
