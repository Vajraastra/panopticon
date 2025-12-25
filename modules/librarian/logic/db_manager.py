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
            # We search across relevant text fields
            sub_query = "(meta_positive LIKE ? OR meta_negative LIKE ? OR meta_model LIKE ? OR meta_tool LIKE ? OR filename LIKE ?)"
            conditions.append(sub_query)
            # Add params for each ? in the sub_query (5 times)
            params.extend([like_term] * 5)
            
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

