import os
import time
from PySide6.QtCore import QThread, Signal
from modules.metadata.logic.reader import UniversalParser

class IndexerWorker(QThread):
    """
    Hilo de trabajo (Worker) para el indexado robusto de archivos.
    Fase 1: Descubrimiento (Los archivos aparecen instantáneamente en la DB).
    Fase 2: Limpieza de huérfanos (Elimina registros de archivos borrados en disco).
    """
    progress_signal = Signal(str) # Notifica mensajes de estado a la UI
    count_signal = Signal(int, int) # Notifica el progreso numérico (actual, total)
    finished_signal = Signal() # Notifica la finalización del hilo
    
    def __init__(self, db_manager, folders, deep_clean=False):
        super().__init__()
        self.db = db_manager
        self.folders = folders
        self.deep_clean = deep_clean
        self.is_running = True
        self.batch_size = 100

    def run(self):
        """Ejecuta el proceso de escaneo recursivo en segundo plano."""
        self.progress_signal.emit("🚀 Iniciando el indexador...")
        
        extensions = ('.png', '.jpg', '.jpeg', '.webp')
        total_found = 0
        total_new = 0 # Reservado para uso futuro
        
        # --- FASE 1: DESCUBRIMIENTO ---
        # Objetivo: Registrar archivos en la DB lo más rápido posible.
        
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
            
            # --- FASE 2: LIMPIEZA DE HUÉRFANOS ---
            # Borra de la DB los archivos que ya no existen físicamente.
            if self.deep_clean:
                known_paths = self.db.get_known_files_in_folder(folder)
                orphans = list(known_paths - disk_paths)
                if orphans:
                    self.progress_signal.emit(f"🧹 Eliminando {len(orphans)} archivos inexistentes...")
                    self.db.remove_files(orphans)

        self.progress_signal.emit(f"✅ Indexado completo. {total_found} archivos activos.")
        self.finished_signal.emit()

    def stop(self):
        self.is_running = False
