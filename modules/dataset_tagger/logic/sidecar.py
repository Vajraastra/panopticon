"""
Salida del Dataset Tagger: copia del set a una carpeta nueva y escritura de
sidecars .txt estilo kohya.

Reglas (decididas con el usuario):
- La salida SIEMPRE va a una carpeta nueva; nunca se escribe junto a los
  originales (respeta Regla de Oro #9 — solo Metadata Hub edita el original).
- Nombre: <carpeta>_<modelo>_<formato>  (formato = tags | natural).
- Política ante captions existentes, elegida antes de iniciar:
  skip / overwrite / append.
"""
import logging
import re
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# Conjunto seguro para VLMs (formatos primarios del databank). Sin AVIF/GIF:
# no todos los servidores los decodifican y el databank es PNG/JPG.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

SKIP = "skip"
OVERWRITE = "overwrite"
APPEND = "append"


def sanitize(name):
    """Sanea un identificador para usarlo en ruta (Ollama 'qwen2-vl:7b' -> 'qwen2-vl_7b')."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("_") or "model"


def list_images(folder, recursive=False):
    folder = Path(folder)
    it = folder.rglob("*") if recursive else folder.glob("*")
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def output_dir(source_folder, model_id, fmt):
    """Carpeta de salida hermana: <carpeta>_<modelo>_<fmt>."""
    src = Path(source_folder)
    base = src.name or "dataset"
    return src.parent / f"{base}_{sanitize(model_id)}_{fmt}"


def prepare_output(source_folder, model_id, fmt):
    out = output_dir(source_folder, model_id, fmt)
    out.mkdir(parents=True, exist_ok=True)
    return out


def caption_path(out_dir, image_path, ext=".txt"):
    return Path(out_dir) / (Path(image_path).stem + ext)


def has_sidecar(image_path, ext=".txt"):
    """True si ya existe un .txt hermano de la imagen (caption previo en la carpeta).

    Sirve para que la UI marque visualmente qué imágenes del set ya tienen
    caption (datasets kohya ya etiquetados o salidas previas del tagger).
    """
    return Path(image_path).with_suffix(ext).exists()


def copy_image(src_image, out_dir):
    """Copia la imagen al dataset de salida si aún no está (preserva metadata)."""
    dest = Path(out_dir) / Path(src_image).name
    if not dest.exists():
        shutil.copy2(src_image, dest)
    return dest


def should_process(cap_path, policy):
    """
    Decide si procesar una imagen según la política y si el .txt ya existe.
    Retorna (procesar: bool, modo_escritura: 'w'|'a'|None).
    skip permite reanudar lotes grandes sin re-gastar en el VLM.
    """
    if not Path(cap_path).exists():
        return True, "w"
    if policy == SKIP:
        return False, None
    if policy == APPEND:
        return True, "a"
    return True, "w"  # OVERWRITE (y default seguro)


def write_caption(cap_path, text, mode="w", sep=", "):
    cap_path = Path(cap_path)
    with open(cap_path, mode, encoding="utf-8") as f:
        if mode == "a" and text:
            f.write(sep + text)
        else:
            f.write(text or "")
    return cap_path
