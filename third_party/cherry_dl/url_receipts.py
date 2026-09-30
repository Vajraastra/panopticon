"""Recibos de URLs duplicadas: historial portable, independiente de presencia física."""
from pathlib import Path
import time

import aiosqlite

CREATE = '''CREATE TABLE IF NOT EXISTS downloaded_url_receipts (
    url_source TEXT PRIMARY KEY,
    file_hash TEXT NOT NULL,
    date_added INTEGER NOT NULL,
    FOREIGN KEY(file_hash) REFERENCES files(hash) ON DELETE CASCADE
)'''


async def write_receipt(db, url_source, file_hash):
    """Dentro de la transacción del llamador; nunca inventa un hash ni cambia otro."""
    if not url_source or not file_hash:
        return False
    async with db.execute('SELECT f.url_source,EXISTS(SELECT 1 FROM files WHERE '
        "url_source=? AND hash<>? AND is_preview=0 AND content_role IS NOT 'archive_member'),"
        '(SELECT file_hash FROM downloaded_url_receipts WHERE url_source=?),f.content_role '
        'FROM files f WHERE f.hash=? AND f.is_preview=0',
        (url_source, file_hash, url_source, file_hash)) as cur:
        observed = await cur.fetchone()
    if observed is None:
        return False
    if observed[1] or (observed[2] is not None and observed[2] != file_hash):
        raise ValueError('download_url_history_conflict')
    # La URL principal ya es un recibo; no duplicar almacenamiento ni escribir no-ops.
    if (observed[0] == url_source and observed[3] != 'archive_member') or observed[2] == file_hash:
        return True
    await db.execute('INSERT INTO downloaded_url_receipts VALUES(?,?,?)',
                     (url_source, file_hash, int(time.time())))
    return True


async def remember_downloaded_url(folder, url_source, file_hash):
    """Confirma el recibo antes de descartar el duplicado o retirar su pendiente."""
    async with aiosqlite.connect(Path(folder) / 'catalog.db', timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        try:
            result = await write_receipt(db, url_source, file_hash)
            await db.commit()
            return result
        except BaseException:
            await db.rollback()
            raise


async def merge_download_history(source, destination):
    """Tras migración física exitosa: conserva todos los recibos, incluso sin bytes.

Sólo escribe el catálogo destino. Un conflicto revierte el historial completo y
el caller conserva el perfil origen. Filas ausentes usan el marcador ya existente
_purged_<hash>, sin inventar un archivo, consumir contadores ni copiar metadata nativa.
"""
    source_db = (Path(source) / 'catalog.db').resolve(strict=True)
    destination_db = (Path(destination) / 'catalog.db').resolve(strict=True)
    if source_db == destination_db or source_db.samefile(destination_db):
        raise ValueError('download_history_source_alias')
    async with aiosqlite.connect(source_db.as_uri() + '?mode=ro', uri=True, timeout=30) as src, \
            aiosqlite.connect(destination_db, timeout=30) as dst:
        await src.execute('BEGIN')
        await dst.execute('BEGIN IMMEDIATE')
        try:
            # Inventario histórico completo, por bloques; no consultar exists() de imágenes.
            async with src.execute('SELECT hash,url_source,date_added,file_size,content_service,post_id,'
                                   'content_role,content_position FROM files WHERE is_preview=0') as cur:
                while rows := await cur.fetchmany(256):
                    for digest, url, date, size, service, post, role, position in rows:
                        await dst.execute('INSERT INTO files(hash,filename,url_source,date_added,file_size,'
                            'counter,is_preview,content_service,post_id,content_role,content_position) '
                            'VALUES(?,?,?,?,?,NULL,0,?,?,?,?) ON CONFLICT(hash) DO NOTHING',
                            (digest, '_purged_' + digest, url, date, size, service, post, role, position))
                        # Un hash final de origen convierte sólo la marca de preview, no su ruta.
                        await dst.execute('UPDATE files SET is_preview=0 WHERE hash=? AND is_preview=1', (digest,))
                        if role != 'archive_member':
                            await write_receipt(dst, url, digest)
            async with src.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
                tables = {row[0] for row in await cur.fetchall()}
            if 'downloaded_url_receipts' in tables:
                async with src.execute('SELECT r.url_source,r.file_hash FROM downloaded_url_receipts r '
                                       'JOIN files f ON f.hash=r.file_hash WHERE f.is_preview=0') as cur:
                    while rows := await cur.fetchmany(256):
                        for url, digest in rows:
                            await write_receipt(dst, url, digest)
            if 'content_aliases' in tables:
                async with src.execute('SELECT a.service,a.post_id,a.original_filename,a.content_role,'
                    'a.content_position,a.remote_size,a.file_hash,a.date_added FROM content_aliases a '
                    'JOIN files f ON f.hash=a.file_hash WHERE f.is_preview=0') as cur:
                    while rows := await cur.fetchmany(256):
                        for row in rows:
                            async with dst.execute('SELECT file_hash FROM content_aliases WHERE service=? '
                                'AND post_id=? AND original_filename=? AND content_position=?',
                                (row[0], row[1], row[2], row[4])) as existing:
                                saved = await existing.fetchone()
                            if saved and saved[0] != row[6]:
                                raise ValueError('download_content_history_conflict')
                            await dst.execute('INSERT INTO content_aliases VALUES(?,?,?,?,?,?,?,?) '
                                'ON CONFLICT(service,post_id,original_filename,content_position) DO NOTHING', row)
            await dst.commit()
        except BaseException:
            await dst.rollback()
            raise
        finally:
            await src.rollback()
