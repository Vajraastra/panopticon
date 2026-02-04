from PySide6.QtCore import QObject, Signal

class GalleryState(QObject):
    """
    Holds the transient state of the Gallery:
    - Current View Mode (Albums vs Images)
    - Active Search Filters (Tags, Strings, Rating)
    - Current Page & Selection
    """
    # Signals
    view_mode_changed = Signal(str)      # "albums" | "images" | "custom"
    filter_changed = Signal()            # Any filter update
    page_changed = Signal(int)           # Page number update
    selection_changed = Signal()         # Picker selection update
    
    # Constants
    VIEW_ALBUMS = "albums"
    VIEW_IMAGES = "images"
    VIEW_CUSTOM = "custom"
    
    def __init__(self):
        super().__init__()
        
        # Navigation State
        self._mode = self.VIEW_ALBUMS
        self._current_folder = None
        self._custom_paths = [] # source for VIEW_CUSTOM
        self._window_title = "Gallery"
        
        # Pagination
        self._page = 0
        self._page_size = 50
        self._total_items = 0
        
        # Filters
        self.tags = []
        self.search_terms = []
        self.min_rating = 0
        
        # Selection (Picker Mode)
        self.picker_active = False
        self.selected_paths = set()
        
    @property
    def mode(self): return self._mode
    
    @property
    def current_folder(self): return self._current_folder
    
    def set_mode(self, mode, folder=None, custom_paths=None, title=None):
        self._mode = mode
        self._current_folder = folder
        if custom_paths is not None:
            self._custom_paths = custom_paths
        if title:
            self._window_title = title
            
        self._page = 0 # Reset page on mode switch
        self.view_mode_changed.emit(mode)
        
    def set_page(self, page):
        self._page = page
        self.page_changed.emit(page)
        
    def set_total_items(self, count):
        self._total_items = count
        
    # --- Filters ---
    
    def add_tag(self, tag):
        if tag not in self.tags:
            self.tags.append(tag)
            self._page = 0
            self.filter_changed.emit()
            
    def remove_tag(self, tag):
        if tag in self.tags:
            self.tags.remove(tag)
            self._page = 0
            self.filter_changed.emit()
            
    def add_term(self, term):
        if term not in self.search_terms:
            self.search_terms.append(term)
            self._page = 0
            self.filter_changed.emit()
            
    def remove_term(self, term):
        if term in self.search_terms:
            self.search_terms.remove(term)
            self._page = 0
            self.filter_changed.emit()
            
    def set_rating_filter(self, rating):
        if self._mode == self.VIEW_ALBUMS: return # No rating filter on albums
        self.min_rating = rating
        self._page = 0
        self.filter_changed.emit()
        
    def clear_filters(self):
        self.tags = []
        self.search_terms = []
        self.min_rating = 0
        self._page = 0
        self.filter_changed.emit()
        
    # --- Selection ---
    
    def toggle_picker_mode(self, active):
        self.picker_active = active
        if not active:
            self.selected_paths.clear()
        self.selection_changed.emit()
        
    def toggle_selection(self, path):
        if path in self.selected_paths:
            self.selected_paths.remove(path)
        else:
            self.selected_paths.add(path)
        self.selection_changed.emit()
        
    def clear_selection(self):
        self.selected_paths.clear()
        self.selection_changed.emit()
