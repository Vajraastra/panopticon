import sqlite3
import os
import logging
from PySide6.QtCore import QObject

from core.paths import ProjectPaths

log = logging.getLogger(__name__)

class DatabaseManager(QObject):
    """
    Robust Database Manager for Librarian.
    Focus: Reliability and Safety.
    """

    def __init__(self, db_path=None):
        super().__init__()
        # Ruta absoluta a la DB compartida en la raíz del proyecto: todos los
        # módulos abren el MISMO archivo sin depender del CWD de lanzamiento.
        self.db_path = str(db_path) if db_path else str(ProjectPaths.root() / "panopticon.db")
        self.conn = None
        self.init_db()

    def init_db(self):
        try:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            # WAL: lectores no bloquean al escritor. Hay varias conexiones al
            # mismo panopticon.db (librarian, gallery, image_optimizer, etc.)
            # → sin esto hay riesgo de "database is locked"
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.create_schema()
            self.run_transformations() # Upgrades
        except sqlite3.Error as e:
            log.error("[DB FATAL] %s", e)

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

        self.conn.commit()

    def run_transformations(self):
        """Ensures schema is up to date."""
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(files)")
        cols = [row[1] for row in cursor.fetchall()]
        if 'rating' not in cols:
            cursor.execute("ALTER TABLE files ADD COLUMN rating INTEGER DEFAULT 0")
            self.conn.commit()

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
        self.conn.commit()

        # Migración FTS: si el índice está vacío pero ya hay archivos (DB previa
        # a FTS5), reconstruirlo una vez para no dejar la búsqueda sin resultados.
        try:
            n_fts = cursor.execute("SELECT count(*) FROM files_fts").fetchone()[0]
            n_files = cursor.execute("SELECT count(*) FROM files").fetchone()[0]
            if n_files > 0 and n_fts == 0:
                self.rebuild_fts()
        except sqlite3.Error as e:
            log.warning("[DB] Chequeo de migración FTS falló: %s", e)

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
            self.conn.commit()
            log.info("[DB] Índice FTS reconstruido.")
        except sqlite3.Error as e:
            log.error("[DB] rebuild_fts falló: %s", e)
            self.conn.rollback()

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
        if not files_data: return

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
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            log.warning("[DB] Bulk Insert Error: %s", e)
            self.conn.rollback()
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
            self.conn.commit()
        except Exception as e:
            log.warning("[DB] Meta Update Error for %s: %s", path, e)

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
        if not paths_list: return
        cursor = self.conn.cursor()
        try:
            for p in paths_list:
                norm = self._normalize_path(p)
                row = cursor.execute("SELECT id FROM files WHERE path = ?", (norm,)).fetchone()
                cursor.execute("DELETE FROM files WHERE path = ?", (norm,))
                if row:
                    cursor.execute("DELETE FROM files_fts WHERE rowid=?", (row[0],))
            self.conn.commit()
        except Exception as e:
            log.warning("[DB] remove_files error: %s", e)
            self.conn.rollback()
            
    # --- Standard Queries (Gallery/Search) ---
    
    def get_watched_folders(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT path FROM watched_folders")
        return [row[0] for row in cursor.fetchall()]
        
    def add_watched_folder(self, path):
        cursor = self.conn.cursor()
        try:
            cursor.execute("INSERT OR IGNORE INTO watched_folders (path) VALUES (?)", (self._normalize_path(path),))
            self.conn.commit()
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
            self.conn.commit()
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

    def add_tag_to_file(self, path, tag_name):
        """Adds a tag to a file. Creates file record and tag if not exists."""
        
        # 1. Ensure File Exists in DB (Lazy Registration)
        file_id = self.get_file_id(path)
        if not file_id:
            try:
                stat = os.stat(path)
                file_data = [{
                    'path': path,
                    'filename': os.path.basename(path),
                    'size': stat.st_size,
                    'created': stat.st_ctime
                }]
                if self.register_files_minimal(file_data):
                     file_id = self.get_file_id(path)
            except Exception as e:
                log.warning("[DB] Failed to auto-register file %s: %s", path, e)
                
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
            self.conn.commit()
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
            self.conn.commit()
            return True
        except Exception as e:
            log.warning("[DB] remove_tag_from_file error: %s", e)
            return False


    def search_files_paginated(self, query=None, tags=None, search_terms=None, limit=50, offset=0):
        # ... (Identical to previous implementation to preserve Gallery logic) ...
        # For brevity in rewrite, reusing the robust query logic
        cursor = self.conn.cursor()
        conditions, params = [], []
        
        # 1. Search Terms
        if search_terms:
            for term in search_terms:
                t = f"%{term}%"
                conditions.append("""(
                    meta_positive LIKE ? OR meta_model LIKE ? OR meta_tool LIKE ? OR filename LIKE ?
                )""")
                params.extend([t]*4)
                
        # 2. Tags
        if tags:
            for tag in tags:
                conditions.append("EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON ft.tag_id=t.id WHERE ft.file_id=files.id AND t.name=?)")
                params.append(tag)
        
        # 3. Path/Rating
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
        
    def vacuum_database(self):
        try:
            self.conn.execute("VACUUM")
        except Exception as e:
            log.warning("[DB] VACUUM error: %s", e)
        
    def get_all_tags(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM tags ORDER BY name")
        return [r[0] for r in cursor.fetchall()]
        
    def get_file_rating(self, path):
        cursor = self.conn.cursor()
        cursor.execute("SELECT rating FROM files WHERE path=?", (self._normalize_path(path),))
        r = cursor.fetchone()
        return r[0] if r else 0
        
    def update_file_rating(self, path, rating):
        try:
            self.conn.execute("UPDATE files SET rating=? WHERE path=?", (rating, self._normalize_path(path)))
            self.conn.commit()
            return True
        except Exception as e:
            log.warning("[DB] update_file_rating error: %s", e)
            return False
