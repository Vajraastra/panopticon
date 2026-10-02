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
| **Duplicate Finder** | Reviews exact SHA-256 duplicates with curation priority and recoverable retirement; visual similarity stays review-only. |
| **Format Scanner** | Analyzes the distribution of image formats across a collection for statistical review. |
| **Image Optimizer** | Compresses and re-encodes images in batch. Supports AVIF, PNG (via oxipng), and standard formats. |
| **Watermarker** | Batch watermarking with configurable position, opacity, and scale. Deliberately removes embedded generation metadata from public output files. |
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


After participating producers finish, dashboard shutdown requests the central
database writer to close admission and drain accepted commands. The window keeps
the Qt event loop alive until the writer has closed its connection and released
ownership; it does not wait for the writer on the GUI thread, even with a full
command queue. Pending cancelled commands are skipped and failed transactions
resolve their futures. Blocking `close()` remains available for CLI/tests.
This writer guarantee does not yet cover every module's own shutdown handler.

Gallery closes its viewers/readers and rejects new inputs during shutdown. It
waits for actual reader completion and its own thumbnail pool; queued thumbnails
are cleared without touching other tools' pools. A decode already in progress
finishes in its worker. Optimizer also reports native worker readiness, including
accepted derivative registration and receipt confirmation after cancellation.
Format Scanner counts files as a stream, supports cooperative cancellation and
always emits a terminal result. Interrupted or failed scans are not presented as
complete reports. Closing keeps its worker retained until native completion.
Cherry now cancels its active download/maintenance through their existing
cooperative cleanup and closes runtime admission without waiting on the GUI.
Actual admitted tasks finish before the loop, async generators and executor close;
a cancelled Future alone does not establish completion. Already accepted short
queries/mutations drain. The runtime stays retained until native completion and
cannot restart through late callbacks. A timeout in the blocking compatibility
API reports unfinished work and keeps ownership; it never terminates the thread.
Dataset Tagger retains discovery, connection, model download, caption, detection,
tag database and refinement workers until native completion. Its auxiliary editors
also defer close, accept/reject and Escape while workers finish; late results cannot
apply edits or reopen output folders during shutdown. Caption review saves its
current edit once on close, as before. Grid replacement cancels stale thumbnails
without GUI waits, retaining at most one decoder and the latest pending set.
Requests or inference already in progress finish in their worker; no force kill.
Refinement proposals belong to the captured image/text/architecture, and changing
that context discards the proposal. Auxiliary copy-version/curation/recovery dialogs also defer close, accept/reject
and Escape until their native readers finish. Librarian includes its own and its
dialogs' readers in shutdown readiness, so admitted copy/curation actions finish
before the central writer closes. Recognizer waits for its Native reader and
recognition workers before closing profile admission, then waits for accepted
profile work and SQLite cleanup. Profile blocking joins remain CLI/test teardown.
Native reader startup failures deliver a terminal error instead of leaving busy
controls stranded.
Active QThreads now register before start and retain their owners until a
nonblocking `wait(0)` succeeds, including deferred Qt/native cleanup after
`finished`. Dashboard checks this registry before closing Native, even when an
older result handler cleared its worker field. A requested `deleteLater` is
deferred while cleanup runs and completed on the creator thread only after join;
no GUI join or thread termination. Tagger's direct-close scopes and Cherry's
stopped notification also wait for the final join. Qt documents this distinction:
https://doc.qt.io/qt-6.8/qthread.html#isRunning
 Remaining module error/admission paths and live usage remain
under audit.

Cropper and Image Optimizer can preserve embedded generation
metadata in their outputs through the shared metadata layer. Watermarker deliberately
removes it for public distribution. Central curation and `.txt` captions remain
separate; exporting metadata does not merge native copy identities.

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

Windows setup serializes environment preparation with a project-local lock.
It validates that Python starts and matches 3.13. A damaged/incompatible `venv`
is moved to `venv.repair-<unique ID>` before creating its replacement; the old
environment remains available for manual recovery. Linked environment/cache
paths are rejected. Dependency installation uses a dedicated `.uv-launcher-cache`
and an explicit 15-minute timeout per attempt, with at most three attempts. Before
retrying, it removes orphaned `.tmp*` directories only inside that cache. Timed-out
processes and their captured children are terminated and checked before retry.
Failures pause the native batch launcher; the application starts only after setup
succeeds, and its exit status is preserved. No CUDA migration is performed.

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
An incomplete or unavailable inventory leaves unverified records unchanged.
Missing-file cleanup also requires an accessible, listable parent with stable identity;
a healthy collection root does not prove absence under a disconnected or replaced
subfolder. Linked/reparse parents require separate review. A file that reappears during
verification is skipped. Only a complete observed inventory admits archival by captured
copy ID and full state fingerprint, in batches of up to 256; changed identities or curation
reject the entire affected batch. No tags, hashes, generation, FTS or Cherry receipts are
deleted. Scan results show confirmed offline identities. Earlier confirmed batches remain
if a later batch fails or cancellation occurs; admitted errors remain visible. Filesystem
and SQLite observations do not form an atomic transaction.

In Librarian, select a watched folder and choose **Archive folder**. A worker
previews its indexed scope before a manual confirmation, with Cancel selected by default.
It shows active, already offline and total identities, including subfolders and nested
watched roots. The commit removes only the selected watched root and marks its active
copies offline; nested watched roots remain registered. No file is deleted or moved.
IDs, curation, generation, hashes, FTS and Cherry download receipts are preserved.
The folder may be disconnected: this explicit catalog action requires no disk access.

