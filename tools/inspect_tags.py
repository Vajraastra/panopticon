import sqlite3
import os

db_path = "panopticon.db"
conn = sqlite3.connect(db_path)
c = conn.cursor()

print("--- TAGS TABLE ---")
c.execute("PRAGMA table_info(tags)")
for row in c.fetchall(): print(row)

print("\n--- FILE_TAGS TABLE ---")
c.execute("PRAGMA table_info(file_tags)")
for row in c.fetchall(): print(row)

conn.close()
