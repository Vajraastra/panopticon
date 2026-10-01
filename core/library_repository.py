import sqlite3
import os
import logging
import re

from core.library_fts import check_fts, repair_fts
from core.library_selection import validate_selection, term_filter, select
from core.library_versions_schema import initialize_new


log = logging.getLogger(__name__)

class LibraryRepository:
    """SQL privado. La conexión se crea, usa y cierra en el hilo dueño."""

    def __init__(self, conn):
        self.conn = conn

    def create_schema(self):
        cursor = self.conn.cursor()
        new_files = not cursor.execute("SELECT 1 FROM sqlite_master WHERE name='files'").fetchone()

        # Files Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                filename TEXT,
                file_size INTEGER DEFAULT 0,
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                created_date DATETIME,

                -- Metadata (Optional)
                meta_tool TEXT,
                meta_model TEXT,
                meta_positive TEXT,
                meta_negative TEXT,
                meta_seed TEXT,

                rating INTEGER DEFAULT 0,

                -- Identidad de contenido + origen recabado (CHERRY_FUSION_DESIGN
                -- CF3/CF8). Todos nullable: bloque espejo del de generación.
                hash_original TEXT,  -- SHA-256 INMUTABLE (tal como se catalogó por 1ª vez)
                hash_mod TEXT,       -- SHA-256 actual tras estampado; NULL si nunca estampado
                phash TEXT,          -- hash perceptual (near-dups), poblado por backfill opcional
                origin_url TEXT,     -- url_source de cherry-dl
                origin_site TEXT,    -- kemono/patreon/pixiv/...
                artist TEXT,         -- display_name del perfil cherry
                is_offline INTEGER NOT NULL DEFAULT 0 CHECK(is_offline IN (0,1))
            )
        """)
        if new_files:
            initialize_new(self.conn)

        # Tags Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                category TEXT DEFAULT 'general'
            )
        """)

        # File-Tags Relationship
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS file_tags (
                file_id INTEGER,
                tag_id INTEGER,
                source TEXT DEFAULT 'manual',
                FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE,
                FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE,
                UNIQUE(file_id, tag_id)
            )
        """)

        # Watched Folders
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watched_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Índice de búsqueda FTS5 (full-text). rowid = files.id; columnas =
        # exactamente los campos buscables (filename + metadata + tags). El
        # tokenizer 'trigram' permite búsqueda por SUBCADENA indexada (como el
        # viejo LIKE %term%) para términos de ≥3 caracteres, sin full-scan.
        # Sincronizado vía _fts_sync_file() en cada escritura de files/tags.
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
                filename, meta_positive, meta_negative, meta_model, meta_tool, tags,
                tokenize='trigram'
            )
        """)


    def run_transformations(self):
        """Ensures schema is up to date."""
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(files)")
        cols = [row[1] for row in cursor.fetchall()]
        if 'rating' not in cols:
            cursor.execute("ALTER TABLE files ADD COLUMN rating INTEGER DEFAULT 0")

        # Migración B1 (CHERRY_FUSION_DESIGN): identidad de contenido + origen
        # recabado. ALTERs aditivos nullable — seguros e idempotentes.
        for col, decl in (
            ('hash_original', 'TEXT'),
            ('hash_mod', 'TEXT'),
            ('phash', 'TEXT'),
            ('origin_url', 'TEXT'),
            ('origin_site', 'TEXT'),
            ('artist', 'TEXT'),
        ):
            if col not in cols:
                cursor.execute(f"ALTER TABLE files ADD COLUMN {col} {decl}")
        # Índice de dedup/anti-redescarga. Va aquí (no en create_schema) para
        # que en DBs viejas la columna exista antes que el índice. hash_mod no
        # lleva índice: solo se consulta junto a hash_original vía COALESCE.
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_hash_original ON files(hash_original)"
        )

        # Sólo durante la migración inicial; instalaciones ya marcadas se
        # revisan/reparan explícitamente, sin full-scan en cada apertura.
        self.repair_fts()

    # --- Sincronización del índice FTS5 ---

    def _fts_sync_file(self, file_id, cursor=None):
        """Recalcula la fila FTS de un archivo desde files + sus tags (delete +
        insert, idempotente). NO hace commit: el llamador controla la
        transacción. file_id debe existir en `files`."""
        cur = cursor or self.conn.cursor()
        row = cur.execute(
            "SELECT filename, meta_positive, meta_negative, meta_model, meta_tool "
            "FROM files WHERE id=?", (file_id,)
        ).fetchone()
        if not row:
            return
        tags = " ".join(
            r[0] for r in cur.execute(
                "SELECT t.name FROM tags t JOIN file_tags ft ON t.id=ft.tag_id "
                "WHERE ft.file_id=? ORDER BY ft.tag_id", (file_id,)
            ).fetchall()
        )
        cur.execute("DELETE FROM files_fts WHERE rowid=?", (file_id,))
        cur.execute(
            "INSERT INTO files_fts(rowid, filename, meta_positive, meta_negative, "
            "meta_model, meta_tool, tags) VALUES (?,?,?,?,?,?,?)",
            (file_id, row[0] or '', row[1] or '', row[2] or '', row[3] or '', row[4] or '', tags)
        )

    def check_fts(self):
        """Diagnóstico RO exacto de filas ausentes/desactualizadas/huérfanas."""
        return check_fts(self.conn)

    def repair_fts(self):
        """Reparación selectiva, atómica dentro de la transacción del dueño."""
        return repair_fts(self.conn)

    def rebuild_fts(self):
        """Regeneración total explícita, incluida la estructura interna FTS.

        Conserva memoria acotada; los fallos llegan al dueño para rollback.
        """
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM files_fts")
        ids = self.conn.execute("SELECT id FROM files ORDER BY id")
        while block := ids.fetchmany(256):
            for (file_id,) in block:
                self._fts_sync_file(file_id, cursor)
        return True

    def _normalize_path(self, path):
        return os.path.normpath(path).replace('\\', '/')

    def _folder_like(self, folder_path):
        """Patrón LIKE recursivo con separador: '/foo/bar' → '/foo/bar/%'.
        Sin el '/' final, '/foo/bar' matchearía también '/foo/barbecue/...'.
        Escapa %/_ del nombre de carpeta; usar junto a ESCAPE '\\' en el SQL."""
        norm = self._normalize_path(folder_path)
        escaped = norm.replace('%', r'\%').replace('_', r'\_')
        return escaped.rstrip('/') + '/%'

    # --- Phase 1: Robust Registration ---

    def register_files_minimal(self, files_data):
        """
        Phase 1: Fast Bulk Insert of basic file info.
        files_data: list of dicts {'path', 'filename', 'size', 'created'}
        Claves opcionales (ingesta cherry, M2 de CHERRY_FUSION_DESIGN):
        'hash_original', 'origin_url', 'origin_site', 'artist'.
        """
        if not files_data: return True

        cursor = self.conn.cursor()
        # El servicio mantiene BEGIN IMMEDIATE durante comparación + escritura.
        # Leer bloques acotados evita una consulta por archivo y el límite de
        # variables SQLite. No intentar INSERT para filas idénticas: incluso un
        # UPSERT sin UPDATE puede avanzar sqlite_sequence (AUTOINCREMENT).
        observations = [(self._normalize_path(f['path']), f) for f in files_data]
        paths = list(dict.fromkeys(path for path, _ in observations))
        previous = {}
        historical = set()
        for start in range(0, len(paths), 256):
            block = paths[start:start + 256]
            placeholders = ','.join('?' * len(block))
            rows = cursor.execute(
                'SELECT path, filename, file_size, created_date, hash_original, '
                'origin_url, origin_site, artist,is_offline FROM files '
                f'WHERE path IN ({placeholders})', block,
            ).fetchall()
            previous.update((row[0], row[1:-1]) for row in rows if not row[-1])
            historical.update(row[0] for row in rows if row[-1])

        # Simular observaciones repetidas en orden: primer hash no-NULL y último
        # origen no-NULL; nunca perder un valor porque la siguiente lo omite.
        current = dict(previous)
        inserts = []
        updates = []
        for path, f in observations:
            # Inventario no verifica bytes ni identifica la versión que está viendo.
            if path in historical:
                continue
            old = current.get(path)
            values = (f['filename'], f['size'], f['created'],
                      f.get('hash_original'), f.get('origin_url'),
                      f.get('origin_site'), f.get('artist'))
            if old is not None:
                values = (*values[:3], old[3] if old[3] is not None else values[3],
                          *(new if new is not None else saved
                            for new, saved in zip(values[4:], old[4:])))
            if old is None:
                inserts.append((path, *values))
            elif old != values:
                updates.append((*values, path))
            current[path] = values

        if not inserts and not updates:
            return True

        cursor.executemany("""
            INSERT INTO files (path, filename, file_size, created_date,
                               hash_original, origin_url, origin_site, artist)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, inserts)
        # UPDATE evita consumir IDs por conflictos; la identidad y los campos
        # opcionales ya están resueltos bajo el mismo bloqueo de escritura.
        cursor.executemany("""
            UPDATE files SET filename=?, file_size=?, created_date=?,
                             hash_original=?, origin_url=?, origin_site=?, artist=?
            WHERE is_offline=0 AND path=?
        """, updates)
        # Tamaño/fecha/hash/origen no son buscables. Sólo filas nuevas o cambios
        # de nombre requieren FTS; metas/tags se conservan y se leen al sincronizar.
        # Reparar un FTS parcialmente corrupto es una operación explícita aparte.
        fts_paths = [path for path in paths if path not in historical
                     if path not in previous or previous[path][0] != current[path][0]]
        for start in range(0, len(fts_paths), 256):
            block = fts_paths[start:start + 256]
            placeholders = ','.join('?' * len(block))
            ids = cursor.execute(
                f'SELECT id FROM files WHERE is_offline=0 AND path IN ({placeholders}) ORDER BY id', block,
            ).fetchall()
            for (fid,) in ids:
                self._fts_sync_file(fid, cursor)
        # Errores SQL/FTS llegan al servicio con su causa; allí todo el lote
        # hace rollback y sólo se confirma True después de commit.
        return True

    def update_file_metadata(self, path, meta):
        """
        Phase 2: Update existing record with rich metadata.
        """
        cursor = self.conn.cursor()
        norm_path = self._normalize_path(path)
        self._require_unversioned_path(norm_path)

        sql = """
            UPDATE files SET
                width=?, height=?, meta_tool=?, meta_model=?,
                meta_positive=?, meta_negative=?, meta_seed=?
            WHERE is_offline=0 AND path=?
        """
        try:
            cursor.execute(sql, (
                meta.get('width', 0),
                meta.get('height', 0),
                meta.get('tool', 'Unknown'),
                meta.get('model', 'Unknown'),
                meta.get('positive', ''),
                meta.get('negative', ''),
                str(meta.get('seed', '')),
                norm_path
            ))
            row = cursor.execute("SELECT id FROM files WHERE is_offline=0 AND path=?", (norm_path,)).fetchone()
            if row:
                self._fts_sync_file(row[0], cursor)
            return bool(row)
        except Exception as e:
            log.warning("[DB] Meta Update Error for %s: %s", path, e)
            return False

    # --- Discovery / Existence ---

    def get_known_files_in_folder(self, folder_path):
        """Returns set of normalized paths for files known in a specific folder (recursive)."""
        cursor = self.conn.cursor()
        msg_path = self._folder_like(folder_path)
        cursor.execute("SELECT path FROM files WHERE is_offline=0 AND path LIKE ? ESCAPE '\\'", (msg_path,))
        return set(row[0] for row in cursor.fetchall())

    def get_files_recursive(self, folder_path, limit=None):
        """Returns a list of all files under a folder (recursive)."""
        cursor = self.conn.cursor()
        msg_path = self._folder_like(folder_path)
        if limit:
            cursor.execute("SELECT path FROM files WHERE is_offline=0 AND path LIKE ? ESCAPE '\\' LIMIT ?", (msg_path, limit))
        else:
            cursor.execute("SELECT path FROM files WHERE is_offline=0 AND path LIKE ? ESCAPE '\\'", (msg_path,))
        return [row[0] for row in cursor.fetchall()]

    def remove_files(self, paths_list):
        """Retira del inventario operativo, conservando identidad/curación offline."""
        if not paths_list: return True
        cursor = self.conn.cursor()
        try:
            for p in paths_list:
                norm = self._normalize_path(p)
                cursor.execute("UPDATE files SET is_offline=1 WHERE is_offline=0 AND path = ?", (norm,))
            return True
        except Exception as e:
            log.warning("[DB] remove_files error: %s", e)
            return False

    def archive_missing_copies(self, approved, *, folder):
        from core.inventory_archive import archive_missing
        return archive_missing(self.conn, approved, folder=folder, normalize=self._normalize_path)

    # --- Standard Queries (Gallery/Search) ---

    def get_watched_folders(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT path FROM watched_folders")
        return [row[0] for row in cursor.fetchall()]

    def add_watched_folder(self, path):
        cursor = self.conn.cursor()
        try:
            cursor.execute("INSERT OR IGNORE INTO watched_folders (path) VALUES (?)", (self._normalize_path(path),))
            return True
        except Exception as e:
            log.warning("[DB] add_watched_folder error: %s", e)
            return False

    def remove_watched_folder(self, path):
        cursor = self.conn.cursor()
        norm = self._normalize_path(path)
        try:
            like = self._folder_like(path)
            cursor.execute("DELETE FROM watched_folders WHERE path=?", (norm,))
            cursor.execute("UPDATE files SET is_offline=1 WHERE is_offline=0 AND path LIKE ? ESCAPE '\\'", (like,))
            return True
        except Exception as e:
            log.warning("[DB] remove_watched_folder error: %s", e)
            return False

    def get_folder_archive_preview(self, folder):
        from core.folder_archive import preview
        return preview(self.conn,folder,normalize=self._normalize_path,folder_like=self._folder_like)

    def archive_watched_folder_reviewed(self, folder, *, expected_fingerprint):
        from core.folder_archive import archive
        return archive(self.conn,folder,expected_fingerprint=expected_fingerprint,normalize=self._normalize_path,
            folder_like=self._folder_like,remove=self.remove_watched_folder)

    def get_folder_count(self, folder_path):
        cursor = self.conn.cursor()
        query = self._folder_like(folder_path)
        cursor.execute("SELECT count(*) FROM files WHERE is_offline=0 AND path LIKE ? ESCAPE '\\'", (query,))
        return cursor.fetchone()[0]

    def get_folder_preview(self, folder_path, limit=5):
        cursor = self.conn.cursor()
        query = self._folder_like(folder_path)
        cursor.execute("SELECT path FROM files WHERE is_offline=0 AND path LIKE ? ESCAPE '\\' LIMIT ?", (query, limit))
        return [row[0] for row in cursor.fetchall()]

    def search_by_terms(self, terms, limit=5):
        """
        Searches for files containing ALL terms in their metadata or tags
        (substring match, igual que antes). Usa el índice FTS5 (trigram) para
        los términos de ≥3 chars — escalable a ~250K imágenes — y cae a LIKE
        solo para los términos más cortos, que trigram no indexa.
        Returns a tuple: (total_count, list_of_preview_paths)
        """
        if not terms:
            return 0, []

        cursor = self.conn.cursor()
        condition, params = term_filter(terms)

        if not condition:  # todos los términos quedaron vacíos tras strip
            return 0, []

        where = "WHERE is_offline=0 AND (" + condition + ")"

        total_count = cursor.execute(f"SELECT count(*) FROM files {where}", params).fetchone()[0]
        preview_paths = [
            row[0] for row in cursor.execute(
                f"SELECT path FROM files {where} LIMIT ?", params + [limit]
            ).fetchall()
        ]
        return total_count, preview_paths

    def get_file_id(self, path):
        """Helper: Gets the DB ID for a file path."""
        cursor = self.conn.cursor()
        norm_path = self._normalize_path(path)
        cursor.execute("SELECT id FROM files WHERE is_offline=0 AND path = ?", (norm_path,))
        row = cursor.fetchone()
        return row[0] if row else None

    def get_curation_snapshot(self, paths, *, include_revision=False):
        """Hasta 256 rutas, dos SELECT en el snapshot del lector; sin registrar archivos.

        Conserva orden/duplicados. id=None distingue ausencia de curación vacía;
        cero estrellas y tags vacíos de una fila registrada son autoritativos.
        include_revision agrega huella del estado completo: tres SELECT acotados,
        sin materializar un estado por ruta repetida ni crear schema.
        """
        if type(include_revision) is not bool:
            raise ValueError('invalid_curation_snapshot_revision')
        if not isinstance(paths, (list, tuple)) or len(paths) > 256:
            raise ValueError('Curation snapshot requires at most 256 paths')
        if any(not isinstance(path, str) or not path for path in paths):
            raise ValueError('Curation paths must be nonempty strings')
        normalized = [self._normalize_path(path) for path in paths]
        unique = tuple(dict.fromkeys(normalized))
        if not unique:
            return []
        if include_revision:
            from core.curation_bindings import compact
            return compact(self.get_duplicate_snapshot(unique), normalized)
        placeholders = ','.join('?' for _ in unique)
        records = {}
        for file_id, path, rating in self.conn.execute(
                f'SELECT id, path, rating FROM files WHERE is_offline=0 AND path IN ({placeholders})', unique):
            records[path] = dict(id=file_id, path=path, rating=rating or 0, tags=[])
        by_id = {record['id']: record for record in records.values()}
        if by_id:
            placeholders = ','.join('?' for _ in by_id)
            for file_id, tag in self.conn.execute(
                    f'''SELECT ft.file_id, t.name FROM file_tags ft
                        JOIN tags t ON t.id=ft.tag_id
                        WHERE ft.file_id IN ({placeholders}) ORDER BY t.name''', tuple(by_id)):
                by_id[file_id]['tags'].append(tag)
        return [dict(id=records[path]['id'], path=path,
                     rating=records[path]['rating'], tags=tuple(records[path]['tags']))
                if path in records else dict(id=None, path=path, rating=None, tags=None)
                for path in normalized]

    def get_curation_restore_state(self, path):
        from core.curation_restore import read_state
        if not isinstance(path, str) or not path.strip():
            raise ValueError('invalid_path')
        return read_state(self.conn, self._normalize_path(path))

    def restore_curation(self, path, *, expected_fingerprint, incoming):
        from core.curation_restore import restore
        if not isinstance(path, str) or not path.strip():
            raise ValueError('invalid_path')
        return restore(self.conn, self._normalize_path(path), expected_fingerprint=expected_fingerprint,
                       incoming=incoming, update_fts=self._fts_sync_file)

    def register_curation_copy(self, path, *, incoming, observation):
        from core.curation_restore import register_copy
        if not isinstance(path, str) or not path.strip():
            raise ValueError('invalid_path')
        return register_copy(self.conn, self._normalize_path(path), incoming=incoming,
                             observation=observation, update_fts=self._fts_sync_file)

    def adopt_curation_identity(self, path, *, expected_fingerprint, incoming):
        from core.curation_restore import adopt_identity
        if not isinstance(path, str) or not path.strip():
            raise ValueError('invalid_path')
        return adopt_identity(self.conn, self._normalize_path(path), expected_fingerprint=expected_fingerprint,
                              incoming=incoming, update_fts=self._fts_sync_file)

    def get_tags_for_file(self, path):
        """Returns list of tag names for a file path."""
        file_id = self.get_file_id(path)
        if not file_id: return []

        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT t.name
            FROM tags t
            JOIN file_tags ft ON t.id = ft.tag_id
            WHERE ft.file_id = ?
        """, (file_id,))
        return [row[0] for row in cursor.fetchall()]

    def edit_curation(self, path, *, add_tags=(), remove_tags=(), rating=None,
                      expected_copy_id=None, expected_fingerprint=None):
        """Delta acotado sobre una fila existente; el servicio confirma todo o nada.

        No registra archivos ni modifica bytes, metadata de generación o hashes.
        """
        if not isinstance(path, str) or not path.strip():
            raise ValueError('invalid_path')
        def normalized(tags):
            if not isinstance(tags, (list, tuple)) or len(tags) > 256:
                raise ValueError('invalid_tags')
            if any(not isinstance(t, str) or not t.strip() or len(t) > 256 for t in tags):
                raise ValueError('invalid_tags')
            return tuple(dict.fromkeys(t.strip().lower() for t in tags))
        added, removed = normalized(add_tags), normalized(remove_tags)
        if len(add_tags) + len(remove_tags) > 256:
            raise ValueError('invalid_tags')
        if set(added).intersection(removed):
            raise ValueError('conflicting_tags')
        if rating is not None and (type(rating) is not int or not 0 <= rating <= 5):
            raise ValueError('invalid_rating')
        if not added and not removed and rating is None:
            raise ValueError('empty_edit')
        if expected_copy_id is not None and (type(expected_copy_id) is not int or not -2**63 <= expected_copy_id <= 2**63-1):
            raise ValueError('invalid_copy_version_id')
        if expected_copy_id is None:
            self._require_unversioned_path(self._normalize_path(path))
        row = self.conn.execute('SELECT id FROM files WHERE is_offline=0 AND path=?',
                                (self._normalize_path(path),)).fetchone()
        if expected_copy_id is not None and (row is None or row[0] != expected_copy_id):
            raise ValueError('curation_copy_changed')
        if expected_fingerprint is not None:
            if expected_copy_id is None:
                raise ValueError('curation_revision_requires_identity')
            from core.library_versions import _reviewed
            _reviewed(self.conn, expected_copy_id, expected_fingerprint)
        if row is None:
            raise ValueError('file_not_registered')
        file_id = row[0]
        changed_tags = False
        if added:
            self.conn.executemany("INSERT OR IGNORE INTO tags(name,category) VALUES(?,'manual')",
                                  [(tag,) for tag in added])
            placeholders = ','.join('?' for _ in added)
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO file_tags(file_id,tag_id,source) "
                f"SELECT ?,id,'manual' FROM tags WHERE name IN ({placeholders})",
                (file_id, *added))
            changed_tags = cursor.rowcount > 0
        if removed:
            placeholders = ','.join('?' for _ in removed)
            cursor = self.conn.execute(
                'DELETE FROM file_tags WHERE file_id=? AND tag_id IN '
                f'(SELECT id FROM tags WHERE name IN ({placeholders}))', (file_id, *removed))
            changed_tags = changed_tags or cursor.rowcount > 0
        if rating is not None:
            self.conn.execute('UPDATE files SET rating=? WHERE id=? AND rating IS NOT ?',
                              (rating, file_id, rating))
        if changed_tags:
            self._fts_sync_file(file_id, self.conn.cursor())
        return True

    def add_tag_to_file(self, path, tag_name, file_data=None):
        """Adds a tag to a file. Creates file record and tag if not exists."""

        self._require_unversioned_path(self._normalize_path(path))
        tag_name = tag_name.lower().strip()
        if not tag_name:
            return False
        file_id = self.get_file_id(path)
        if not file_id and file_data:
            if not self.register_files_minimal([file_data]):
                return False
            file_id = self.get_file_id(path)
        if not file_id:
            return False
        cursor = self.conn.cursor()
        tag_name = tag_name.lower().strip()
        if not tag_name: return False

        try:
            # 2. Ensure Tag Exists
            cursor.execute("SELECT id FROM tags WHERE name = ?", (tag_name,))
            row = cursor.fetchone()
            if row:
                tag_id = row[0]
            else:
                cursor.execute("INSERT INTO tags (name, category) VALUES (?, 'manual')", (tag_name,))
                tag_id = cursor.lastrowid

            # 3. Link Tag
            cursor.execute("INSERT OR IGNORE INTO file_tags (file_id, tag_id, source) VALUES (?, ?, 'manual')", (file_id, tag_id))
            self._fts_sync_file(file_id, cursor)
            return True
        except Exception as e:
            log.warning("[DB] add_tag_to_file error: %s", e)
            return False

    def remove_tag_from_file(self, path, tag_name):
        self._require_unversioned_path(self._normalize_path(path))
        file_id = self.get_file_id(path)
        if not file_id: return False

        cursor = self.conn.cursor()
        tag_name = tag_name.lower().strip()

        try:
            # Get Tag ID
            cursor.execute("SELECT id FROM tags WHERE name = ?", (tag_name,))
            row = cursor.fetchone()
            if not row: return False
            tag_id = row[0]

            # Remove Link
            cursor.execute("DELETE FROM file_tags WHERE file_id = ? AND tag_id = ?", (file_id, tag_id))
            self._fts_sync_file(file_id, cursor)
            return True
        except Exception as e:
            log.warning("[DB] remove_tag_from_file error: %s", e)
            return False


    def search_files_paginated(self, query=None, tags=None, search_terms=None, limit=50,
                               offset=0, *, folder=None, min_rating=0):
        cursor = self.conn.cursor()
        conditions, params = ["is_offline=0"], []

        # 1. Search Terms
        if search_terms:
            for term in search_terms:
                t = f"%{term}%"
                columns = ('meta_positive', 'meta_model', 'meta_tool', 'filename')
                # LIKE conserva substrings, comodines y mayúsculas originales.
                # Trigram sólo ayuda con un tramo literal de al menos 3 caracteres.
                candidates = None
                if any(len(part) >= 3 for part in re.split(r'[%_]', term)):
                    candidates = set()
                    for column in columns:
                        # No materializar decenas de miles de hits de términos comunes.
                        # Si el sondeo alcanza su tope, usar el LIKE de files.
                        probe_limit = 513 - len(candidates)
                        hits = cursor.execute(
                            f'SELECT rowid FROM files_fts WHERE {column} LIKE ? LIMIT ?',
                            (t, probe_limit)).fetchall()
                        if len(hits) == probe_limit:
                            candidates = None
                            break
                        candidates.update(row[0] for row in hits)
                if candidates is not None:
                    conditions.append('files.id IN (' + ','.join('?' for _ in candidates) + ')')
                    params.extend(sorted(candidates))
                # Verificar siempre contra files: un FTS viejo no debe añadir hits falsos.
                conditions.append('(' + ' OR '.join(
                    f'{column} LIKE ?' for column in columns) + ')')
                params.extend([t]*4)

        # 2. Tags
        if tags:
            for tag in tags:
                conditions.append("EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON ft.tag_id=t.id WHERE ft.file_id=files.id AND t.name=?)")
                params.append(tag)

        # 3. Path/Rating
        if folder:
            conditions.append("(path LIKE ? ESCAPE '\\' OR path = ?)")
            params.extend([self._folder_like(folder), self._normalize_path(folder)])
        if min_rating > 0:
            conditions.append('rating >= ?')
            params.append(min_rating)
        if query:
            if "path:" in query:
                # crude parse
                p = query.split("path:")[1].split(" rating:")[0].strip()
                # Contenido de la carpeta, o el archivo exacto si p es un archivo
                conditions.append("(path LIKE ? ESCAPE '\\' OR path = ?)")
                params.extend([self._folder_like(p), self._normalize_path(p)])
            if "rating:" in query:
                try:
                    # Split and pick the part after rating: then strip and take first word/number
                    r_part = query.split("rating:")[1].strip().split(" ")[0]
                    r = int(r_part)
                    conditions.append("rating = ?")
                    params.append(r)
                except (ValueError, IndexError):
                    pass

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        cursor.execute(f"SELECT count(*) FROM files {where}", params)
        total = cursor.fetchone()[0]

        cursor.execute(f"SELECT path, rating FROM files {where} ORDER BY id DESC LIMIT ? OFFSET ?", params + [limit, offset])
        return total, [(r[0], r[1]) for r in cursor.fetchall()]

    def get_folders_paginated(self, limit=50, offset=0):
        # Returns list of watched folders with counts
        cursor = self.conn.cursor()
        cursor.execute("SELECT count(*) FROM watched_folders")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT path FROM watched_folders LIMIT ? OFFSET ?", (limit, offset))
        folders = []
        for row in cursor.fetchall():
            path = row[0]
            count = self.get_folder_count(path)
            prev = self.get_folder_preview(path, 1)
            cover = prev[0] if prev else None
            folders.append({'path': path, 'name': os.path.basename(path), 'count': count, 'cover': cover})

        return total, folders

    # --- Backfill de identidad de contenido (M3, CHERRY_FUSION_DESIGN) ---

    def count_files_missing_hash(self):
        cursor = self.conn.cursor()
        return cursor.execute(
            "SELECT count(*) FROM files WHERE is_offline=0 AND hash_original IS NULL"
        ).fetchone()[0]

    def get_files_missing_hash(self, after_id=0, limit=200):
        """Lote de (id, path) sin hash, por keyset (id > after_id): los
        archivos que fallen al hashear no re-entran en la misma corrida."""
        cursor = self.conn.cursor()
        return cursor.execute(
            "SELECT id, path FROM files WHERE is_offline=0 AND hash_original IS NULL AND id > ? "
            "ORDER BY id LIMIT ?", (after_id, limit)
        ).fetchall()

    def set_hash_original_bulk(self, pairs):
        """pairs: [(hash_hex, file_id)]. El guard IS NULL preserva la
        inmutabilidad de hash_original ante corridas concurrentes."""
        if not pairs: return True
        try:
            self.conn.executemany(
                "UPDATE files SET hash_original=? WHERE is_offline=0 AND id=? AND hash_original IS NULL",
                pairs
            )
            return True
        except sqlite3.Error as e:
            log.warning("[DB] set_hash_original_bulk error: %s", e)
            return False

    def vacuum_database(self):
        try:
            self.conn.execute("VACUUM")
            return True
        except Exception as e:
            log.warning("[DB] VACUUM error: %s", e)
            return False

    def get_all_tags(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM tags ORDER BY name")
        return [r[0] for r in cursor.fetchall()]

    def get_file_rating(self, path):
        cursor = self.conn.cursor()
        cursor.execute("SELECT rating FROM files WHERE is_offline=0 AND path=?", (self._normalize_path(path),))
        r = cursor.fetchone()
        return r[0] if r else 0

    def get_library_overview(self):
        """Carpetas, conteo y vocabulario en el snapshot RO del servicio."""
        return dict(folders=self.get_watched_folders(), count=self.count_files(),
                    tags=self.get_all_tags())

    def get_library_selection(self, *, terms=(), folder=None, limit=10000):
        """Conteo completo y muestra acotada; no accede al sistema de archivos."""
        if (not isinstance(terms, (list, tuple)) or len(terms) > 256 or
                any(not isinstance(term, str) or not term.strip() or len(term) > 256
                    for term in terms) or type(limit) is not int or not 1 <= limit <= 10000 or
                (folder is not None and (not isinstance(folder, str) or not folder))):
            raise ValueError('invalid_selection')
        if terms:
            count, paths = self.search_by_terms(terms, limit=limit)
        elif folder:
            count = self.get_folder_count(folder)
            paths = self.get_files_recursive(folder, limit=limit)
        else:
            count, paths = 0, []
        return dict(count=count, paths=paths)

    def _selection_filter(self, terms, folder):
        validate_selection(terms, folder)
        if terms:
            return term_filter(terms)
        if folder:
            return "path LIKE ? ESCAPE '\\'", [self._folder_like(folder)]
        return '', []

    def get_library_page(self, *, terms=(), folder=None, limit=100, offset=0):
        """Página SQL y conteo del mismo snapshot; orden estable por identidad."""
        if (type(limit) is not int or not 1 <= limit <= 256 or
                type(offset) is not int or not 0 <= offset <= 2**63 - 1 or offset % limit):
            raise ValueError('invalid_page')
        where, params = self._selection_filter(terms, folder)
        return select(self.conn, where, params, limit, offset)

    def resolve_library_selection(self, *, terms=(), folder=None, limit=1000):
        """Entrega completa acotada o error explícito; nunca envía un prefijo."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('invalid_selection_limit')
        where, params = self._selection_filter(terms, folder)
        return select(self.conn, where, params, limit, complete=True)

    def count_files(self):
        return self.conn.execute('SELECT count(*) FROM files WHERE is_offline=0').fetchone()[0]

    def update_file_rating(self, path, rating):
        self._require_unversioned_path(self._normalize_path(path))
        try:
            cursor = self.conn.execute("UPDATE files SET rating=? WHERE is_offline=0 AND path=?", (rating, self._normalize_path(path)))
            return cursor.rowcount > 0
        except Exception as e:
            log.warning("[DB] update_file_rating error: %s", e)
            return False

    def _require_unversioned_path(self, path):
        # Lecturas por ruta muestran la copia operativa; una escritura antigua sin
        # ID/revisión no puede atribuir datos a otra versión recién admitida.
        if self.conn.execute('SELECT 1 FROM files WHERE path=? AND is_offline=1 LIMIT 1', (path,)).fetchone():
            raise ValueError('version_identity_required')

    def get_copy_versions_page(self, **kwargs):
        from core.library_versions import page
        return page(self.conn, **kwargs)

    def get_copy_version_state(self, copy_id):
        from core.library_versions import state
        return state(self.conn, copy_id)

    def get_metadata_edit_state(self, path):
        from core.generation_history import edit_state
        return edit_state(self.conn, path, normalize=self._normalize_path)

    def get_generation_history_status(self):
        from core.generation_history import status
        return status(self.conn)

    def get_metadata_path_status(self, path):
        from core.generation_history import path_status
        return path_status(self.conn, path, normalize=self._normalize_path)

    def get_generation_history_page(self, copy_id, **kwargs):
        from core.generation_history import page
        return page(self.conn, copy_id, **kwargs)

    def prepare_generation_history(self):
        from core.generation_history import prepare
        return prepare(self.conn)

    def approve_metadata_edit(self, copy_id, **kwargs):
        from core.generation_history import approve
        return approve(self.conn, copy_id, **kwargs)

    def commit_metadata_edit(self, copy_id, **kwargs):
        from core.generation_history import commit
        return commit(self.conn, copy_id, update_fts=self._fts_sync_file, **kwargs)

    def get_derived_copies_status(self):
        from core.derived_copies import status
        return status(self.conn)

    def get_derived_copy_receipt(self, operation_id):
        from core.derived_copies import receipt
        return receipt(self.conn, operation_id)

    def prepare_derived_copies(self):
        from core.derived_copies import prepare
        return prepare(self.conn)

    def approve_derived_copy(self, copy_id, **kwargs):
        from core.derived_copies import approve
        return approve(self.conn, copy_id, normalize=self._normalize_path, **kwargs)

    def register_derived_copy(self, copy_id, **kwargs):
        from core.derived_copies import register
        return register(self.conn, copy_id, normalize=self._normalize_path,
                        update_fts=self._fts_sync_file, **kwargs)

    def get_duplicate_snapshot(self, paths):
        from core.duplicate_snapshot import snapshot
        return snapshot(self.conn, paths, self._normalize_path)

    def get_duplicate_retirement_roots(self):
        # No cargar una lista ilimitada para validar un destino de recuperación.
        from core.duplicate_archive import roots
        return roots(self.conn)

    def get_duplicate_candidates_page(self, **kwargs):
        from core.duplicate_candidates import page
        return page(self.conn, **kwargs)

    def get_duplicate_candidate_copies(self, digest, **kwargs):
        from core.duplicate_candidates import copies
        return copies(self.conn, digest, **kwargs)

    def get_duplicate_search_status(self):
        from core.duplicate_index import status
        return status(self.conn)

    def get_folder_overlap(self, folders, **kwargs):
        from core.folder_overlap import compare
        return compare(self.conn, folders, normalize=self._normalize_path, **kwargs)

    def get_phash_registry_status(self):
        from core.perceptual_hash import status
        return status(self.conn)

    def get_phash_states(self, paths, algorithm):
        from core.perceptual_hash import states
        return states(self.conn, paths, algorithm, normalize=self._normalize_path)

    def get_phash_page(self, folders, algorithm, **kwargs):
        from core.perceptual_hash import page
        return page(self.conn, folders, algorithm, normalize=self._normalize_path, **kwargs)

    def prepare_phash_registry(self):
        from core.perceptual_hash import prepare
        return prepare(self.conn)

    def store_phash_batch(self, records, algorithm):
        from core.perceptual_hash import store
        return store(self.conn, records, algorithm, normalize=self._normalize_path)

    def prepare_duplicate_search(self):
        from core.duplicate_index import prepare
        return prepare(self.conn)

    def archive_duplicate_copies(self, approved, selected, **kwargs):
        from core.duplicate_archive import archive
        return archive(self.conn, approved, selected, normalize_path=self._normalize_path, **kwargs)

    def get_duplicate_recovery_state(self, copy_ids):
        from core.duplicate_snapshot import recovery_states
        return recovery_states(self.conn, copy_ids)

    def restore_duplicate_copy(self,copy_id,**kwargs):
        from core.duplicate_restore import restore
        return restore(self.conn,copy_id,normalize=self._normalize_path,**kwargs)

    def approve_duplicate_purge(self,approved,**kwargs):
        from core.duplicate_purge import approve
        return approve(self.conn,approved,normalize=self._normalize_path,**kwargs)

    def archive_copy(self, copy_id, **kwargs):
        from core.library_versions import archive
        return archive(self.conn, copy_id, **kwargs)

    def make_copy_online(self, copy_id, **kwargs):
        from core.library_versions import make_online
        return make_online(self.conn, copy_id, update_fts=self._fts_sync_file, **kwargs)