The confirmed scope is checked again inside the native owner transaction. Changes to
members, paths, availability or watched roots require a new preview; newer curation is
preserved. Checks are limited to one million identities, 10 seconds per preview and a
30-second owner transaction. Exceeding a limit rejects the action, without accepting a
partial scope. Only a confirmed commit reports success. Indexing and hashing in Librarian
wait while it runs. Closing cancels before admission; an admitted command finishes in its
retained worker without blocking Qt. Reactivation requires explicit copy-ID review.
This does not pause Cherry downloads or coordinate every module's shutdown.

Select **All copy versions** to review active copies too. Select an ID and read its
complete stored state. **Keep offline** archives that identity without touching file
bytes. **Verify and make available** computes the current SHA-256 in a worker. If it
matches the stored current hash, the same ID and curation become available. Different
or unknown content creates a new ID with empty curation, provenance and generation
metadata while preserving the earlier version offline. An occupied active path or
changed reviewed state stops the action. Scanning alone never reactivates history.
The viewer binds edits to its displayed copy ID and rejects a replaced identity.

Gallery's advanced viewer decodes a QImage, reads file statistics and extracts
embedded metadata in its own worker. Each viewer keeps one active request and
the latest pending image; stale navigation/closed owners discard results. Source
identity, size and modification time are checked around loading to reject mixed
versions. Only the current preview is retained, with a maximum edge of 2048;
small sources are not enlarged by the decoder. Files over 256 MiB and sources
over 100 million pixels require review. Decoder plugins may allocate the full
source before scaling; this is not a total process-memory guarantee.

QPixmap creation/display stays on Qt's GUI thread. Fullscreen and resize reuse
the loaded preview without rereading files. Native and embedded curation are
combined only once the matching preview/snapshot are ready; native empty tags
and zero rating remain authoritative. Preview failure clears earlier embedded
state and preserves native curation. Decoder devices close explicitly, and error
delivery does not retain decoder tracebacks. Module shutdown tracks actual worker
completion without GUI waits. Cancellation is checked between phases; it cannot
forcibly interrupt a codec or blocked filesystem call. The viewer uses the active
theme with bilingual controls/tooltips; source bytes and Cherry data are untouched.
Character Recognizer also captures the native copy ID before decoding each image.
Manual tag additions/removals and reference learning require the displayed registered
identity and a confirmed curation commit. A changed native ID disables tag editing until
the batch is reloaded. Optional curation revisions use the complete stored state;
legacy snapshot consumers retain their two-query interface. The revision read uses
three bounded queries, up to 256 paths and 1 MiB, with a 10-second SQL budget.

Automatic recognition captures the whole selected scope before inference, in read
blocks of up to 256, without registering images. The scope is limited to 100,000 paths,
8 MiB of serialized bindings and 30 seconds for capture. Oversized selections fail
before approving a prefix or allocating a serialization of the entire large selection.
Bulk tagging submits one identity/revision at a time. A changed identity or stored
state stops the remaining batch and requires a fresh scan; confirmed tags remain.
A transient write failure can retry the unchanged tail. Missing native identities
never inherit tags through the path. This does not pin image bytes against external
replacement or make inference and SQLite an atomic transaction.

Recognizer workers remain retained through native thread completion. Closing cancels
pending reads and inference loops without blocking Qt; an admitted native write still
finishes. Application shutdown checks recognition workers before closing services;
the other remaining module workers are still outside this guarantee.

The initial native schema migration creates a SQLite backup beside the database before
rebuilding path uniqueness for active copies. Unsupported custom schemas are preserved
and rejected for review. Command-line curation application requires the migrated schema
and never performs that migration itself. Review supports at most 256 tags, 65,536
characters per stored field and 1 MiB serialized state; larger records remain stored
and require separate review. Closing the catalog cancels pre-admission verification;
an accepted database action continues, and the catalog can be reopened to see its result.
File verification and the SQLite commit are separate operations, without an atomic
snapshot spanning external file changes. These actions preserve Cherry catalogs and
their download receipts. Duplicate Finder applies curation priority to exact folder
scans and complete native groups across folders.

### Duplicate review and recovery

Select a folder in **Duplicate Finder**, choose **Exact content (SHA-256)** and scan.
Current file bytes and complete native copy states determine the recommendation. A
documented copy takes priority over a raw Cherry download even when all weights are
zero. Configurable weights compare tags, rating, generation data and provenance.
Conflicting or complementary information retains every copy for review. Unknown or
offline identities, links, physical aliases, captions and curation sidecars also block
retirement. The tree displays 100 copies per page; **View group in Gallery** sends the
complete image group, up to 1,000 images, for inspection.

For cross-folder discovery, select **Native library (exact groups)**, then **Find
native candidates**. Pages contain up to 25 active stored-hash groups. They read no
images and never authorize retirement. Select a group and **Verify selected group**
to read its current bytes and complete curation, up to 256 members. Changed, missing
or unreviewed copies block retirement; larger groups remain visible for separate
review without approving a subset. Pages reflect current catalog state.

**Prepare search index** explicitly builds or reuses the compatible partial index
through the central database owner. Reads and startup create no index. Construction
may hold other writes for up to 30 seconds, then rolls back if it exceeds its budget;
candidate reads have a 10-second budget. The index tracks hash and offline changes.
Global retirement requires the prepared index and rechecks every current member,
including additions or identity replacements since verification. Preparing the
index, changing options or selecting another group consumes the previous review.

**Select recommended copies** only marks safe exact candidates. **Retire selected
copies** shows the count, destination and full reviewed states for confirmation,
then rechecks every member, including the retained copy, before archiving the chosen
IDs. Their curation and history remain offline in the native database. Physical files
move to a persistent recovery folder on their volume, outside every source folder,
watched root and Cherry catalog ancestor. A retained copy may be on another volume. This action supports native Windows and never changes
Cherry catalog tables or download receipts. Known source URLs remain downloaded even
without their physical copy; previously unknown URLs may need one transfer to prove
that their content matches an existing hash.

