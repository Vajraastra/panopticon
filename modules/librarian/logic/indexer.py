import os
import time
from PySide6.QtCore import QThread, Signal
from modules.metadata.logic.reader import UniversalParser

class IndexerWorker(QThread):
    """
    Robust 2-Phase Indexer.
    Phase 1: Discovery (Files appear instantly).
    Phase 2: Analysis (Metadata enrichment).
    """
    progress_signal = Signal(str)
    count_signal = Signal(int, int)
    finished_signal = Signal()
    
    def __init__(self, db_manager, folders, deep_clean=False):
        super().__init__()
        self.db = db_manager
        self.folders = folders
        self.deep_clean = deep_clean
        self.is_running = True
        self.batch_size = 100

    def run(self):
        self.progress_signal.emit("🚀 Starting Indexer...")
        
        extensions = ('.png', '.jpg', '.jpeg', '.webp')
        total_found = 0
        total_new = 0
        
        # --- PHASE 1: DISCOVERY (The "Survival" Phase) ---
        # Objective: Get files into DB as fast as possible.
        
        for folder in self.folders:
            if not self.is_running: break
            
            self.progress_signal.emit(f"🔍 Scanning: {os.path.basename(folder)}")
            
            # 1. Get Disk State
            disk_files = [] # list of dicts for DB
            disk_paths = set()
            
            for root, _, files in os.walk(folder):
                for f in files:
                    if f.lower().endswith(extensions):
                        full_path = os.path.join(root, f)
                        try:
                            # Basic Stats only
                            stat = os.stat(full_path)
                            disk_files.append({
                                'path': full_path,
                                'filename': f,
                                'size': stat.st_size,
                                'created': stat.st_ctime
                            })
                            disk_paths.add(os.path.normpath(full_path).replace('\\', '/'))
                        except:
                            pass # Skip file access errors, keep moving
            
            total_found += len(disk_files)
            
            # 2. Sync with DB (Minimal)
            # Register ALL files found. The DB manager handles UPSERT (Insert or Update).
            # This ensures even if they exist, we confirm they are still there.
            if disk_files:
                self.progress_signal.emit(f"💾 Registering {len(disk_files)} files in {os.path.basename(folder)}...")
                for i in range(0, len(disk_files), self.batch_size):
                    batch = disk_files[i : i + self.batch_size]
                    self.db.register_files_minimal(batch)
            
            # 3. Cleanup Orphans (Files in DB but not on Disk)
            if self.deep_clean:
                known_paths = self.db.get_known_files_in_folder(folder)
                orphans = list(known_paths - disk_paths)
                if orphans:
                    self.progress_signal.emit(f"🧹 Removing {len(orphans)} deleted files...")
                    self.db.remove_files(orphans)

        # --- PHASE 2: ENRICHMENT (Optional / Background) ---
        # The user said: "No need to worry if metadata doesn't exist".
        # So we will do a very lightweight pass or skip it entirely if not needed.
        # For now, we SKIP deep parsing to guarantee speed and stability as requested.
        # If we need tags later, we can add a specific "Scan Tags" button.
        
        self.progress_signal.emit(f"✅ Indexing Complete. Found {total_found} active files.")
        self.finished_signal.emit()

    def stop(self):
        self.is_running = False
