"""
Sistema de Paths Centralizado para Panopticon.
Gestiona la carpeta de cache central donde todas las herramientas
depositan sus outputs.
"""
import os
import sys
import subprocess
from pathlib import Path
from typing import Optional


class CachePaths:
    """
    Gestiona paths de cache centralizados para todas las herramientas.
    
    Estructura:
        PANOPTICON_CACHE/
        ├── watermarked/
        ├── optimized/
        ├── cropped/
        ├── scored/
        ├── converted/
        ├── failed/
        └── temp/
    """
    
    _cache_root: Optional[Path] = None
    
    # Nombres estándar de subcarpetas por herramienta
    TOOL_FOLDERS = {
        "watermarker": "watermarked",
        "optimizer": "optimized",
        "cropper": "cropped",
        "quality_scorer": "scored",
        "face_scorer": "face_sorted",
        "format_converter": "converted",
    }
    
    @classmethod
    def set_cache_root(cls, path: str | Path) -> None:
        """
        Configura la carpeta raíz del cache.
        Llamar desde main.py al inicio de la aplicación.
        """
        cls._cache_root = Path(path)
        cls._cache_root.mkdir(parents=True, exist_ok=True)
    
    @classmethod
    def get_cache_root(cls) -> Path:
        """
        Retorna la carpeta cache central.
        Si no está configurada, usa una por defecto junto al ejecutable.
        """
        if cls._cache_root is None:
            # Default: carpeta 'cache' junto al script principal
            if getattr(sys, 'frozen', False):
                # Si está empaquetado como exe
                base = Path(sys.executable).parent
            else:
                # En desarrollo
                base = Path(__file__).parent.parent
            
            cls._cache_root = base / "cache"
            cls._cache_root.mkdir(parents=True, exist_ok=True)
        
        return cls._cache_root
    
    @classmethod
    def get_tool_cache(cls, tool_name: str) -> Path:
        """
        Retorna la subcarpeta de cache para una herramienta específica.
        Crea la carpeta si no existe.
        
        Args:
            tool_name: Nombre de la herramienta (ej: "watermarker", "optimizer")
        
        Returns:
            Path a la subcarpeta del tool
        """
        folder_name = cls.TOOL_FOLDERS.get(tool_name, tool_name)
        tool_path = cls.get_cache_root() / folder_name
        tool_path.mkdir(parents=True, exist_ok=True)
        return tool_path
    
    @classmethod
    def get_temp_folder(cls) -> Path:
        """Retorna carpeta para operaciones temporales."""
        temp = cls.get_cache_root() / "temp"
        temp.mkdir(parents=True, exist_ok=True)
        return temp
    
    @classmethod
    def get_failed_folder(cls) -> Path:
        """Retorna carpeta para archivos que fallaron verificación."""
        failed = cls.get_cache_root() / "failed"
        failed.mkdir(parents=True, exist_ok=True)
        return failed
    
    @classmethod
    def open_folder(cls, path: str | Path) -> bool:
        """
        Abre una carpeta en el explorador de archivos del sistema.
        
        Args:
            path: Ruta a la carpeta a abrir
        
        Returns:
            True si se abrió exitosamente
        """
        path = Path(path)
        if not path.exists():
            return False
        
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=True)
            else:
                subprocess.run(["xdg-open", str(path)], check=True)
            return True
        except Exception as e:
            print(f"[CachePaths] Error opening folder: {e}")
            return False
    
    @classmethod
    def get_output_path(cls, tool_name: str, filename: str, 
                        subfolder: Optional[str] = None) -> Path:
        """
        Genera un path de output completo para un archivo.
        
        Args:
            tool_name: Nombre de la herramienta
            filename: Nombre del archivo de salida
            subfolder: Subcarpeta opcional dentro del tool cache
        
        Returns:
            Path completo al archivo de output
        """
        base = cls.get_tool_cache(tool_name)
        if subfolder:
            base = base / subfolder
            base.mkdir(parents=True, exist_ok=True)
        return base / filename
    
    @classmethod
    def clean_temp(cls) -> int:
        """
        Limpia la carpeta temporal.
        
        Returns:
            Número de archivos eliminados
        """
        temp = cls.get_temp_folder()
        count = 0
        for item in temp.iterdir():
            try:
                if item.is_file():
                    item.unlink()
                    count += 1
                elif item.is_dir():
                    import shutil
                    shutil.rmtree(item)
                    count += 1
            except Exception:
                pass
        return count