Each retirement group has a durable recovery journal. A partial failure is shown with
its journal and moved paths; it requires review before retrying. SQLite and file moves
do not share an atomic transaction. Recovery moves do not free disk space. Explicit
restoration and manual permanent deletion by reviewed native ID are available below.
Neither action runs automatically. Dashboard closure keeps the event loop and catalog alive until Duplicate
Finder's accepted work finishes.

Folder scans admit at most 100,000 files, 1,000 groups, 2,000 candidate copies and a
4 MiB review. Exact hashing admits batches of 256 jobs; native snapshots and retirement
groups contain at most 256 copies. Watched-folder validation admits at most 256 roots.
Limits or inventory errors disable retirement instead of publishing a discardable
partial result. **Visual similarity** admits at most 2,000 images and 10 million pixels
per image, with four workers and bounded pair comparison. It never authorizes retirement
and performs no implicit cache writes. It can reuse previously saved pHash only after
checking current SHA-256; absent or unproven cache uses fresh decoding for review.

### Review recovery journals

Select **Recovery journal (review)** and paste or choose the `journal.json` shown
after a retirement. Press **Review recovery journal** to inspect one complete group.
Choosing a path does not start work. The worker validates journal structure and
contained destinations, reads native IDs and active occupants in one bounded
snapshot, then checks current SHA and file identity at original and recovery paths.
It also checks retained copies and external documentation. Neither the native DB,
the journal, image files nor Cherry catalogs are modified.

Recorded phases and move statuses are shown separately from observed evidence:
an interrupted process can leave them behind the actual commit or rename. The table
distinguishes verified retirements, offline IDs whose source is still present,
copies active at their original path, and cases requiring review. Replaced files with identical
SHA, changed native states, new occupants or unavailable paths remain pending.
A missing path counts as absent only when its parent is accessible; a disconnected
disk is not treated as proven loss. Select a copy to inspect IDs, both paths and
the retained copies. These observations do not authorize a restore or purge.

The review admits a journal up to 5 MiB, up to 256 native IDs, four file workers
with batches of at most 64 jobs, a 2 MiB report and a 10-second native read budget.
It rereads the journal before publishing evidence. Changes, limits, errors or
cancellation discard the entire result. The interface remains responsive and
dashboard closure waits for workers before closing the catalog. This is an
inspection at a point in time, without a global filesystem/SQLite transaction.

### Restore a reviewed copy by ID

After reviewing a recovery journal, select a verified offline copy and press
**Restore selected copy**. The confirmation shows its ID and captured paths;
**Cancel** is the default. A fresh worker check must still confirm the same native
state, journal, SHA, file identity and documentation before admitting effects.
Ambiguous copies, occupied paths, unavailable original folders or changed curation
require review. Folders are not recreated automatically.

For a verified retirement, Windows renames the recovered file to its empty original
path on the same volume, without overwriting. If retirement committed but the source
never moved, it reactivates that intact source without a file move. The owner rechecks
the complete native fingerprint, effective SHA and all active path occupants, including
case/Unicode aliases, within a 10-second check budget. It changes only the offline
flag of the same ID: tags, rating, generation, original/modified hashes, FTS and all
Cherry hash/URL/alias receipts are preserved. No new version inherits curation.

Each attempt has its own durable `restore-UUID.json`, preserving the original retirement
journal. A failed commit after a move leaves bytes at the original path with the ID
still offline; request a fresh review before retrying. No automatic file compensation
occurs. Once a move or owner command is admitted, cancellation or dashboard closure
waits for its verification, commit and journal without blocking the interface. Results
distinguish returned bytes from confirmed native restoration. A final journal failure
still reports a confirmed commit accurately. Subsequent actions require new evidence.
No global filesystem/SQLite transaction protects against later external changes.

### Manually delete a verified recovery copy

Review its retirement journal, select a **verified retired copy**, and press
**Prepare deletion preview**. This rechecks the current native state and information,
SHA-256, file identities and library scope. Inspect the displayed ID, recovery file,
size and verified retained copy. Then press **Permanently delete verified copy** and
confirm those captured values; **Cancel** is the default. A new selection or changed
evidence requires a new preview. Similarity or a recorded journal phase never approves
deletion. Only one copy is admitted per manual confirmation.

This permanently removes the recovery file, bypassing the Recycle Bin. It preserves
every native row, offline ID, tag, rating, generation record, hash, FTS entry, original
retirement journal and Cherry hash/URL/alias receipt. The curated copy takes priority
even with zero weights; complementary or conflicting information retains both copies.
External captions/sidecars, changed curation, replaced files, occupied original paths,
unavailable folders or ambiguous links require review. Recovery must remain outside
watched folders and portable projects. Deletion supports regular local Windows files;
network paths, alternate streams and hardlinked victims require manual review.

The worker repeats the preview and keeps the retained file protected against writes,
rename and deletion, while opening the victim exclusively. Those handles remain open
through the owner's serialized ID/state/scope approval and deletion of the exact opened
file. Native approval performs no database DML or schema changes. A separate durable
`purge-UUID.json` records each admitted attempt. Cancellation before marking deletion
preserves the victim, including while awaiting native approval; once marked, the
operation finishes and reports confirmed effects without blocking the interface.
Results distinguish approval, marking and confirmed physical deletion. A final journal
failure still reports a confirmed deletion; keep all journals for review. There is no
common filesystem/SQLite transaction or guarantee against later external changes.
Neither recovery folders nor Cherry receipts are automatically pruned.

### Explicit perceptual hash phase

Select **pHash by folders (save)**, enter 1–32 separate absolute indexed folders,
and press **Calculate and save pHash**. This phase is independent of SHA-256 backfill.
Images without a known native SHA are reported and require that separate phase first.
Choosing folders or changing budgets never starts calculation automatically.

