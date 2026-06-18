"""Detección automática de bounding boxes para el captioner Ideogram 4.

Usa YOLOv8 (COCO, 80 clases) para PRE-CREAR cajas que el usuario luego repasa:
ajusta medidas, añade las que falten o borra las innecesarias. No busca certeza
absoluta, sino ahorrar el grueso del trabajo manual.

El esquema canónico de Ideogram solo admite `type` = obj/text (sin campo de
categoría), así que la distinción personaje/animal/objeto se precarga en `desc`
—la descripción legible que el VLM refina al pulsar Iniciar y que el usuario ve
en la etiqueta de cada caja—. Todas las detecciones entran como `type="obj"`;
el texto se sigue marcando a mano.

El peso de YOLO se descarga solo (ultralytics) a models/yolo/ la primera vez.
"""
import logging

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


def desc_for(class_name):
    """Descripción inicial precargada: 'character' para personas, si no la clase."""
    return "character" if class_name == "person" else class_name


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
