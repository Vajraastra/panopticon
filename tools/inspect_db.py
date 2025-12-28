import sqlite3
import os

db_path = "panopticon.db"
if not os.path.exists(db_path):
    print("DB not found")
    exit()

conn = sqlite3.connect(db_path)
c = conn.cursor()

print("--- FILES TABLE INFO ---")
c.execute("PRAGMA table_info(files)")
for row in c.fetchall():
    print(row)

print("\n--- TABLES ---")
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
for row in c.fetchall():
    print(row[0])
    
conn.close()