The worker checks current bytes against the effective native SHA, with file identity
checks before and after processing. Decoding is surrounded by two SHA reads from the
same unbuffered handle. It saves derived hashes in native batches of at most 64,
preserving curation, generation metadata and all portable Cherry databases/receipts.
The evidence registry is prepared only by this explicit phase; ordinary reads and
startup do not create it. Cache evidence includes native copy ID, SHA and algorithm
library versions. Legacy values, changed content, offline copies or changed algorithm
versions cannot be reused. Even valid cache reuse reads current SHA before skipping
decoding. External changes after verification still require a subsequent check.

Adjust limits up to 100,000 known images per run and 10 million decoded pixels per
image. An initial image limit rejects the whole scope before preparation. Unknown
hashes, unreadable files, mismatches and oversized images are reported; at most 20
issue samples are shown. Confirmed batches survive later errors or cancellation.
The stored count includes only acknowledged commits. Dashboard closure waits without
blocking the event loop for accepted writes before closing the catalog. This phase
uses CPU workers and never authorizes retirement based on similarity.

### Folder overlap and pair classification

Select **Folder overlap (review only)** in Duplicate Finder, then paste absolute
indexed folder paths, one per line, or use **Add folder**. Compare 2–32 separate
scopes: repeated or nested folders are rejected. **Compare folder pairs** returns
every pair in the chosen batch, up to 496 pairs, from one native read snapshot.
No image decoding, indexing, Cherry catalog access or file changes occur.

The report counts active copies, known and unknown hashes, and unique effective
SHA-256 values per folder. Duplicate copies within a folder count once in its
hash set. Jaccard is shared unique hashes divided by their union. Select a pair
to inspect each folder's coverage and directional containment, which can differ
from Jaccard. Empty or unknown sets show insufficient evidence. The adjustable
percentage threshold, initially 51%, classifies high or partial overlap for review.
It never authorizes automatic merges or retirement; use exact group verification
for that decision, preserving curated copies and all Cherry download receipts.

Comparison admits at most 250,000 active copies across the selected scopes,
256 KiB of report data and a 10-second native read budget. A limit, cancellation
or error invalidates the entire report; no partial prefix is published. Choose
a smaller complete batch when needed. Changing folders or threshold consumes
the captured report. Scopes follow stored native paths; this classification does
not prove current physical bytes, aliases or complete curation.

### Metadata Hub: editing and generation history

Load an image and switch to **Editor mode** to replace its prompts. Clearing both
fields removes embedded generation metadata. **Save changes** requires a currently
indexed, writable original outside Cherry folders. Review the file and copy ID in
the confirmation before updating it. Reading metadata and decoding previews run in
a worker; the interface remains available for cancellation and coordinated closure.

Before publishing an edit, Panopticon saves the complete original and a recovery
journal under its `metadata_recovery` tool cache, outside watched collection roots.
The same native copy keeps its tags, rating, sources and immutable original hash.
Its current generation fields, current hash and search index update together with
a history record containing the previous native and embedded generation data.
**Generation history** shows earlier values in pages; the current curation remains
separate. A perceptual hash from different bytes requires explicit recalculation.

**Export copy** requires a fresh filename with the same extension, outside Cherry
folders and without existing native path history. It does not update the source's
native identity. JPEG edits preserve the compressed image stream; WebP output is
lossless. AVIF output is re-encoded and can change pixels. Supported files are
single-frame PNG, JPEG, WebP and AVIF, up to 256 MiB and 20 million pixels.

Results distinguish file publication from a confirmed native update. A partial
result requires review using its recovery journal and backup; there is no automatic
disk rollback or atomic transaction spanning the file and SQLite. External editors
can still race with revalidation. Reload the image before another operation.
Backups remain available after success and are not automatically deleted.
A native update is confirmed by a bounded historical receipt for its operation ID.
If the commit response is lost, Metadata Hub checks that receipt once without
resubmitting the edit. A missing or conflicting receipt leaves the published
file and backup for review. Confirmation proves the historical commit; later
file, identity or curation changes require separate current-state review.

### Reviewing a producer recovery journal

Inspect one explicit Metadata Hub, Cropper or Image Optimizer journal:

```bat
venv\Scripts\python.exe scripts\producer_journal.py --db "panopticon.db" --journal "cache\derived_recovery\<operation-id>\journal.json"
```

Metadata Hub journals use the `metadata_recovery` tool cache. This command opens
Native read-only and never prepares schema, resubmits a write, restores bytes,
edits a journal or deletes backups. Its JSON separates the historical receipt
from current file SHA, copy ID, curation fingerprint, generation and offline state.
`confirmed_current` describes the result copy; the parent may since have changed.
After an original metadata edit, the source's current bytes naturally differ from
its pre-edit SHA. Review `backup_matches_source` separately.

`published_bytes_without_receipt` needs explicit reconciliation; a confirmed
historical receipt with changed or missing current output never authorizes replay
or deletion. Native snapshots close before hashing and are checked again afterward.
Observed concurrent changes yield `changed_during_review`. Unknown/malformed
journals, unsafe paths and limits remain unresolved. Reviews are bounded to one
journal (2 MiB), files of 256 MiB and a report of 32 KiB. Current curation above
the existing copy-state limits fails explicitly. The review fingerprint identifies
this observation; it is not a reservation against external editors.

Exit 0 means a stable report was produced, including cases still requiring manual
review. Exit 1 means concurrent changes; exit 2 means inspection failed.
After inspecting the report, explicit reconciliation requires its exact fingerprint.
Close the application's Native writer before using the CLI:

