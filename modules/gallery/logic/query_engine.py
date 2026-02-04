import os
from modules.librarian.logic.db_manager import DatabaseManager

class QueryEngine:
    """
    Decoupled logic for fetching data from the DatabaseManager.
    Translates GalleryState to DB queries.
    """
    def __init__(self):
        self.db = DatabaseManager()
        
    def fetch_albums(self, page=0, page_size=50):
        """
        Returns (total_count, list_of_dicts)
        Each dict has: path, name, count, cover_path
        """
        offset = page * page_size
        return self.db.get_folders_paginated(limit=page_size, offset=offset)
        
    def fetch_images(self, state, page_size=50):
        """
        Returns (total_count, list_of_paths)
        """
        offset = state._page * page_size
        
        # [NEW] Handle Custom Mode (no DB)
        if state.mode == state.VIEW_CUSTOM:
            total = len(state._custom_paths)
            start = offset
            end = min(total, start + page_size)
            # Return list of (path, rating=0)
            # Maybe fetch ratings from DB if paths exist there? For now, just 0.
            return total, [(p, 0) for p in state._custom_paths[start:end]]
            
        # Build Query from State
        folder_filter = state.current_folder
        
        # Construct query string compatible with DB Manager
        query_parts = []
        if folder_filter:
            query_parts.append(f"path:{folder_filter}")
        
        if state.min_rating > 0:
            query_parts.append(f"rating:{state.min_rating}")
            
        full_query = " ".join(query_parts)
        
        count, results = self.db.search_files_paginated(
            query=full_query,
            tags=state.tags,
            search_terms=state.search_terms,
            limit=page_size,
            offset=offset
        )
        
        # Results is list of (path, rating)
        # We assume the UI primarily needs paths, but might cache ratings
        return count, results
        
    def update_rating(self, path, rating):
        """Updates the rating of a single file."""
        return self.db.update_file_rating(path, rating)

    def get_all_tags(self):
        return self.db.get_all_tags()
