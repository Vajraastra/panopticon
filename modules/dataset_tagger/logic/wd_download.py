"""
Descarga del WD tagger (modelo ONNX + su CSV casado) desde HuggingFace.

Los dos archivos viven juntos en `models/wd_tagger/<tagger_key>/` para que el
CSV nunca se separe del .onnx con el que está alineado.

La descarga es SÍNCRONA y bloqueante (toca la red): debe invocarse desde un
QThread, nunca desde el hilo GUI (Regla de Oro #4). Usa `huggingface_hub`, que
ya forma parte del stack — no añade dependencias.
"""
import logging
from pathlib import Path

from core.paths import CachePaths

log = logging.getLogger(__name__)


def tagger_dir(tagger_key):
    """Carpeta local donde se guardan el .onnx y el .csv de un tagger."""
    d = CachePaths.get_models_root() / "wd_tagger" / tagger_key
    d.mkdir(parents=True, exist_ok=True)
    return d


def local_paths(preset):
    """(model_path, csv_path) esperados en disco para este preset (existan o no)."""
    d = tagger_dir(preset.key)
    return d / preset.model_file, d / preset.csv_file


def is_downloaded(preset):
    """True si el modelo y su CSV ya están en disco."""
    model_path, csv_path = local_paths(preset)
    return model_path.exists() and csv_path.exists()


def download(preset, progress_cb=None):
    """
    Descarga `model_file` y `csv_file` del repo HF del preset a su carpeta local.

    :param progress_cb: callable(str) opcional para reportar el archivo en curso
                        (la UI lo muestra; hf no expone % fácilmente).
    :returns: (model_path, csv_path)
    :raises RuntimeError: con mensaje claro si la descarga falla.
    """
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import HfHubHTTPError

    d = tagger_dir(preset.key)
    out = {}
    for kind, fname in (("modelo", preset.model_file), ("tags", preset.csv_file)):
        if progress_cb:
            progress_cb(fname)
        try:
            path = hf_hub_download(
                repo_id=preset.hf_repo,
                filename=fname,
                local_dir=str(d),
            )
        except HfHubHTTPError as e:
            raise RuntimeError(
                f"No se pudo descargar el {kind} '{fname}' de {preset.hf_repo} "
                f"(error HTTP: {e}). La URL pudo cambiar o no hay conexión."
            ) from e
        except Exception as e:  # noqa: BLE001 — superficie de error para la UI
            raise RuntimeError(
                f"Error inesperado descargando '{fname}' de {preset.hf_repo}: {e}"
            ) from e
        out[fname] = Path(path)
        log.info("[WDTagger] %s descargado -> %s", fname, path)

    return out[preset.model_file], out[preset.csv_file]