```bat
venv\Scripts\python.exe scripts\producer_journal.py --db "panopticon.db" --journal "cache\derived_recovery\<operation-id>\journal.json" --reconcile --expected-review-fingerprint "<review_fingerprint>"
```

This opens an existing ready Native database without migration or schema repair.
A matching receipt is acknowledged without replay, including when the current
file has changed or disappeared. An export review creates only an evidence record.
With no receipt, one derived registration or original metadata commit requires
unchanged source identity/curation, current result bytes and verified generation;
original edits also require the unchanged backup and previous generation. Known
Native destination identities, changed observations/aliases, unsupported metadata
or a stale review remain pending. It does not reinterpret conflicting real values.

The action writes a separate `reconciliation-<UUID>.json` intent before admission,
outside watched/Cherry folders. It retains the original producer journal, image
and backup. Admitted Native work drains through cancellation; a lost reply is
checked against its receipt once without resubmission. Database/evidence failures
remain reviewable, and a historical acknowledgement never restores missing bytes.
Existing guards require manual review and are not automatically cleared. External
file edits can still race with checks; post-commit review reports current state.
For reconciliation, exit 0 means the action was recorded; inspect `current_status`
separately.

In the desktop dashboard, open **Recovery journals**, select one `journal.json`,
then **Review current evidence**. The window shows current source/output/backup
hashes, Native IDs, curation summary, revision checks and the historical receipt.
Its explicit action distinguishes historical acknowledgement, export review,
pending derived-copy registration and pending original metadata registration.
It reuses the application's open Native writer; do not close the app for this GUI.
Changed or conflicting evidence requires a fresh review. A successful action
consumes that review and records current state separately from historical confirmation.

Hashing, image verification and Native admission run in one retained worker.
Closing cancels before admission and waits without blocking the GUI for accepted
work and final native thread cleanup. The dashboard closes its Native owner only
after this worker exits. Producer images, backups and journals remain untouched.
Original Metadata Hub backups are retained until an explicit, per-operation
purge is reviewed and confirmed. The backend verifies the current online copy's
bytes and generation, its unchanged identity/curation revision, the selected
backup's bytes, and its complete historical generation record and matching
receipt. Earlier-operation backups can be reviewed after a later verified edit;
this preserves all intervening history and the current file. Unknown or pending
history, changed evidence, indexed/documented backups, hard links and portable or
watched recovery folders require review and cannot be purged.

On Windows, open handles pin the current file and exclusively pin the selected
backup through approval and deletion by handle. The Native owner approves current
revision/history/scope without deleting or updating any Native rows. Separate,
immutable prepared/approved/complete evidence records never overwrite earlier
records or the producer journal. A lost approval reply prevents deletion; queued
approval can be cancelled and admitted approval drains. Cancellation before the
delete mark preserves the backup; once marked, completion records the actual
deletion even if the final evidence write fails. No cleanup is automatic and
there is no joint filesystem/SQLite transaction.

In **Recovery journals**, review one original Metadata Hub journal, then choose
**Review original backup purge**. This worker preview verifies the backup and
current copy without deleting anything. **Permanently delete reviewed backup**
opens a per-operation confirmation showing the backup path, size and SHA-256,
operation ID and current copy kept. The default is No. Confirmation consumes the
preview and the worker rechecks it before effects; selection changes, new reviews,
changed evidence and failed previews invalidate eligibility. Exports, derived
outputs, pending metadata commits and absent backups cannot offer this purge.

One worker handles review, reconciliation, purge preview or confirmed purge.
Nested confirmation cannot admit another action or a late action after closing.
Closing before the delete mark preserves the backup; after deletion, accepted
evidence work finishes before native thread cleanup. The GUI reports confirmed
deletion separately from a failed final evidence write. History, curation,
receipts, current images and producer journals remain preserved. Live concurrent
Windows validation and manual review of old guards/partial evidence remain open.

### Metadata in transformed outputs

Cropper and Image Optimizer stamp and verify a temporary output
before publication. PNG, JPEG and WebP metadata updates preserve the encoded image
data; AVIF receives its metadata in the initial encoding, avoiding a second pass.
Unicode prompts, generation settings, raw workflows, tags and ratings round-trip
through the shared payload. Ambiguous or oversized metadata requires review and
does not count as a successful preserved output.

Image Optimizer and Cropper use stable cache names derived from each source path to distinguish
equal basenames. It requires a fresh destination outside Cherry folders and rejects
active or offline native path history when using the application's data service.
For indexed sources, their application workers capture the source ID and full revision,
prepares the additive receipt schema once, and verifies its own output bytes and
generation data. A recovery journal is flushed before non-overwriting publication;
approval and final registration recheck the source revision and native destination.
The new copy has its own ID and SHA-256, generation data, FTS entry and historical
receipt. It starts without central tags/rating, pHash or Cherry origin/receipts;
preserved embedded metadata remains separate from central curation.

If publication succeeds but registration, receipt confirmation or journal completion
fails, the result reports an output needing review and its journal path. It keeps
the file and does not automatically retry or delete it. After publication, cancellation
waits for native confirmation in the worker. The journal lives in `derived_recovery`
under the configured cache, outside watched/Cherry folders. Keep it for manual review;
a ready-to-publish journal after interruption does not establish whether a file was
published. Receipt acknowledgement establishes a historical commit, not current
availability. Disk publication and SQLite registration are separate operations.

External sources without a native ID retain export without automatic registration
and the bounded read-only destination check. The indexed flow uses owner approvals
without a redundant per-image RO scan. The owner reuses bounded destination keys
between registrations and refreshes after external commits or other writes, with
complete fallback above cache limits. No read transaction remains open between images.
Cropper protects existing destinations even for explicitly chosen save paths. Its
worker registers indexed recrops through this contract; external sources remain
unregistered.

