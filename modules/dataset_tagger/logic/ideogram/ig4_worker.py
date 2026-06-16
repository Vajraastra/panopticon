"""
Worker de captura del captioner Ideogram 4 (QThread).

Por cada sidecar de trabajo `.pano.json` (con sus cajas ya dibujadas), captura
lo que es PER-IMAGEN y ensambla el caption canónico:

  a) high_level_description  ← VLM (imagen completa)         [si está vacío]
  b) style.color_palette     ← k-means GLOBAL (≤16)
  c) background (string)      ← VLM ("describe solo el fondo")[si está vacío]
  d) cada elemento OBJ:
       crop(bbox + padding) → VLM desc [si vacío] + k-means paleta (≤5)
  e) cada elemento TEXT       = ground truth (string/color/bbox del compositing);
                                NUNCA pasa por VLM ni k-means.
  f) ensambla en orden canónico → valida → escribe <imagen>.json

Decisión: aesthetics / lighting / art_style son MANUALES (estilo 3D compartido
del set, no se infieren por imagen). El worker no los toca; solo exige que
art_style exista al exportar (si no, reporta error para esa imagen).

Aislamiento del VLM = CROP (+padding), no máscara: la relación entre objetos
vive en high_level_description / background, no en el desc del elemento.

Reanudable: con recapture=False solo se rellenan campos vacíos / elementos no
capturados; así no se re-gasta el VLM en lotes grandes.
"""
import logging
import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PIL import Image

from . import schema, palette
from .datamodel import WorkDoc, STATUS_EXPORTED
from .. import sidecar
from ..providers.base_provider import ProviderError

log = logging.getLogger(__name__)

GLOBAL_PALETTE_K = 8     # clusters para la paleta global (recorte final ≤16)
OBJ_PALETTE_K = 5        # clusters por crop de objeto (≤5)


