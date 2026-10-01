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
is bounded. The GUI preview/save worker migration is a separate next step.

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

## License

Business Source License 1.1 — free for non-commercial use.  
Commercial use requires a separate license. See [LICENSE](LICENSE) for details.  
Converts to MIT on 2030-04-18.
