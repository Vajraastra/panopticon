import sqlite3
import os
import logging
import re


log = logging.getLogger(__name__)

class LibraryRepository:
    """SQL privado. La conexión se crea, usa y cierra en el hilo dueño."""

    def __init__(self, conn):
        self.conn = conn

    def create_schema(self):
        cursor = self.conn.cursor()

        # Files Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
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
                artist TEXT          -- display_name del perfil cherry
            )
        """)

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

        # Migración FTS: si el índice está vacío pero ya hay archivos (DB previa
        # a FTS5), reconstruirlo una vez para no dejar la búsqueda sin resultados.
        n_fts = cursor.execute("SELECT count(*) FROM files_fts").fetchone()[0]
        n_files = cursor.execute("SELECT count(*) FROM files").fetchone()[0]
        if n_files > 0 and n_fts == 0 and not self.rebuild_fts():
            raise sqlite3.DatabaseError('FTS migration failed')

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
                "WHERE ft.file_id=?", (file_id,)
            ).fetchall()
        )
        cur.execute("DELETE FROM files_fts WHERE rowid=?", (file_id,))
        cur.execute(
            "INSERT INTO files_fts(rowid, filename, meta_positive, meta_negative, "
            "meta_model, meta_tool, tags) VALUES (?,?,?,?,?,?,?)",
            (file_id, row[0] or '', row[1] or '', row[2] or '', row[3] or '', row[4] or '', tags)
        )

    def rebuild_fts(self):
        """Reconstruye el índice FTS completo desde cero (migración / reparación).
        Costoso (recorre todos los files) pero solo se llama una vez."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("DELETE FROM files_fts")
            for (file_id,) in cursor.execute("SELECT id FROM files").fetchall():
                self._fts_sync_file(file_id, cursor)
            log.info("[DB] Índice FTS reconstruido.")
            return True
        except sqlite3.Error as e:
            log.error("[DB] rebuild_fts falló: %s", e)
            return False

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
        # hash_original es INMUTABLE: si la fila ya lo tiene, se conserva
        # (identidad "tal como se catalogó por 1ª vez"). El resto de campos
        # de origen se actualizan solo si llega un valor nuevo no-NULL.
        sql = """
            INSERT INTO files (path, filename, file_size, created_date,
                               hash_original, origin_url, origin_site, artist)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                file_size=excluded.file_size,
                created_date=excluded.created_date,
                hash_original=COALESCE(files.hash_original, excluded.hash_original),
                origin_url=COALESCE(excluded.origin_url, files.origin_url),
                origin_site=COALESCE(excluded.origin_site, files.origin_site),
                artist=COALESCE(excluded.artist, files.artist)
        """

        params = []
        norm_paths = []
        for f in files_data:
            np = self._normalize_path(f['path'])
            norm_paths.append(np)
            params.append((np, f['filename'], f['size'], f['created'],
                           f.get('hash_original'), f.get('origin_url'),
                           f.get('origin_site'), f.get('artist')))

        try:
            cursor.executemany(sql, params)
            # Sincronizar el índice FTS del lote (filename; metas/tags llegan
            # luego vía update_file_metadata/add_tag y se re-sincronizan ahí).
            placeholders = ",".join("?" * len(norm_paths))
            ids = cursor.execute(
                f"SELECT id FROM files WHERE path IN ({placeholders})", norm_paths
            ).fetchall()
            for (fid,) in ids:
                self._fts_sync_file(fid, cursor)
            return True
        except sqlite3.Error as e:
            log.warning("[DB] Bulk Insert Error: %s", e)
            return False

    def update_file_metadata(self, path, meta):
        """
        Phase 2: Update existing record with rich metadata.
        """
        cursor = self.conn.cursor()
        norm_path = self._normalize_path(path)

        sql = """
            UPDATE files SET
                width=?, height=?, meta_tool=?, meta_model=?,
                meta_positive=?, meta_negative=?, meta_seed=?
            WHERE path=?
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
            row = cursor.execute("SELECT id FROM files WHERE path=?", (norm_path,)).fetchone()
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
        cursor.execute("SELECT path FROM files WHERE path LIKE ? ESCAPE '\\'", (msg_path,))
        return set(row[0] for row in cursor.fetchall())

    def get_files_recursive(self, folder_path, limit=None):
        """Returns a list of all files under a folder (recursive)."""
        cursor = self.conn.cursor()
        msg_path = self._folder_like(folder_path)
        if limit:
            cursor.execute("SELECT path FROM files WHERE path LIKE ? ESCAPE '\\' LIMIT ?", (msg_path, limit))
        else:
            cursor.execute("SELECT path FROM files WHERE path LIKE ? ESCAPE '\\'", (msg_path,))
        return [row[0] for row in cursor.fetchall()]

    def remove_files(self, paths_list):
        if not paths_list: return True
        cursor = self.conn.cursor()
        try:
            for p in paths_list:
                norm = self._normalize_path(p)
                row = cursor.execute("SELECT id FROM files WHERE path = ?", (norm,)).fetchone()
                cursor.execute("DELETE FROM files WHERE path = ?", (norm,))
                if row:
                    cursor.execute("DELETE FROM files_fts WHERE rowid=?", (row[0],))
            return True
        except Exception as e:
            log.warning("[DB] remove_files error: %s", e)
            return False

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
            # Limpiar el índice FTS de los archivos que se van a borrar.
            ids = cursor.execute(
                "SELECT id FROM files WHERE path LIKE ? ESCAPE '\\'", (like,)
            ).fetchall()
            cursor.execute("DELETE FROM files WHERE path LIKE ? ESCAPE '\\'", (like,))
            for (fid,) in ids:
                cursor.execute("DELETE FROM files_fts WHERE rowid=?", (fid,))
            return True
        except Exception as e:
            log.warning("[DB] remove_watched_folder error: %s", e)
            return False

    def get_folder_count(self, folder_path):
        cursor = self.conn.cursor()
        query = self._folder_like(folder_path)
        cursor.execute("SELECT count(*) FROM files WHERE path LIKE ? ESCAPE '\\'", (query,))
        return cursor.fetchone()[0]

    def get_folder_preview(self, folder_path, limit=5):
        cursor = self.conn.cursor()
        query = self._folder_like(folder_path)
        cursor.execute("SELECT path FROM files WHERE path LIKE ? ESCAPE '\\' LIMIT ?", (query, limit))
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
        conditions, params = [], []

        long_terms = [t.strip() for t in terms if len(t.strip()) >= 3]
        short_terms = [t.strip() for t in terms if 0 < len(t.strip()) < 3]

        # Términos ≥3 chars → UNA búsqueda FTS5 con AND implícito entre términos.
        # Cada término entrecomillado (y "" escapado) → subcadena vía trigram en
        # cualquier columna indexada (filename/meta*/tags) = el viejo OR de 6.
        if long_terms:
            match_expr = " ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
            conditions.append("files.id IN (SELECT rowid FROM files_fts WHERE files_fts MATCH ?)")
            params.append(match_expr)

        # Términos <3 chars → LIKE de respaldo (mismo OR de 6 campos de antes).
        for t in short_terms:
            like = f"%{t}%"
            conditions.append("""(
                meta_positive LIKE ? OR meta_negative LIKE ? OR meta_model LIKE ?
                OR meta_tool LIKE ? OR filename LIKE ?
                OR EXISTS (
                    SELECT 1 FROM file_tags ft JOIN tags tg ON ft.tag_id = tg.id
                    WHERE ft.file_id = files.id AND tg.name LIKE ?
                )
            )""")
            params.extend([like] * 6)

        if not conditions:  # todos los términos quedaron vacíos tras strip
            return 0, []

        where = "WHERE " + " AND ".join(conditions)

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
        cursor.execute("SELECT id FROM files WHERE path = ?", (norm_path,))
        row = cursor.fetchone()
        return row[0] if row else None

    def get_curation_snapshot(self, paths):
        """Hasta 256 rutas, dos SELECT en el snapshot del lector; sin registrar archivos.

        Conserva orden/duplicados. id=None distingue ausencia de curación vacía;
        cero estrellas y tags vacíos de una fila registrada son autoritativos.
        """
        if not isinstance(paths, (list, tuple)) or len(paths) > 256:
            raise ValueError('Curation snapshot requires at most 256 paths')
        if any(not isinstance(path, str) or not path for path in paths):
            raise ValueError('Curation paths must be nonempty strings')
        normalized = [self._normalize_path(path) for path in paths]
        unique = tuple(dict.fromkeys(normalized))
        if not unique:
            return []
        placeholders = ','.join('?' for _ in unique)
        records = {}
        for file_id, path, rating in self.conn.execute(
                f'SELECT id, path, rating FROM files WHERE path IN ({placeholders})', unique):
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

    def edit_curation(self, path, *, add_tags=(), remove_tags=(), rating=None):
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
        row = self.conn.execute('SELECT id FROM files WHERE path=?',
                                (self._normalize_path(path),)).fetchone()
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
        conditions, params = [], []

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
            "SELECT count(*) FROM files WHERE hash_original IS NULL"
        ).fetchone()[0]

    def get_files_missing_hash(self, after_id=0, limit=200):
        """Lote de (id, path) sin hash, por keyset (id > after_id): los
        archivos que fallen al hashear no re-entran en la misma corrida."""
        cursor = self.conn.cursor()
        return cursor.execute(
            "SELECT id, path FROM files WHERE hash_original IS NULL AND id > ? "
            "ORDER BY id LIMIT ?", (after_id, limit)
        ).fetchall()

    def set_hash_original_bulk(self, pairs):
        """pairs: [(hash_hex, file_id)]. El guard IS NULL preserva la
        inmutabilidad de hash_original ante corridas concurrentes."""
        if not pairs: return True
        try:
            self.conn.executemany(
                "UPDATE files SET hash_original=? WHERE id=? AND hash_original IS NULL",
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
        cursor.execute("SELECT rating FROM files WHERE path=?", (self._normalize_path(path),))
        r = cursor.fetchone()
        return r[0] if r else 0

    def count_files(self):
        return self.conn.execute('SELECT count(*) FROM files').fetchone()[0]

    def update_file_rating(self, path, rating):
        try:
            cursor = self.conn.execute("UPDATE files SET rating=? WHERE path=?", (rating, self._normalize_path(path)))
            return cursor.rowcount > 0
        except Exception as e:
            log.warning("[DB] update_file_rating error: %s", e)
            return False
