"""Compatibilidad para scripts anteriores; los módulos importan desde core."""
from core.database_manager import DatabaseManager

__all__ = ['DatabaseManager']
