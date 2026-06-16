"""
Modelo de datos del captioner Ideogram 4.

Separa el ESTADO DE TRABAJO (reanudable, editable) del ARTEFACTO DE EXPORT
(caption canónico validado):

  <imagen>.pano.json   ← sidecar de trabajo (fuente de verdad del editor)
  <imagen>.json        ← caption canónico generado al exportar (lo consume el trainer)

Decisión de implementación: las bboxes se guardan en el sidecar de trabajo como
ESQUINAS EN PÍXELES ([x0,y0,x1,y1]) junto con las dimensiones de la imagen, no
ya normalizadas. El editor trabaja en píxeles y así puede reabrir/editar sin
pérdida; la normalización a 0–1000 ocurre una sola vez, al exportar.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import schema

WORK_SCHEMA = "panopticon.ideogram4.work/1"

# Estados que controlan cómo el grid pinta cada imagen.
STATUS_DRAFT = "draft"        # bboxes puestas, falta captura VLM
STATUS_CAPTURED = "captured"  # capturado, listo para exportar
STATUS_EXPORTED = "exported"  # caption final escrito


@dataclass
class Element:
    """Un elemento del caption (objeto o texto).

    bbox_px: esquinas en píxeles [x0, y0, x1, y1] sobre la imagen original.
    """
    id: int
    type: str  # "obj" | "text"
    bbox_px: list = field(default_factory=lambda: [0, 0, 0, 0])
    desc: str = ""
    color_palette: list = field(default_factory=list)
    # Solo obj:
    padding: float = 0.08      # margen del crop que recibe el VLM (fracción)
    captured: bool = False     # ya se capturó desc/paleta (para reanudar)
    # Solo text:
    text: str = ""
    render: dict = None        # fuente/efecto si hubo compositing (Caso A)

    def to_dict(self):
        d = {"id": self.id, "type": self.type, "bbox_px": list(self.bbox_px),
             "desc": self.desc, "color_palette": list(self.color_palette)}
        if self.type == "obj":
            d["padding"] = self.padding
            d["captured"] = self.captured
        else:
            d["text"] = self.text
            if self.render:
                d["render"] = self.render
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=d["id"], type=d["type"],
            bbox_px=list(d.get("bbox_px", [0, 0, 0, 0])),
            desc=d.get("desc", ""), color_palette=list(d.get("color_palette", [])),
            padding=d.get("padding", 0.08), captured=d.get("captured", False),
            text=d.get("text", ""), render=d.get("render"),
        )


@dataclass
class StyleSpec:
    """style_description (ruta no-photo / 3D). El captioner siempre usa art_style."""
    aesthetics: str = ""
    lighting: str = ""
    medium: str = "graphic_design"
    art_style: str = ""
    color_palette: list = field(default_factory=list)

    def to_dict(self):
        return {"aesthetics": self.aesthetics, "lighting": self.lighting,
                "medium": self.medium, "art_style": self.art_style,
                "color_palette": list(self.color_palette)}

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(aesthetics=d.get("aesthetics", ""), lighting=d.get("lighting", ""),
                   medium=d.get("medium", "graphic_design"), art_style=d.get("art_style", ""),
                   color_palette=list(d.get("color_palette", [])))


@dataclass
class WorkDoc:
    """Documento de trabajo de una imagen (un .pano.json)."""
    image: str
    image_w: int = 0
    image_h: int = 0
    status: str = STATUS_DRAFT
    high_level_description: str = ""
    background: str = ""
    style: StyleSpec = field(default_factory=StyleSpec)
    elements: list = field(default_factory=list)

    # -- (de)serialización del sidecar de trabajo -----------------------------
    def to_dict(self):
        return {
            "schema": WORK_SCHEMA,
            "image": self.image,
            "image_w": self.image_w,
            "image_h": self.image_h,
            "status": self.status,
            "global": {
                "high_level_description": self.high_level_description,
                "style": self.style.to_dict(),
                "background": self.background,
            },
            "elements": [e.to_dict() for e in self.elements],
        }

    @classmethod
    def from_dict(cls, d):
        g = d.get("global", {})
        return cls(
            image=d.get("image", ""),
            image_w=d.get("image_w", 0), image_h=d.get("image_h", 0),
            status=d.get("status", STATUS_DRAFT),
            high_level_description=g.get("high_level_description", ""),
            background=g.get("background", ""),
            style=StyleSpec.from_dict(g.get("style")),
            elements=[Element.from_dict(e) for e in d.get("elements", [])],
        )

    def save(self, path):
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def next_element_id(self):
        return (max((e.id for e in self.elements), default=0)) + 1

    # -- conversión al caption canónico de export -----------------------------
    def to_canonical(self):
        """Genera el dict del caption canónico (orden estricto, bbox 0–1000).

        Lanza ValueError si faltan dimensiones de imagen (necesarias para
        normalizar las bboxes) o si el estilo es inválido.
        """
        if self.image_w <= 0 or self.image_h <= 0:
            raise ValueError("Faltan dimensiones de imagen para normalizar las bboxes")

        style = schema.build_style(
            aesthetics=self.style.aesthetics, lighting=self.style.lighting,
            medium=self.style.medium, art_style=self.style.art_style,
            color_palette=self.style.color_palette,
        )

        elements = []
        for e in self.elements:
            x0, y0, x1, y1 = e.bbox_px
            bbox = schema.normalize_bbox(x0, y0, x1, y1, self.image_w, self.image_h)
            raw = {"type": e.type, "bbox": bbox, "desc": e.desc,
                   "color_palette": e.color_palette}
            if e.type == "text":
                raw["text"] = e.text
            elements.append(raw)

        return schema.canonical_caption(
            high_level_description=self.high_level_description,
            style=style, background=self.background, elements=elements,
        )
