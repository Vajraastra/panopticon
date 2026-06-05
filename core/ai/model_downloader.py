"""
core/ai/model_downloader.py

Utilidad centralizada para descargar modelos ML a disco.

Contratos:
- Descarga a archivo temporal y mueve al destino solo si completa con éxito.
- Nunca deja archivos parciales en la ruta de destino.
- Soporta ZIP con extracción de un miembro específico (e.g., buffalo_l).
- Logging estandarizado con prefijo [ModelDownloader].
"""
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)


def download_file(url: str, dest: Path, timeout: int = 120) -> None:
    """
    Descarga `url` a `dest`.

    Escribe a un archivo temporal en el mismo directorio y lo mueve
    al destino solo tras una descarga exitosa. Si falla, no deja rastro.

    :raises RuntimeError: si la descarga o el servidor fallan.
    """
    import requests

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = Path(tempfile.mktemp(dir=dest.parent, suffix=".tmp"))
    log.info(f"[ModelDownloader] Descargando {dest.name}…")
    try:
        r = requests.get(url, stream=True, timeout=timeout)
        r.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(65_536):
                f.write(chunk)
        shutil.move(str(tmp_path), str(dest))
        log.info(f"[ModelDownloader] {dest.name} → {dest}")
    except Exception as e:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Error descargando {url}: {e}") from e


def download_zip_member(url: str, member: str, dest: Path,
                         timeout: int = 300) -> None:
    """
    Descarga un ZIP desde `url` y extrae únicamente `member` a `dest`.

    Útil para paquetes como buffalo_l.zip que contienen un solo modelo.
    El ZIP temporal se elimina siempre, con o sin error.

    :param url:    URL del archivo ZIP.
    :param member: Ruta interna en el ZIP (e.g. 'buffalo_l/w600k_r50.onnx').
    :param dest:   Ruta de destino del archivo extraído.
    :raises RuntimeError: si la descarga, extracción o miembro fallan.
    """
    import requests

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    tmp_zip = Path(tempfile.mktemp(suffix=".zip"))
    log.info(f"[ModelDownloader] Descargando ZIP para {dest.name}…")
    try:
        r = requests.get(url, stream=True, timeout=timeout)
        r.raise_for_status()
        with open(tmp_zip, "wb") as f:
            for chunk in r.iter_content(65_536):
                f.write(chunk)

        log.info(f"[ModelDownloader] Extrayendo '{member}'…")
        with zipfile.ZipFile(tmp_zip) as zf:
            names = zf.namelist()
            if member not in names:
                raise FileNotFoundError(
                    f"'{member}' no encontrado en el ZIP. "
                    f"Contenido: {names[:10]}"
                )
            tmp_dest = Path(tempfile.mktemp(dir=dest.parent, suffix=".tmp"))
            with zf.open(member) as src, open(tmp_dest, "wb") as dst:
                shutil.copyfileobj(src, dst)

        shutil.move(str(tmp_dest), str(dest))
        log.info(f"[ModelDownloader] {dest.name} extraído → {dest}")

    except Exception as e:
        if dest.exists() and dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
        raise RuntimeError(f"Error extrayendo {member} de {url}: {e}") from e
    finally:
        tmp_zip.unlink(missing_ok=True)