class Ig4Worker(QThread):
    progress = Signal(int, int, str)     # done, total, filename
    image_done = Signal(str, str)        # image_path, json_path exportado
    warnings = Signal(str, list)         # image_path, lista de warnings del validador
    error = Signal(str, str)             # image_path, mensaje
    finished_all = Signal(int, int)      # exportadas, saltadas

    def __init__(self, provider, model, jobs, *, prompt_hld, prompt_background,
                 prompt_obj, default_padding=0.08, recapture=False, retries=2,
                 parent=None):
        super().__init__(parent)
        self.provider = provider
        self.model = model
        # jobs: lista de (image_path_original, pano_path). El out_dir es
        # pano_path.parent; la imagen de referencia es la copia de salida si
        # existe (texto compuesto), si no el original.
        self.jobs = list(jobs)
        self.prompt_hld = prompt_hld
        self.prompt_background = prompt_background
        self.prompt_obj = prompt_obj
        self.default_padding = default_padding
        self.recapture = recapture
        self.retries = retries
        self._cancel = False

    def cancel(self):
        self._cancel = True

    # ── ciclo principal ──────────────────────────────────────────────────
    def run(self):
        total = len(self.jobs)
        exported = skipped = 0
        for i, (image_path, pano_path) in enumerate(self.jobs, 1):
            if self._cancel:
                break
            name = Path(image_path).name
            self.progress.emit(i, total, name)
            try:
                ok = self._process_one(Path(image_path), Path(pano_path))
            except Exception as exc:  # noqa: BLE001 — una imagen no debe tumbar el lote
                log.exception("ig4: fallo en %s", name)
                self.error.emit(str(image_path), str(exc))
                ok = False
            exported += int(ok)
            skipped += int(not ok)
        self.finished_all.emit(exported, skipped)

    def _process_one(self, image_path, pano_path):
        if not pano_path.exists():
            self.error.emit(str(image_path), "falta el .pano.json")
            return False
        doc = WorkDoc.load(pano_path)
        out_dir = pano_path.parent

        # Imagen de referencia: la copia de salida (con texto si se compuso),
        # si no el original. De ella salen crops y paletas.
        out_img = out_dir / image_path.name
        ref_path = out_img if out_img.exists() else image_path
        ref = Image.open(ref_path).convert("RGB")
        if not doc.image_w or not doc.image_h:
            doc.image_w, doc.image_h = ref.width, ref.height

        # a) high_level_description
        if self.recapture or not doc.high_level_description:
            cap = self._caption_with_retry(str(ref_path), self.prompt_hld)
            if cap is not None:
                doc.high_level_description = cap.strip()

        # b) paleta global
        if self.recapture or not doc.style.color_palette:
            doc.style.color_palette = palette.extract_palette(
                ref, k=GLOBAL_PALETTE_K, max_colors=schema.STYLE_PALETTE_MAX)

        # c) background
        if self.recapture or not doc.background:
            cap = self._caption_with_retry(str(ref_path), self.prompt_background)
            if cap is not None:
                doc.background = cap.strip()

        # d) elementos OBJ (crop → VLM + k-means)
        for elem in doc.elements:
            if self._cancel:
                return False
            if elem.type != "obj":
                continue  # TEXT = ground truth, intacto
            if elem.captured and not self.recapture and elem.desc and elem.color_palette:
                continue
            crop = self._crop(ref, elem.bbox_px, elem.padding or self.default_padding)
            if self.recapture or not elem.desc:
                desc = self._caption_crop(crop, self.prompt_obj, str(image_path))
                if desc is not None:
                    elem.desc = desc.strip()
            if self.recapture or not elem.color_palette:
                elem.color_palette = palette.extract_palette(crop, k=OBJ_PALETTE_K,
                                                             max_colors=schema.ELEMENT_PALETTE_MAX)
            elem.captured = True

        # f) ensamblar + validar + exportar
        try:
            caption = doc.to_canonical()
        except ValueError as exc:
            self.error.emit(str(image_path),
                            f"no se pudo ensamblar (¿falta art_style?): {exc}")
            doc.save(pano_path)  # conserva lo capturado para reanudar
            return False

        warns = schema.verify(caption)
        if warns:
            self.warnings.emit(str(image_path), warns)

        json_path = out_dir / (image_path.stem + ".json")
        json_path.write_text(schema.dumps(caption), encoding="utf-8")
        # Si no hubo compositing, asegura que la imagen acompañe al caption.
        if not out_img.exists():
            sidecar.copy_image(image_path, out_dir)
        doc.status = STATUS_EXPORTED
        doc.save(pano_path)
        self.image_done.emit(str(image_path), str(json_path))
        return True

    # ── helpers ──────────────────────────────────────────────────────────
    def _crop(self, ref, bbox_px, padding):
        x0, y0, x1, y1 = bbox_px
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        pad = padding * max(x1 - x0, y1 - y0)
        cx0 = max(0, int(x0 - pad))
        cy0 = max(0, int(y0 - pad))
        cx1 = min(ref.width, int(x1 + pad))
        cy1 = min(ref.height, int(y1 + pad))
        if cx1 <= cx0 or cy1 <= cy0:
            return ref.crop((0, 0, ref.width, ref.height))
        return ref.crop((cx0, cy0, cx1, cy1))

    def _caption_crop(self, crop, prompt, image_path):
        """Guarda el crop a un temporal y lo pasa al VLM (que recibe un PATH)."""
        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="ig4_crop_")
        os.close(fd)
        try:
            crop.save(tmp)
            return self._caption_with_retry(tmp, prompt)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _caption_with_retry(self, img_path, prompt):
        last = None
        for _ in range(self.retries + 1):
            if self._cancel:
                return None
            try:
                return self.provider.caption(img_path, prompt, model=self.model)
            except (ProviderError, Exception) as exc:  # noqa: BLE001
                last = str(exc)
        self.error.emit(img_path, last or "fallo desconocido del VLM")
        return None
