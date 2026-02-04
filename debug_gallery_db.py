
import sys
import os
sys.path.append(os.getcwd())

from modules.gallery.logic.query_engine import QueryEngine
from modules.gallery.logic.state import GalleryState

try:
    print("Initializing Engine...")
    engine = QueryEngine()
    state = GalleryState()
    
    print("Fetching Albums...")
    # Mocking DatabaseManager internal conn check
    cursor = engine.db.conn.cursor()
    cursor.execute("SELECT count(*) FROM watched_folders")
    print(f"Raw Watched Folders count: {cursor.fetchone()[0]}")
    
    count, albums = engine.fetch_albums(page=0, page_size=10)
    print(f"Engine fetch_albums result: Count={count}, Items={len(albums)}")
    
    if len(albums) > 0:
        print(f"Sample Album: {albums[0]}")
        
except Exception as e:
    import traceback
    traceback.print_exc()
