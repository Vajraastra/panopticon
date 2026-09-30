import os
from core.database_manager import DatabaseManager

class QueryEngine:
    """
    Decoupled logic for fetching data from the DatabaseManager.
    Translates GalleryState to DB queries.
    """
    def __init__(self, service=None):
        self.db = DatabaseManager(service=service)
        
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
            
        count, results = self.db.search_files_paginated(**self.image_query(state, page_size))
        
        # Results is list of (path, rating)
        # We assume the UI primarily needs paths, but might cache ratings
        return count, results

    def image_query(self, state, page_size):
        # Capturar sólo datos en GUI; ningún worker lee un QObject mutable.
        return dict(folder=state.current_folder, min_rating=state.min_rating,
                    tags=tuple(state.tags), search_terms=tuple(state.search_terms),
                    limit=page_size, offset=state._page * page_size)
        
    def update_rating(self, path, rating):
        """Updates the rating of a single file."""
        return self.db.update_file_rating(path, rating)

    def get_all_tags(self):
        return self.db.get_all_tags()
