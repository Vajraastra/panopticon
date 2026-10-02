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
        
        # Consultar sólo la página visible, nunca todo el conjunto externo.
        if state.mode == state.VIEW_CUSTOM:
            if self.has_custom_filters(state):
                return self.db.get_external_files_page(**self.custom_query(state, page_size))
            total = len(state._custom_paths)
            start = offset
            end = min(total, start + page_size)
            paths = state._custom_paths[start:end]
            snapshot = self.db.get_curation_snapshot(paths)
            return total, [(path, row['rating'] or 0) for path, row in zip(paths, snapshot)]
            
        count, results = self.db.search_files_paginated(**self.image_query(state, page_size))
        
        # Results is list of (path, rating)
        # We assume the UI primarily needs paths, but might cache ratings
        return count, results

    @staticmethod
    def has_custom_filters(state):
        return bool(state.tags or state.search_terms or state.min_rating)

    @staticmethod
    def custom_query(state, page_size):
        return dict(paths=state._custom_paths, tags=tuple(state.tags),
                    search_terms=tuple(state.search_terms), min_rating=state.min_rating,
                    limit=page_size, offset=state._page * page_size)

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
