import sqlite3
import os
from PySide6.QtCore import QObject, Signal

class DatabaseManager(QObject):
    """
    Manages the SQLite database connection and schema.
    Implemented as a Singleton-like QObject to be shared across the module.
    """
    
    def __init__(self, db_path="panopticon.db"):
        super().__init__()
        self.db_path = db_path
        self.conn = None
        self.init_db()

    def init_db(self):
        """Initialize the database and create tables if they don't exist."""
        try:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False) # Allow access from worker threads
            self.create_schema()
        except sqlite3.Error as e:
            print(f"Database initialization error: {e}")

    def create_schema(self):
        """Creates the necessary tables for the Librarian."""
        cursor = self.conn.cursor()
        
        # Files Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                filename TEXT,
                file_size INTEGER,
                width INTEGER,
                height INTEGER,
                created_date DATETIME,
                
                -- Metadata from Parser
                meta_tool TEXT,
                meta_model TEXT,
                meta_positive TEXT,
                meta_negative TEXT,
                meta_seed TEXT
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
                source TEXT DEFAULT 'manual', -- 'manual', 'auto', 'ai'
                FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE,
                FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE,
                UNIQUE(file_id, tag_id)
            )
        """)
        
        # Watched Folders Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watched_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        self.conn.commit()

    def close(self):
        if self.conn:
            self.conn.close()

    # --- Watched Folders Operations ---

    def add_watched_folder(self, path):
        cursor = self.conn.cursor()
        try:
            cursor.execute("INSERT OR IGNORE INTO watched_folders (path) VALUES (?)", (path,))
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            print(f"Error adding folder: {e}")
            return False

    def remove_watched_folder(self, path):
        cursor = self.conn.cursor()
        try:
            cursor.execute("DELETE FROM watched_folders WHERE path = ?", (path,))
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            print(f"Error removing folder: {e}")
            return False

    def get_watched_folders(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT path FROM watched_folders")
        return [row[0] for row in cursor.fetchall()]

    # --- Preview & Stats Operations ---

    def get_folder_preview(self, folder_path, limit=5):
        """Returns the first 'limit' image paths that reside within 'folder_path'."""
        cursor = self.conn.cursor()
        # Ensure path ends with separator for correct LIKE query (avoid partial matches)
        # SQLite uses % as wildcard. escape spec chars if needed, but path usually safe.
        search_path = os.path.normpath(folder_path)
        
        # We need to find files that start with this path
        # Note: This is a simple prefix match. It includes subfolders.
        query_path = f"{search_path}%"
        
        cursor.execute("SELECT path FROM files WHERE path LIKE ? LIMIT ?", (query_path, limit))
        return [row[0] for row in cursor.fetchall()]

    def get_folder_count(self, folder_path):
        """Returns the total number of files indexed within a specific folder."""
        cursor = self.conn.cursor()
        search_path = os.path.normpath(folder_path)
        query_path = f"{search_path}%"
        cursor.execute("SELECT count(*) FROM files WHERE path LIKE ?", (query_path,))
        return cursor.fetchone()[0]

    def search_files_paginated(self, query=None, tags=None, limit=50, offset=0):
        """
        Main query method for the Gallery. 
        Supports pagination and optional tag filtering.
        Returns: (total_count, list_of_paths)
        """
        cursor = self.conn.cursor()
        conditions = []
        params = []
        
        # 1. Build Query Filters using LIKE matching similar to search_by_terms
        search_terms = tags if tags else []
        
        for term in search_terms:
            like_term = f"%{term}%"
            sub_query = """(
                meta_positive LIKE ? OR 
                meta_negative LIKE ? OR 
                meta_model LIKE ? OR 
                meta_tool LIKE ? OR 
                filename LIKE ? OR
                EXISTS (
                    SELECT 1 FROM file_tags ft 
                    JOIN tags t ON ft.tag_id = t.id 
                    WHERE ft.file_id = files.id AND t.name LIKE ?
                )
            )"""
            conditions.append(sub_query)
            params.extend([like_term] * 6)
            
        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)
        
        # Add Filter by path if query is passed (e.g. for folder view filtering)
        # We overload 'query' arg to pass a folder path constraint if needed
        # Or we can handle it in the args more explicitly. 
        # For simplicity, let's assume 'tags' handles text search, 'query' manages specific constraints like path.
        if query and query.startswith("path:"):
            raw_path = query.replace("path:", "").strip()
            # Normalize to match how we store files (os.path.normpath)
            folder_path = os.path.normpath(raw_path)
            
            # If we already have a WHERE, append AND, else WHERE
            prefix = " AND " if where_clause else "WHERE "
            where_clause += f"{prefix}path LIKE ?"
            
            # Append % for wildcard
            params.append(f"{folder_path}%")

        # 2. Get Total Count (for pagination)
        count_sql = f"SELECT count(*) FROM files {where_clause}"
        cursor.execute(count_sql, params)
        total_count = cursor.fetchone()[0]
        
        # 3. Get Paginated Results
        # ORDER BY id DESC to show newest first
        # We append LIMIT/OFFSET to the EXISTING params
        data_sql = f"SELECT path FROM files {where_clause} ORDER BY id DESC LIMIT ? OFFSET ?"
        data_params = params + [limit, offset]
        
        cursor.execute(data_sql, data_params)
        paths = [row[0] for row in cursor.fetchall()]
        
        return total_count, paths

    def get_folders_paginated(self, limit=20, offset=0):
        """
        Returns a list of 'Watched Folders' with metadata for the Gallery Album View.
        Returns: (total_folders, list_of_dicts)
        Each dict: {'path': str, 'name': str, 'cover': str|None, 'count': int}
        """
        cursor = self.conn.cursor()
        
        # 1. Total Folders
        cursor.execute("SELECT count(*) FROM watched_folders")
        total_folders = cursor.fetchone()[0]
        
        # 2. Get Page of Folders
        cursor.execute("SELECT path FROM watched_folders LIMIT ? OFFSET ?", (limit, offset))
        rows = cursor.fetchall()
        
        albums = []
        for row in rows:
            folder_path = row[0]
            
            # Get Count
            # We already have get_folder_count logic, let's inline or reuse
            msg_path = os.path.normpath(folder_path) + "%"
            cursor.execute("SELECT count(*) FROM files WHERE path LIKE ?", (msg_path,))
            count = cursor.fetchone()[0]
            
            # Get Cover (First image)
            cursor.execute("SELECT path FROM files WHERE path LIKE ? LIMIT 1", (msg_path,))
            cover_row = cursor.fetchone()
            cover_path = cover_row[0] if cover_row else None
            
            albums.append({
                'path': folder_path,
                'name': os.path.basename(folder_path),
                'cover': cover_path,
                'count': count
            })
            
        return total_folders, albums
        
    def get_file_id(self, path):
        cursor = self.conn.cursor()
        cursor.execute("SELECT id FROM files WHERE path = ?", (path,))
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
        """Adds a tag to a file. Creates tag if not exists."""
        file_id = self.get_file_id(path)
        if not file_id: return False
        
        cursor = self.conn.cursor()
        tag_name = tag_name.lower().strip()
        if not tag_name: return False
        
        # 1. Ensure Tag Exists
        cursor.execute("SELECT id FROM tags WHERE name = ?", (tag_name,))
        row = cursor.fetchone()
        if row:
            tag_id = row[0]
        else:
            cursor.execute("INSERT INTO tags (name, category) VALUES (?, 'manual')", (tag_name,))
            tag_id = cursor.lastrowid
            
        # 2. Link Tag
        try:
            cursor.execute("INSERT OR IGNORE INTO file_tags (file_id, tag_id, source) VALUES (?, ?, 'manual')", (file_id, tag_id))
            self.conn.commit()
            return True
        except Exception as e:
            print(f"Error adding tag: {e}")
            return False

    def remove_tag_from_file(self, path, tag_name):
        file_id = self.get_file_id(path)
        if not file_id: return False
        
        cursor = self.conn.cursor()
        tag_name = tag_name.lower().strip()
        
        # Get Tag ID
        cursor.execute("SELECT id FROM tags WHERE name = ?", (tag_name,))
        row = cursor.fetchone()
        if not row: return False
        tag_id = row[0]
        
        # Remove Link
        cursor.execute("DELETE FROM file_tags WHERE file_id = ? AND tag_id = ?", (file_id, tag_id))
        self.conn.commit()
        self.conn.commit()
        return True
        
    def search_by_terms(self, terms, limit=5):
        """
        Searches for files containing ALL terms in their metadata (positive prompt, model, tool).
        Returns a tuple: (total_count, list_of_preview_paths)
        """
        if not terms:
            return 0, []
            
        cursor = self.conn.cursor()

        # Build dynamic query
        # WHERE (meta_positive LIKE %term1% OR meta_model LIKE %term1% ...) AND ...
        conditions = []
        params = []
        
        for term in terms:
            # Wrap term in % for partial match
            like_term = f"%{term}%"
            # We search across relevant text fields AND tags
            sub_query = """(
                meta_positive LIKE ? OR 
                meta_negative LIKE ? OR 
                meta_model LIKE ? OR 
                meta_tool LIKE ? OR 
                filename LIKE ? OR
                EXISTS (
                    SELECT 1 FROM file_tags ft 
                    JOIN tags t ON ft.tag_id = t.id 
                    WHERE ft.file_id = files.id AND t.name LIKE ?
                )
            )"""
            conditions.append(sub_query)
            # Add params for each ? in the sub_query (5 metadata + 1 tag = 6)
            params.extend([like_term] * 6)
            
        where_clause = " AND ".join(conditions)
        
        # Count Query
        count_sql = f"SELECT count(*) FROM files WHERE {where_clause}"
        cursor.execute(count_sql, params)
        total_count = cursor.fetchone()[0]
        
        # Preview Query
        preview_sql = f"SELECT path FROM files WHERE {where_clause} LIMIT ?"
        # We need to add the limit param to the end of the existing params list
        preview_params = params + [limit]
        cursor.execute(preview_sql, preview_params)
        preview_paths = [row[0] for row in cursor.fetchall()]
        
        return total_count, preview_paths

    # --- Basic CRUD Operations ---

    def add_file(self, path, stats=None, metadata=None):
        """
        Inserts a file into the database. 
        Returns the new file ID or the existing ID if it's already there.
        """
        cursor = self.conn.cursor()
        
        # Check if exists
        cursor.execute("SELECT id FROM files WHERE path = ?", (path,))
        row = cursor.fetchone()
        if row:
            return row[0]
            
        # Parse data safely
        stats = stats or {}
        metadata = metadata or {}
        
        try:
            cursor.execute("""
                INSERT INTO files (
                    path, filename, file_size, width, height, created_date,
                    meta_tool, meta_model, meta_positive, meta_negative, meta_seed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                path,
                os.path.basename(path),
                stats.get('size_bytes', 0),
                metadata.get('width', 0),
                metadata.get('height', 0),
                stats.get('created', None),
                
                metadata.get('tool', 'Unknown'),
                metadata.get('model', 'Unknown'),
                metadata.get('positive', ''),
                metadata.get('negative', ''),
                str(metadata.get('seed', ''))
            ))
            self.conn.commit()
            return cursor.lastrowid
        except sqlite3.Error as e:
            print(f"Error adding file {path}: {e}")
            return None

    # --- Optimized Batch Operations for Indexer ---

    def get_all_file_paths(self):
        """Returns a set of all file paths currently in the DB for O(1) checks."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT path FROM files")
        # Normalize paths in set for reliable comparison
        return set(os.path.normpath(row[0]) for row in cursor.fetchall())

    def add_many_files(self, paths, stats_list, meta_list):
        """
        Transactional bulk insert for high performance.
        All lists must be same length.
        """
        if not paths: return
        
        data_to_insert = []
        for i in range(len(paths)):
            p = paths[i]
            s = stats_list[i]
            m = meta_list[i]
            
            data_to_insert.append((
                p,
                os.path.basename(p),
                s.get('size_bytes', 0),
                s.get('width', 0) or m.get('width', 0),
                s.get('height', 0) or m.get('height', 0),
                s.get('created', None),
                
                m.get('tool', 'Unknown'),
                m.get('model', 'Unknown'),
                m.get('positive', ''),
                m.get('negative', ''),
                str(m.get('seed', ''))
            ))
            
        cursor = self.conn.cursor()
        try:
            cursor.executemany("""
                INSERT OR IGNORE INTO files (
                    path, filename, file_size, width, height, created_date,
                    meta_tool, meta_model, meta_positive, meta_negative, meta_seed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, data_to_insert)
            self.conn.commit()
        except sqlite3.Error as e:
            print(f"Batch insert error: {e}")

