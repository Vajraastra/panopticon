import os
import time
from PySide6.QtCore import QThread, Signal
from modules.workshop.logic.parser import UniversalParser

class IndexerWorker(QThread):
    """
    Background worker to scan watched folders and index images into the database.
    Designed for high performance with heavy datasets (10k+ images).
    """
    progress_signal = Signal(str)      # Update status text
    count_signal = Signal(int, int)    # Processed, Total (Total might be unknown initially)
    finished_signal = Signal()
    
    def __init__(self, db_manager, folders, deep_clean=False):
        super().__init__()
        self.db = db_manager
        self.folders = folders
        self.deep_clean = deep_clean
        self.is_running = True
        self.batch_size = 50  # Commit to DB every 50 files to balance speed/safety

    def run(self):
        title = "🚀 Starting Deep Clean..." if self.deep_clean else "🚀 Starting Intelligent Sync..."
        self.progress_signal.emit(title)
        
        extensions = ('.png', '.jpg', '.jpeg', '.webp')
        all_new_files = []
        total_purged = 0

        # --- Phase 1: Folder Sync ---
        for folder in self.folders:
            if not self.is_running: break
            
            self.progress_signal.emit(f"🔍 Checking: {os.path.basename(folder)}")
            
            # 1. Quick Count Heuristic (Skip if not deep_clean)
            disk_files = []
            for root, _, files in os.walk(folder):
                for f in files:
                    if f.lower().endswith(extensions):
                        # Critical: Normalize to / for consistent comparison
                        full_path = os.path.normpath(os.path.join(root, f)).replace('\\', '/')
                        disk_files.append(full_path)
            
            disk_count = len(disk_files)
            db_count = self.db.get_file_count_for_folder(folder)
            
            # Skip if counts match AND we are not forcing a deep clean
            if not self.deep_clean and disk_count == db_count:
                self.progress_signal.emit(f"✅ {os.path.basename(folder)}: Match ({disk_count} files). Skipping.")
                continue

            msg = f"⚠️ {os.path.basename(folder)}: Deep Checking..." if self.deep_clean else f"⚠️ {os.path.basename(folder)}: Mismatch. Syncing..."
            self.progress_signal.emit(msg)

            # 2. Sync Logic (Purge + Add)
            db_paths = self.db.get_files_under_path(folder)
            disk_paths_set = set(disk_files)
            
            # Find orphaned (in DB but not on Disk)
            orphaned = [p for p in db_paths if p not in disk_paths_set]
            if orphaned:
                self.progress_signal.emit(f"🧹 Purging {len(orphaned)} missing files from {os.path.basename(folder)}...")
                self.db.remove_many_files(orphaned)
                total_purged += len(orphaned)

            # Find new (on Disk but not in DB)
            new_in_folder = [p for p in disk_files if p not in db_paths]
            all_new_files.extend(new_in_folder)

        # --- Phase 2: Global Audit (Deep Clean Only) ---
        if self.deep_clean:
            self.progress_signal.emit("🕵️ Running Global Audit (Checking all DB entries)...")
            all_db_paths = self.db.get_all_file_paths()
            global_orphaned = []
            
            count_audit = 0
            total_audit = len(all_db_paths)
            
            for p in all_db_paths:
                if not self.is_running: break
                
                # Check existence (convert back to OS separator for check)
                os_path = os.path.normpath(p) 
                if not os.path.exists(os_path):
                    global_orphaned.append(p)
                
                count_audit += 1
                if count_audit % 1000 == 0:
                     self.progress_signal.emit(f"🕵️ Auditing: {count_audit}/{total_audit}")

            if global_orphaned:
                self.progress_signal.emit(f"🧹 Found {len(global_orphaned)} global orphans. Purging...")
                self.db.remove_many_files(global_orphaned)
                total_purged += len(global_orphaned)
            
            self.progress_signal.emit("📉 Vacuuming database to reclaim space...")
            self.db.vacuum_database()

        # 3. Processing Phase: Parse metadata and insert for new files
        total_new = len(all_new_files)
        if total_new == 0:
            if total_purged > 0:
                self.progress_signal.emit(f"✅ Sync complete. Purged {total_purged} entries.")
            else:
                self.progress_signal.emit("✅ Library is up to date.")
            self.finished_signal.emit()
            return

        self.progress_signal.emit(f"📦 Found {total_new} new files. Processing metadata...")
        
        count = 0
        batch_stats = []
        batch_meta = []
        batch_paths = []
        
        for file_path in all_new_files:
            if not self.is_running: break
            
            try:
                result = UniversalParser.parse_image(file_path)
                batch_paths.append(file_path)
                batch_stats.append(result.get("stats", {}))
                
                meta = {
                    'width': result.get('raw', {}).get('width', 0),
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
            
            if len(batch_paths) >= self.batch_size:
                self.db.add_many_files(batch_paths, batch_stats, batch_meta)
                batch_paths, batch_stats, batch_meta = [], [], []
                
            count += 1
            if count % 10 == 0:
                self.count_signal.emit(count, total_new)
                self.progress_signal.emit(f"⚙ Processing: {os.path.basename(file_path)}")

        if batch_paths:
            self.db.add_many_files(batch_paths, batch_stats, batch_meta)

        self.progress_signal.emit(f"✅ Done! Added: {total_new}, Purged: {total_purged}")
        self.finished_signal.emit()

    def stop(self):
        self.is_running = False