Watermarker publishes a new public copy through a private temporary file,
one encoding pass, full decode/privacy verification and a no-replace rename
on Windows. Existing outputs, Native destination history and Cherry folders
are protected. Missing/changed assets abort publication; source and assets
stay untouched. Embedded metadata is deliberately removed; these copies
do not inherit Native curation or register automatically as dataset derivatives.
Inputs are limited to 256 MiB, one frame and 20 million pixels; pattern work
is bounded. Preview decoding/rendering and saving share one worker, with one
latest pending preview and one explicit save. GUI updates use detached QImage
results; save takes priority after cancelling the active preview. The save
dialog revalidates its image/settings, existing destinations are rejected,
and a successful save opens its output folder. Closing cancels before
publication and retains the worker through final native cleanup. A copy
already published stays on disk.

Format Converter is retired from module discovery and cannot be opened through
the application loader. Existing converted files remain untouched. Its legacy
implementation stays in Git for compatibility and historical tests; shared
metadata and image codecs used by active tools remain available. This retirement
does not imply that every training pipeline supports every image format.

The shared stamping API requires `prepared=True`, meaning a private temporary
output owned by its producer. It does not authorize editing an indexed original.
Use Metadata Hub's reviewed operation for such edits. Batch conversion verification
is read-only and no longer repairs published files automatically. AVIF producers
use `encoder_options` before saving and `verify_prepared` afterward.
Prepared stamping accepts single-frame images up to 256 MiB and 20 million pixels,
with at most 256 KiB of shared metadata; JPEG EXIF also has its segment size limit.

Cherry advances a source's incremental discovery frontier only after a complete
remote scan and durable catalog flush, using the scan's UTC start time. Artist
registration and transfer counters cannot advance it. Interrupted scans keep
the previous frontier; downloads interrupted after completed discovery keep
their durable pending queue. Catalog and index remain separate portable DBs.
A legacy timestamp without full-history coverage triggers a complete scan.
Previously incorrect frontiers with coverage already marked require review
and an explicit Rescan; this update does not inspect or reset real profiles.

Native write admission copies only bounded plain data: an estimated 4 MiB per
request, 1 MiB per string/bytes/path field, 65,536 visited values, 10,000 items
per container and nesting depth 16. Requests exceeding these bounds fail as
whole requests; no prefix is written. Producers must submit deliberate batches.
Closed/full services reject before copying. Custom object hooks and cyclic
payloads are rejected. Metrics count rejected payloads without storing content.

New curation stays within 256 tags per copy and 256 characters per normalized
tag, matching portable restoration. The writer checks the resulting total
inside the same transaction, including replacement deltas and queued writers.
Old oversized curation remains stored and can be reduced or rated; no tags are
automatically removed. Unicode lowercase expansion is checked before storage.
Native reads also snapshot bounded plain input before opening SQL. All reads
have a 10-second default budget; existing discovery APIs retain their timeout
reason. SQLite progress checks interrupt long queries and release their reader.
Cancellation is checked before admission and after result work, so cancelled
or late results are not delivered. This is cooperative SQL cancellation: it
does not forcibly terminate arbitrary Python work. Count and page still share
one read-only snapshot. Metrics separately count rejected read payloads.

The Qt reader validates input without arbitrary deepcopy hooks. Invalid requests
use queued error delivery without opening SQL or starting another thread, while
the existing one-active/latest-pending behavior remains. Old tickets and closed
owners discard late results.

Legacy path pages, previews and hash-backfill batches require exact integer limits from
1 to 10,000 and nonnegative SQLite integer offsets/cursors. Negative LIMIT can no
longer turn a paginated request into an unrestricted query. These responses and
Librarian selections accumulate rows incrementally within an 8 MiB estimated
Python-object budget, with a 1 MiB field cap; oversized results fail explicitly
instead of returning a partial list. Count and page share the same read snapshot,
and complete selections still reject transfers that require streaming. Metrics
count oversized responses without retaining their content. Cancellation and
deadlines retain priority over a response-size error.

This bounds the accumulated response, not total process RAM or a temporary cell
already decoded by SQLite. Other full legacy APIs still need consumer migration
and bounded responses.

Gallery sidebar/viewer and Librarian now read tag suggestions in pages of at most
200. Matching uses Unicode casefold and literal containment, including `%` and
`_`. Librarian provides previous/next navigation through matching vocabulary;
statistics read counts/folders without materializing every tag. Suggestions are
optional hints: users can still type any tag manually. Typing invalidates previous
results immediately and debounces new requests by 150 ms. Readers keep one active
request and the latest pending request, discard stale results and close with their
owner. Pages use binary-name cursors and independent snapshots; they do not freeze
the vocabulary across edits. Legacy full-vocabulary APIs retain compatibility.

Librarian missing-file reconciliation reads native inventory by copy ID in pages
of at most 256. It captures an upper ID at the start, excluding later IDs created
by the central writer from that sweep. Each page has its own snapshot; this is
not a frozen snapshot across pages. A private reader stays in the worker's thread
and ends its transaction after every page, before filesystem work. The service
retains input/result limits, per-page deadlines, cancellation and read metrics.
Readers reject use from another thread or after their scope closes.

