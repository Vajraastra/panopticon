import os
import time
from PySide6.QtCore import QThread, Signal
from modules.metadata_reader.logic.parser import UniversalParser

class IndexerWorker(QThread):
    """
    Background worker to scan watched folders and index images into the database.
    Designed for high performance with heavy datasets (10k+ images).
    """
    progress_signal = Signal(str)      # Update status text
    count_signal = Signal(int, int)    # Processed, Total (Total might be unknown initially)
    finished_signal = Signal()
    
    def __init__(self, db_manager, folders):
        super().__init__()
        self.db = db_manager
        self.folders = folders
        self.is_running = True
        self.batch_size = 50  # Commit to DB every 50 files to balance speed/safety

    def run(self):
        self.progress_signal.emit("🚀 Starting scan...")
        
        # 1. Verification Phase: Quickly scan files to finding new ones
        # For huge datasets, we don't want to parse everything every time.
        # We'll get a set of existing paths from DB first for O(1) lookups.
        existing_paths = self.db.get_all_file_paths()
        self.progress_signal.emit(f"📋 Loaded {len(existing_paths)} existing file records.")
        
        new_files = []
        extensions = ('.png', '.jpg', '.jpeg', '.webp')
        
        for folder in self.folders:
            if not self.is_running: break
            
            self.progress_signal.emit(f"🔍 Scanning: {folder}")
            for root, _, files in os.walk(folder):
                if not self.is_running: break
                
                for f in files:
                    if f.lower().endswith(extensions):
                        full_path = os.path.join(root, f)
                        # Normalize path separators for consistency
                        full_path = os.path.normpath(full_path)
                        
                        if full_path not in existing_paths:
                            new_files.append(full_path)
                            
        total_new = len(new_files)
        if total_new == 0:
            self.progress_signal.emit("✅ Library is up to date.")
            self.finished_signal.emit()
            return

        self.progress_signal.emit(f"📦 Found {total_new} new files. processing...")
        
        # 2. Processing Phase: Parse metadata and insert
        count = 0
        batch_stats = []
        batch_meta = []
        batch_paths = []
        
        for i, file_path in enumerate(new_files):
            if not self.is_running: break
            
            # Parse Metadata (Heavy operation)
            try:
                # We interpret the file just like Module 1
                result = UniversalParser.parse_image(file_path)
                
                batch_paths.append(file_path)
                batch_stats.append(result.get("stats", {}))
                
                # Strip raw for DB to save space (we have specific columns)
                # If you want to keep raw string, we can
                meta = {
                    'width': result.get('raw', {}).get('width', 0), # Fallback if not in stats
                    'height': result.get('raw', {}).get('height', 0),
                    'tool': result.get('tool', 'Unknown'),
                    'model': result.get('model', 'Unknown'),
                    'positive': result.get('positive', ''),
                    'negative': result.get('negative', ''),
                    'seed': result.get('seed', '')
                }
                batch_meta.append(meta)
                
            except Exception as e:
                print(f"Error parsing {file_path}: {e}")
            
            # Batch Commit
            if len(batch_paths) >= self.batch_size:
                self.db.add_many_files(batch_paths, batch_stats, batch_meta)
                batch_paths, batch_stats, batch_meta = [], [], []
                
            count += 1
            if count % 10 == 0: # Update UI every 10 files
                self.count_signal.emit(count, total_new)
                self.progress_signal.emit(f"⚙ Processing: {os.path.basename(file_path)}")

        # Final Batch
        if batch_paths:
            self.db.add_many_files(batch_paths, batch_stats, batch_meta)

        self.progress_signal.emit("✅ Indexing complete!")
        self.finished_signal.emit()

    def stop(self):
        self.is_running = False
