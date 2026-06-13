"""
core/ai/model_downloader.py

Utilidad centralizada para descargar modelos ML a disco.

Contratos:
- Descarga a archivo temporal y mueve al destino solo si completa con éxito.
- Nunca deja archivos parciales en la ruta de destino.
- Soporta ZIP con extracción de un miembro específico (e.g., buffalo_l).
- Distingue errores de red (servidor caído) de errores HTTP (archivo no encontrado).
- Logging estandarizado con prefijo [ModelDownloader].
"""
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)


def _classify_request_error(e, url: str, filename: str) -> RuntimeError:
    """
    Convierte una excepción de requests en un RuntimeError con mensaje claro
    que distingue: archivo no encontrado en servidor vs fallo de red/conexión.
    """
    import requests

    if isinstance(e, requests.exceptions.HTTPError):
        code = e.response.status_code if e.response is not None else "?"
        if code == 404:
            return RuntimeError(
                f"[ModelDownloader] Archivo no encontrado en el servidor (HTTP 404).\n"
                f"  Modelo : {filename}\n"
                f"  URL    : {url}\n"
                f"  → La URL puede haber cambiado o el archivo fue eliminado del servidor.\n"
                f"  → Reportar en el proyecto para actualizar la URL."
            )
        return RuntimeError(
            f"[ModelDownloader] El servidor respondió con error HTTP {code}.\n"
            f"  Modelo : {filename}\n"
            f"  URL    : {url}\n"
            f"  → Puede ser un problema temporal. Intenta de nuevo."
        )

    if isinstance(e, requests.exceptions.ConnectionError):
        return RuntimeError(
            f"[ModelDownloader] No se pudo conectar al servidor.\n"
            f"  Modelo : {filename}\n"
            f"  → Verifica tu conexión a internet e intenta de nuevo."
        )

    if isinstance(e, requests.exceptions.Timeout):
        return RuntimeError(
            f"[ModelDownloader] Tiempo de espera agotado descargando {filename}.\n"
            f"  URL    : {url}\n"
            f"  → El servidor puede estar caído temporalmente. Intenta de nuevo."
        )

    return RuntimeError(
        f"[ModelDownloader] Error descargando {filename}: {e}\n"
        f"  URL: {url}"
    )


def _mkstemp_path(**kwargs) -> Path:
    """Crea un archivo temporal seguro y devuelve su Path.
    Reemplaza tempfile.mktemp (deprecado, con race condition)."""
    fd, path = tempfile.mkstemp(**kwargs)
    os.close(fd)
    return Path(path)


def download_file(url: str, dest: Path, timeout: int = 120) -> None:
    """
    Descarga `url` a `dest`.

    Escribe a un archivo temporal en el mismo directorio y lo mueve
    al destino solo tras una descarga exitosa. Si falla, no deja rastro.

    :raises RuntimeError: con mensaje diferenciado según el tipo de fallo
                          (404 / error de servidor / error de red / timeout).
    """
    import requests

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = _mkstemp_path(dir=dest.parent, suffix=".tmp")
    log.info(f"[ModelDownloader] Descargando {dest.name}…")
    try:
        r = requests.get(url, stream=True, timeout=timeout)
        r.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(65_536):
                f.write(chunk)
        shutil.move(str(tmp_path), str(dest))
        log.info(f"[ModelDownloader] {dest.name} → {dest}")
    except requests.exceptions.RequestException as e:
        tmp_path.unlink(missing_ok=True)
        err = _classify_request_error(e, url, dest.name)
        log.error(str(err))
        raise err
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"[ModelDownloader] Error inesperado descargando {dest.name}: {e}") from e


def download_zip_member(url: str, member: str, dest: Path,
                        timeout: int = 300) -> None:
    """
    Descarga un ZIP desde `url` y extrae únicamente `member` a `dest`.

    Útil para paquetes como buffalo_l.zip que contienen un solo modelo.
    El ZIP temporal se elimina siempre, con o sin error.

    :param url:    URL del archivo ZIP.
    :param member: Ruta interna en el ZIP (e.g. 'buffalo_l/w600k_r50.onnx').
    :param dest:   Ruta de destino del archivo extraído.
    :raises RuntimeError: con mensaje diferenciado según el tipo de fallo.
    """
    import requests

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    tmp_zip = _mkstemp_path(suffix=".zip")
    tmp_dest = None
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
                    f"'{member}' no encontrado en el ZIP.\n"
                    f"  Contenido: {names[:10]}\n"
                    f"  → El formato del paquete puede haber cambiado. Reportar para actualizar."
                )
            tmp_dest = _mkstemp_path(dir=dest.parent, suffix=".tmp")
            with zf.open(member) as src, open(tmp_dest, "wb") as dst:
                shutil.copyfileobj(src, dst)

        shutil.move(str(tmp_dest), str(dest))
        log.info(f"[ModelDownloader] {dest.name} extraído → {dest}")

    except requests.exceptions.RequestException as e:
        err = _classify_request_error(e, url, dest.name)
        log.error(str(err))
        raise err
    except FileNotFoundError as e:
        log.error(f"[ModelDownloader] {e}")
        raise RuntimeError(str(e)) from e
    except Exception as e:
        raise RuntimeError(
            f"[ModelDownloader] Error inesperado extrayendo {member}: {e}"
        ) from e
    finally:
        tmp_zip.unlink(missing_ok=True)
        if tmp_dest and Path(tmp_dest).exists():
            Path(tmp_dest).unlink(missing_ok=True)