Missing candidates retain their initial copy ID and accessible parent identity in
an automatically closed temporary file, capped at 256 MiB. All pages, temporary
record counts/bytes/SHA and parent identities are checked before the first archive.
The parent cache and review blocks are limited to 256. A failed/cancelled read or
temporary-file limit cannot authorize a partial inventory. The current copy must
still have its initial ID; a replacement at the same path requires another scan.
Fresh state fingerprints, absence and directory identity are checked again before
each existing writer transaction. Confirmed earlier batches remain if a later
archive fails or is cancelled. This moves records offline without deleting files,
curation, history or Cherry receipts. The temporary is not a recovery journal.
Disk discovery now uses a private record stream and SQLite path index, each
capped at 128 MiB, with a 2 MiB SQLite page cache and <=64 KiB per record. Seal
checks the complete stream/index before registration; each <=100-row delivery
checks its bytes again, and another full check precedes absence reconciliation.
Presence is queried by native pages of <=256 paths instead of a full Python set.
Lazy scandir traversal avoids per-directory filename lists and caps open directory
handles at 64; an exceeded depth or inaccessible directory makes the pass
incomplete. Worker errors retain at most 32 messages of 4096 characters.
Storage failure, corruption, cancellation or limits cannot authorize archival
using a prefix. The private spool is closed/removed before worker completion and
is not a journal. It trades extra temporary disk work for lower Python memory.
Cherry catalogs reuse one RO connection with <=128 rows per short snapshot and
an initial rowid frontier; each snapshot ends before file checks or spool writes.
Oversized/invalid source fields are flagged for review, never delivered as
accepted truncated metadata. Existing Cherry catalogs, schema and receipts are
not modified by Panopticon SQL. The pass has no common snapshot across pages;
later records need a subsequent scan. Filesystem/SQLite checks are not atomic.
Legacy list-returning inventory APIs remain available for other consumers.

The native owner exposes an ephemeral revision token: service epoch, sequence
and monitor health. Confirmed commands that changed SQLite's total_changes advance
it after commit; rollback, cancellation and empty writes do not. This counts
committed SQLite changes, not necessarily semantic differences in matched UPDATEs.
The same persistent owner connection polls PRAGMA data_version while idle and
before commands to detect external commits. Multiple external commits can coalesce.
Monitor failure/recovery/closure changes its health token. Reading revision_snapshot
copies local primitives under a short lock without SQL/stat or writer waits;
logging happens outside that lock. Epochs change when a service is recreated.

Gallery's module/viewers and Librarian poll that local token every 250 ms and
coalesce refreshes until their existing readers are idle. Notifications preserve
typing, vocabulary cursors and current pages, and avoid cancelling a slow read on
every commit. Librarian retains externally supplied selections and protects an
active handoff/export; reduced native results clamp to the last available page.
The initial token is captured before first reads, so commits during view creation
are not missed. Owners stop their timers and discard pending notices on closure.
Viewer curation refreshes without rereading image files. Physical publication
refresh and Gallery thumbnail source validation are described below; other
consumers still need integration.

This is eventual invalidation, not a durable log, receipt, approval fingerprint or
frozen DB revision attached to a read result. A busy writer/read/export can delay
notification. External commits still require valid schema/FTS; monitoring does not
repair their integrity. FIFO scheduling remains unchanged. Other consumer caches,
physical cache invalidation and live WAL pressure remain open.

`scripts/library_scheduling_selftest.py` verifies ordering on temporary Native
databases. A reviewed restore followed by an interactive tag edit succeeds in
FIFO order; executing the edit first correctly rejects the restore because its
reviewed state changed. Two ratings on one copy also depend on order. Ratings on
distinct copies commute in that fixture; this does not establish independence
for global tags, registration, lifecycle or maintenance operations. These are
policy evidence tests, not a priority scheduler. The writer still uses FIFO.

Native keeps global FIFO for accepted writes. Librarian observation/hash producers
submit at most 100 records per write and wait for confirmation before submitting
the next. This is a conservative row bound, not a measured universal optimum or
a cap on SQL duration. Hash reads can still contain more than one write batch.
With `cancel_event`, the worker facade waits under queue pressure and retries only
`LibraryQueueFull`, which specifically means no command was admitted. Command,
payload and shutdown failures are not retried. Stop cancels queued work where
possible; already running writes settle and remain included in confirmed counts.
The legacy facade without that option remains atomic and does not silently split
requests. Whole discovery memory, active transactions and long maintenance remain
separate work; FIFO does not guarantee an immediate rating under sustained load.
`scripts/library_batch_selftest.py` exercises private admission and cancellation.

