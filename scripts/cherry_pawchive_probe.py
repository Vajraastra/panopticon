"""
Consulta las cabeceras de una ruta de Pawchive encolada con el host genérico.

Envía una petición Range con la identidad actual de Cherry-DL. Muestra el
estado, las cabeceras de transferencia y el host de cualquier redirección.
Cierra la respuesta tras las cabeceras, sin seguir el salto ni leer el cuerpo.
El índice y los catálogos se abren en modo de solo lectura.

Uso:  venv/Scripts/python.exe scripts/cherry_pawchive_probe.py
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from third_party.cherry_dl.config import CHERRY_DIR, load_config  # noqa: E402
from third_party.cherry_dl.templates.base import ArtistInfo  # noqa: E402
from third_party.cherry_dl.templates.pawchive import PawchiveTemplate  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def una_url_pendiente() -> str | None:
    """Toma una URL del host canónico de archivos en la cola activa."""
    index = CHERRY_DIR / "index.db"
    if not index.exists():
        return None

    with closing(sqlite3.connect(f"{index.as_uri()}?mode=ro", uri=True)) as con:
        carpetas = [Path(r[0]) for r in con.execute(
            "SELECT DISTINCT folder_path FROM profiles WHERE folder_path IS NOT NULL"
        )]

    for carpeta in carpetas:
        db = carpeta.absolute() / "catalog.db"
        if not db.exists():
            continue
        try:
            with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as cat:
                fila = cat.execute(
                    "SELECT download_url FROM pending_queue "
                    "WHERE download_url LIKE 'https://file.pawchive.pw/data/%' LIMIT 1"
                ).fetchone()
        except sqlite3.Error:
            continue
        if fila:
            return fila[0]
    return None


async def main() -> int:
    try:
        url = una_url_pendiente()
    except sqlite3.Error as exc:
        print(f"No se pudo leer el índice de Cherry-DL ({type(exc).__name__}).")
        return 1
    if not url:
        print("No hay rutas al host genérico de Pawchive en las colas pendientes.")
        return 1

    template = PawchiveTemplate(SimpleNamespace(config=load_config()))
    headers = template.transfer_headers(ArtistInfo("", "", "", template.name))
    headers.update({"Range": "bytes=0-0", "Accept-Encoding": "identity"})
    print("Una petición a file.pawchive.pw con Range: bytes=0-0\n")

    async with httpx.AsyncClient(
        timeout=15.0, follow_redirects=False,
    ) as cli:
        try:
            async with asyncio.timeout(20.0):
                async with cli.stream("GET", url, headers=headers) as response:
                    print(f"  {response.http_version} {response.status_code}")
                    if response.next_request is not None:
                        print(f"  host de destino: {response.next_request.url.host}")
                    for name in ("server", "content-type", "content-length",
                                 "content-range", "accept-ranges", "retry-after"):
                        if name in response.headers:
                            print(f"  {name}: {response.headers[name]}")
        except (httpx.HTTPError, httpx.InvalidURL, TimeoutError) as exc:
            print(f"No se pudieron obtener las cabeceras ({type(exc).__name__}).")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
