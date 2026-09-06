"""
Una sola petición a Pawchive, para saber si el bloqueo sigue puesto.

El 2026-09-05 el host de archivos empezó a devolver 403 y el motor, que trataba
cada 403 como problema de un archivo, siguió pidiendo a una petición por segundo
durante trece minutos: 774 rechazos. El freno del motor ya impide que eso se
repita, pero queda una pregunta que solo el servidor puede contestar: **¿sigue
cerrada la puerta, y por qué?**

Este script la hace una vez. Literalmente una:

  - Toma una fila de `pending_queue` de un proyecto que use Pawchive —la más
    pequeña que encuentre— y pide **un solo byte** con `Range: bytes=0-0`.
  - Imprime el código, las cabeceras que importan y **el cuerpo entero** si es
    texto: el 403 traía 143 bytes escritos a mano y nadie los había leído.
  - No escribe nada: ni en el catálogo, ni en el índice, ni en disco.

Si contesta 206 o 200, el bloqueo se levantó. Si contesta 403, no — y el cuerpo
dirá si es la misma queja de antes o algo nuevo.

Uso:  venv/Scripts/python.exe scripts/cherry_pawchive_probe.py
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "third_party"))

import httpx   # noqa: E402

from cherry_dl.config import CHERRY_DIR   # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def una_url_pendiente() -> tuple[str, str] | None:
    """Una URL de file.pawchive.pw de la cola, con su nombre. Solo lectura."""
    index = CHERRY_DIR / "index.db"
    if not index.exists():
        print(f"✗ no está {index}")
        return None

    con = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    carpetas = [Path(r[0]) for r in con.execute(
        "SELECT DISTINCT folder_path FROM profiles WHERE folder_path IS NOT NULL")]
    con.close()

    for carpeta in carpetas:
        db = carpeta / "catalog.db"
        if not db.exists():
            continue
        try:
            cat = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            fila = cat.execute(
                "SELECT download_url, filename_hint FROM pending_queue "
                "WHERE download_url LIKE '%file.pawchive.pw%' LIMIT 1"
            ).fetchone()
            cat.close()
        except sqlite3.Error:
            continue
        if fila:
            print(f"  fuente: {carpeta.name}")
            return fila[0], fila[1] or "?"
    return None


async def main() -> int:
    dato = una_url_pendiente()
    if not dato:
        print("✗ no hay ninguna URL de pawchive en las colas pendientes")
        return 1

    url, nombre = dato
    print(f"  archivo: {nombre}")
    print(f"  URL:     {url[:100]}…\n")
    print("→ UNA petición, un solo byte (Range: bytes=0-0)\n")

    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True,
        headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
    ) as cli:
        try:
            r = await cli.get(url, headers={"Range": "bytes=0-0"})
        except httpx.HTTPError as e:
            print(f"✗ ni siquiera conectó: {type(e).__name__}: {e}")
            return 1

    print(f"  HTTP {r.status_code}")
    for h in ("server", "content-type", "content-length", "content-range",
              "retry-after", "cf-mitigated", "ddg-cache-status"):
        if h in r.headers:
            print(f"  {h}: {r.headers[h]}")

    cuerpo = r.text.strip() if "text" in r.headers.get("content-type", "") else ""
    if cuerpo:
        print(f"\n  el servidor dice:\n  «{cuerpo[:600]}»")

    print()
    if r.status_code in (200, 206):
        print("✓ la puerta está abierta otra vez")
    elif r.status_code == 403:
        print("⛔ sigue bloqueado — no insistir; el cuerpo de arriba es la pista")
    else:
        print(f"? respuesta inesperada ({r.status_code})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