Native `metrics_snapshot()` now includes sampled physical `wal_bytes`,
`wal_status` (`unsampled`, `present`, `absent`, `error`), sample age, observer
state and stat duration/error counters. The existing owner samples outside SQL
transactions, at most once per second. Sampling failures are nonfatal and clear
the current size; snapshots do not perform SQL/stat or wait for the owner. A busy
owner delays samples, so check age and observer state before interpreting them.
An absent WAL is not proof that a disk/database is healthy. Physical size can
include reused space and does not measure pending frames or checkpoint progress;
the sampler never requests a checkpoint or changes durability/pragmas.
[SQLite explains WAL reuse and checkpointing](https://www.sqlite.org/wal.html).

`queue_busy_seconds` and `reads_busy_seconds` measure continuous nonempty queue
and read-call occupancy; overlapping work keeps that interval open. They are not
the age of the oldest current item/reader or measurements of SQLite locks.
`write_active_seconds` is elapsed time for the current owner command. Metrics
retain only fixed scalar state, with no paths, payloads or per-request history.
A slow filesystem stat can delay the owner while snapshots remain available;
these observations are diagnostics, not deadlines or automatic pressure control.
`scripts/library_wal_metrics_selftest.py` verifies private WAL/occupancy behavior.

Gallery grids now own independent thumbnail loaders and pools; unloading one
does not stop another. Up to four workers run per grid, with at most 256 pending
requests. Navigation drops pending old-page work, cancels active old requests and
discards their late results. Shutdown clears only its own pool and polls actual
thread completion without a GUI wait. The compatibility `get_loader()` factory
remains available, but active Gallery grids no longer share that singleton.

Each cache is an LRU bounded to 256 images and 16 MiB of retained decoded pixels.
Keys include path and requested dimensions (maximum 512 per edge). Even cache
hits asynchronously check file device/identity, size and nanosecond mtime in the
worker; decoding verifies that the source identity did not change. QImageReader
uses decoder scaling when supported and explicitly closes its device. Source
limits match preview (256 MiB/100M pixels); plugins may still allocate a full
source image, so cache/queue limits are not a total process-memory guarantee.
Cache values use QImage copy-on-write semantics; QPixmap construction stays in UI.
Failures show a localized unavailable label instead of an endless placeholder.

Native also exposes a coarse `preview_revision_snapshot()`: potential physical
publication commands (`commit_metadata_edit`, legacy `update_file_metadata`,
`register_derived_copy`, `restore_duplicate_copy`) advance it after a changed
commit; unknown external commits invalidate it conservatively. Ordinary local
curation, observations and hash backfill retain image caches. Gallery clears its
cache on that hint, while worker stat validation detects other changed sources.
Old cache epochs cannot repopulate cleared entries. This is not a content hash,
durable publication proof or defense against external edits preserving all stat
fields. An open viewer also follows this physical hint and asynchronously
reloads its current preview, file metadata and fullscreen after busy readers or
accepted curation edits settle. It retains Native curation, unfinished tag input
and navigation; multiple notices during a slow preview coalesce to the latest.
Local tags/ratings do not decode files again. Unknown external commits can reload
unaffected images conservatively; file edits without a Native notice are checked
on later navigation, not by a filesystem watcher. Other consumers remain open.
`scripts/thumbnail_version_selftest.py` verifies private image/cache/window cases.

`scripts/path_identity_policy_selftest.py` reproduces current path-policy gaps on
private Windows files: ASCII case variants can refer to one file but register
two Native IDs with independent curation; Unicode folder aliases can disagree
with legacy SQLite LIKE counts. Synthetic case-distinct paths also demonstrate
that ASCII LIKE combines scopes regardless of filesystem rules. Tests preserve
both histories when one spelling is archived. The filesystem-aware
policy preserves spelling and separate copy histories. Worker-only
`LibraryService.inspect_path_identity_page()` prepares at most100 observations
per page, with a fixed initial ID boundary. Its SQL snapshot closes before
Windows name probes. Offline/unavailable/reparse paths have no guessed key;
hard links retain distinct opened names. Revision changes mark the preview stale.
This is preparation, not authority to merge/delete. Explicit Native
`prepare_path_identity` takes a consistent SQLite backup before additive DDL;
`publish_path_identity_page()` rechecks disk evidence before ≤100-row FIFO SQL
CAS. Existing paths, IDs, curation, history and FTS are unchanged. The nonunique
canonical index keeps conflicting IDs for review; no record wins automatically.
Status/match APIs are bounded diagnostics.
Explicit worker `backfill_path_identity()` processes all rows within its fixed
initial ID boundary in ≤100-row batches, counts confirmed commits, and cancels
between probes/admissions. It preserves previous confirmed evidence on failure;
a new explicit pass safely rechecks it. `resolve_path_identity()` asks Windows
before its SQL snapshot. It selects a unique ID only when coverage is complete
and no online rows are unresolved; collisions/incomplete/offline remain review
results. A bounded one-entry coverage cache follows a transactional Native SQL
clock: file inserts/deletes, changed path/offline bindings and registry writes
advance it even through other SQLite clients; rollback does not. Reader
population requires coherent query-only snapshots without dirty reads, because
rolled-back epoch numbers can later be reused. Known ingestion transactions can
also carry exact coverage counts forward, published only after successful commit.
That optional hint uses the token captured inside the transaction: it never reads
SQL after commit to acknowledge a possible intervening foreign write. Exact
Native triggers, expected epoch increments and unchanged canonical memberships
are required; foreign/temporary triggers, unexpected bindings or a changed epoch
force an authoritative recount. A cache failure cannot report a committed batch
as failed. Ordinary
curation retains coverage while match rows read current ratings. Producer
destination guards recognize only the exact verified Native clock triggers,
retaining their warm path cache; foreign/altered/temporary triggers still force
a complete refresh before another approval. Offline and collision safeguards
remain unchanged. Readers validate
owned clock/table/index/trigger definitions before cache use; incomplete old
preparations work uncached until another explicit backed-up prepare. These APIs
route the indexer's bounded worker registration after explicit preparation:
resolved aliases reuse the existing ID and stored spelling; new files and their
identity evidence commit together with FTS. Saved non-NULL provenance, immutable
original hashes, tags, ratings and generation remain preserved. Ambiguity,
incomplete coverage, unavailable online evidence or a changed SQL clock rejects
the whole batch; incomplete indexing cannot archive omissions. Offline exact
histories remain offline. Stop waits for running commits; admission retains FIFO.
Unprepared worker batches retain legacy behavior, with a writer-side guard
against preparation racing admission. Other legacy mutations and callers without
the worker cancellation contract remain open. No aggregate filesystem/SQLite
snapshot or hard I/O deadline is claimed; physical evidence expires before SQL.
Librarian exposes explicit path preparation and paginated review; it can check
one image spelling and open retained-copy histories without merging IDs. Stop
keeps confirmed evidence; closing waits for actual reader completion.
Existing lookups, other registrations and folder prefixes still require
migration. Known changing ingest batches retain exact coverage without repeated
full scans; other mutations still invalidate it normally. Validating the real
migration remains open. Folder spelling and portable Cherry data remain preserved.
[Windows supports case rules per directory](https://learn.microsoft.com/en-us/windows/wsl/case-sensitivity).

## License

Business Source License 1.1 — free for non-commercial use.  
Commercial use requires a separate license. See [LICENSE](LICENSE) for details.  
Converts to MIT on 2030-04-18.
