"""Convención de rutas de salida del modo Ideogram v4.

La carpeta de salida (anidada DENTRO del dataset, ver `sidecar.output_dir`)
queda así:

    <dataset>/<dataset>_ideogram4_json/
        <imagen>.json        ← caption canónico final (el producto)
        <imagen>.<ext>        ← copia de la imagen (con texto compuesto si lo hubo)
        drafts/
            <imagen>.pano.json  ← borrador de trabajo reanudable (WorkDoc)

Los borradores `.pano.json` viven en una SUBCARPETA VISIBLE `drafts/` para no
contaminar la carpeta de salida con archivos de trabajo, pero quedando a la
vista del usuario (decisión de David: subcarpeta visible, NO oculta).

Centralizar la convención aquí evita que cada sitio (editor, autobox, worker,
grid) la reconstruya a mano y se desincronicen.
"""
from pathlib import Path

DRAFTS_SUBDIR = "drafts"


def drafts_dir(out_dir):
    """Subcarpeta de borradores dentro de la carpeta de salida ig4."""
    return Path(out_dir) / DRAFTS_SUBDIR


def pano_path(out_dir, image):
    """Ruta del borrador `.pano.json` de `image` dentro de `drafts/`."""
    return drafts_dir(out_dir) / (Path(image).stem + ".pano.json")


def json_path(out_dir, image):
    """Ruta del caption canónico final `<imagen>.json` (en la raíz de salida)."""
    return Path(out_dir) / (Path(image).stem + ".json")


def out_dir_from_pano(pano):
    """Carpeta de salida real a partir de un `.pano.json` (drafts/ → su padre)."""
    return Path(pano).parent.parent
