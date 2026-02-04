import sqlite3
import numpy as np
import os
import json
from core.paths import ProjectPaths

class ProfileDB:
    def __init__(self):
        self.db_path = os.path.join(ProjectPaths.app_data(), "character_profiles.db")
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                embedding BLOB NOT NULL,
                samples_count INTEGER DEFAULT 1
            )
        ''')
        conn.commit()
        conn.close()

    def add_reference(self, name, embedding):
        """Adds a new character or updates existing one by averaging embeddings."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Serialize embedding
        emb_bytes = embedding.tobytes()
        
        try:
            # Check if exists to update (Simple average for now)
            cursor.execute("SELECT embedding, samples_count FROM characters WHERE name = ?", (name,))
            row = cursor.fetchone()
            
            if row:
                existing_emb = np.frombuffer(row[0], dtype=np.float32)
                count = row[1]
                
                # Update running average
                new_emb = (existing_emb * count + embedding) / (count + 1)
                
                cursor.execute("UPDATE characters SET embedding = ?, samples_count = ? WHERE name = ?", 
                               (new_emb.tobytes(), count + 1, name))
            else:
                cursor.execute("INSERT INTO characters (name, embedding) VALUES (?, ?)", 
                               (name, emb_bytes))
                               
            conn.commit()
        except Exception as e:
            print(f"ProfileDB Error: {e}")
        finally:
            conn.close()

    def get_all_profiles(self):
        """Returns list of (name, embedding) tuples."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name, embedding FROM characters")
        rows = cursor.fetchall()
        conn.close()
        
        results = []
        for name, blob in rows:
            emb = np.frombuffer(blob, dtype=np.float32)
            results.append((name, emb))
        return results
