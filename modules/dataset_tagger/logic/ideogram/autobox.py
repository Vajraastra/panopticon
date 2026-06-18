"""Detección automática de bounding boxes para el captioner Ideogram 4.

Usa YOLOv8 (COCO, 80 clases) para PRE-CREAR cajas que el usuario luego repasa:
ajusta medidas, añade las que falten o borra las innecesarias. No busca certeza
absoluta, sino ahorrar el grueso del trabajo manual.

El esquema canónico de Ideogram solo admite `type` = obj/text (y `text` es una
categoría "sintética" nuestra para componer texto sobre la imagen). En la
práctica solo existe `obj`: personajes, animales y objetos caben todos ahí. Por
eso las detecciones entran como `type="obj"` con `desc` VACÍO: precategorizar
sería redundante —y contraproducente, porque el editor conserva el `desc` que no
esté vacío y bloquearía la descripción rica que el VLM captura al pulsar Iniciar—.
La categoría detectada se usa SOLO internamente para ordenar por saliencia.

El peso de YOLO se descarga solo (ultralytics) a models/yolo/ la primera vez.
"""
import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.paths import CachePaths

log = logging.getLogger(__name__)

# Clases COCO consideradas animales → categoría "animal".
_COCO_ANIMALS = {"bird", "cat", "dog", "horse", "sheep", "cow",
                 "elephant", "bear", "zebra", "giraffe"}

# Prioridad de saliencia para el orden de Ideogram (1 = más saliente):
# el personaje suele ser el sujeto, luego animales, luego objetos.
_PRIORITY = {"character": 0, "animal": 1, "object": 2}

DEFAULT_MODEL = "yolov8m.pt"   # balance precisión/peso (~50 MB), autodescarga
DEFAULT_CONF = 0.35            # umbral de confianza (filtra detecciones flojas)
MAX_BOXES = 20                 # tope de cajas por imagen (el resto sería ruido)


def category_for(class_name):
    """character / animal / object a partir de la clase COCO."""
    if class_name == "person":
        return "character"
    if class_name in _COCO_ANIMALS:
        return "animal"
    return "object"


class AutoBoxer:
    """Envoltura perezosa de YOLOv8 para detectar cajas en una imagen.

    La carga del modelo se difiere a la primera detección (es lenta y no debe
    bloquear el arranque). Reutilizable: una instancia detecta muchas imágenes
    sin recargar el peso.
    """

    def __init__(self, model_name=DEFAULT_MODEL, conf=DEFAULT_CONF, max_boxes=MAX_BOXES):
        self.model_name = model_name
        self.conf = conf
        self.max_boxes = max_boxes
        self._model = None

    def _ensure_model(self):
        if self._model is not None:
            return
        from ultralytics import YOLO
        path = CachePaths.get_models_root() / "yolo" / self.model_name
        path.parent.mkdir(parents=True, exist_ok=True)
        # ultralytics descarga el peso a `path` si no existe (nombre conocido).
        self._model = YOLO(str(path))
        log.info("AutoBoxer: YOLO cargado (%s)", path)

    def detect(self, image_path):
        """Detecta objetos en `image_path`.

        Devuelve una lista de dicts ordenada por saliencia (personaje → animal →
        objeto, y dentro de cada grupo por área descendente), recortada a
        `max_boxes`:
            {"bbox_px": [x0,y0,x1,y1], "category": str, "class": str, "conf": float}
        Lanza si el modelo no puede cargarse o inferir.
        """
        self._ensure_model()
        results = self._model(str(image_path), conf=self.conf, verbose=False)
        dets = []
        for r in results:
            names = r.names
            for b in r.boxes:
                cls = names[int(b.cls[0])]
                x0, y0, x1, y1 = (round(float(v)) for v in b.xyxy[0])
                dets.append({
                    "bbox_px": [x0, y0, x1, y1],
                    "category": category_for(cls),
                    "class": cls,
                    "conf": float(b.conf[0]),
                })

        def area(d):
            x0, y0, x1, y1 = d["bbox_px"]
            return (x1 - x0) * (y1 - y0)

        dets.sort(key=lambda d: (_PRIORITY.get(d["category"], 3), -area(d)))
        return dets[:self.max_boxes]


_SHARED = None


def get_shared_boxer():
    """Instancia compartida en el proceso (carga el modelo una sola vez)."""
    global _SHARED
    if _SHARED is None:
        _SHARED = AutoBoxer()
    return _SHARED


class AutoBoxBatchWorker(QThread):
    """Pre-detecta cajas para TODO un set y guarda un borrador .pano.json por
    imagen, para que el usuario luego repase imagen por imagen.

    Por defecto NO toca imágenes que ya tienen .pano.json (protege el trabajo
    manual previo). Hereda el estilo del panel del set en cada borrador nuevo.
    """

    progress = Signal(int, int, str)   # hechas, total, nombre actual
    finished_ok = Signal(int, int)     # creadas, omitidas
    failed = Signal(str)

    def __init__(self, images, out_dir, style_defaults, skip_existing=True,
                 conf=DEFAULT_CONF, parent=None):
        super().__init__(parent)
        self.images = list(images)
        self.out_dir = Path(out_dir)
        self.style_defaults = dict(style_defaults or {})
        self.skip_existing = skip_existing
        self.conf = conf
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            from PIL import Image
            from .datamodel import WorkDoc, Element, STATUS_DRAFT
            boxer = AutoBoxer(conf=self.conf)
            self.out_dir.mkdir(parents=True, exist_ok=True)
            total = len(self.images)
            created = skipped = 0
            for i, img in enumerate(self.images, 1):
                if self._cancel:
                    break
                img = Path(img)
                self.progress.emit(i, total, img.name)
                pano = self.out_dir / (img.stem + ".pano.json")
                if self.skip_existing and pano.exists():
                    skipped += 1
                    continue
                try:
                    with Image.open(img) as im:
                        w, h = im.size
                except (OSError, ValueError):
                    skipped += 1
                    continue
                doc = WorkDoc(image=img.name, image_w=w, image_h=h, status=STATUS_DRAFT)
                sd = self.style_defaults
                doc.style.art_style = sd.get("art_style", "")
                doc.style.aesthetics = sd.get("aesthetics", "")
                doc.style.lighting = sd.get("lighting", "")
                for d in boxer.detect(str(img)):
                    x0, y0, x1, y1 = d["bbox_px"]
                    x0, x1 = max(0, min(x0, w)), max(0, min(x1, w))
                    y0, y1 = max(0, min(y0, h)), max(0, min(y1, h))
                    # desc vacío: el VLM lo captura al Iniciar (no precategorizar).
                    doc.elements.append(Element(
                        id=doc.next_element_id(), type="obj",
                        bbox_px=[x0, y0, x1, y1]))
                doc.save(pano)
                created += 1
            self.finished_ok.emit(created, skipped)
        except Exception as exc:  # noqa: BLE001 — se reporta a la UI
            self.failed.emit(str(exc))
