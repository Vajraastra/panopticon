"""
Schema canónico del caption JSON de Ideogram 4: orden estricto de claves,
normalización (hex, bbox, paletas) y validador.

El validador (`CaptionVerifier`) es una adaptación de `caption_verifier.py` del
repo oficial `ideogram-oss/ideogram4` (Apache-2.0). En Ideogram 4 NO existe un
JSON Schema aparte: el verificador ES el contrato. Por eso se porta su lógica
en vez de añadir una dependencia (`jsonschema`) — alineado con la Regla 7.

Confirmado contra el repo (2026-06-15):
- Campo de descripción = "desc" (no "description"); high_level_description = str.
- "background" es un STRING directo dentro de compositional_deconstruction.
- bbox = [ymin, xmin, ymax, xmax], enteros 0–1000, origen arriba-izquierda.
- color_palette: "#RRGGBB" MAYÚSCULAS; ≤16 a nivel estilo, ≤5 por elemento.
- style_description: exactamente uno de "photo" XOR "art_style".
- El orden de claves importa (el text-encoder es sensible a la secuencia).
"""
import json
import re
from pathlib import Path
from typing import Sequence

# --- Orden canónico de claves (NO ordenar alfabéticamente al serializar) -----
TOP_LEVEL_ORDER = (
    "high_level_description",
    "style_description",
    "compositional_deconstruction",
)
STYLE_ORDER_PHOTO = ("aesthetics", "lighting", "photo", "medium", "color_palette")
STYLE_ORDER_NON_PHOTO = ("aesthetics", "lighting", "medium", "art_style", "color_palette")
COMPOSITION_ORDER = ("background", "elements")
ELEMENT_ORDER_OBJ = ("type", "bbox", "desc", "color_palette")
ELEMENT_ORDER_TEXT = ("type", "bbox", "text", "desc", "color_palette")

BBOX_MIN, BBOX_MAX = 0, 1000
STYLE_PALETTE_MAX = 16
ELEMENT_PALETTE_MAX = 5

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


# --- Normalización (funciones puras) -----------------------------------------
def normalize_hex(color):
    """Normaliza un color a '#RRGGBB' MAYÚSCULAS.

    Acepta '#rrggbb', '#rgb' (shorthand → expandido) o una tupla/lista (r,g,b)
    de enteros 0–255. Devuelve None si no es interpretable.
    """
    if isinstance(color, (tuple, list)) and len(color) >= 3:
        try:
            r, g, b = (int(round(c)) for c in color[:3])
        except (TypeError, ValueError):
            return None
        r, g, b = (max(0, min(255, v)) for v in (r, g, b))
        return f"#{r:02X}{g:02X}{b:02X}"
    if isinstance(color, str):
        s = color.strip()
        if re.fullmatch(r"#[0-9A-Fa-f]{3}", s):  # shorthand #abc -> #aabbcc
            s = "#" + "".join(ch * 2 for ch in s[1:])
        if _HEX_RE.match(s):
            return s.upper()
    return None


def clamp_palette(colors, max_colors):
    """Normaliza una lista de colores a hex MAYÚSC, descarta inválidos,
    deduplica preservando orden y recorta a `max_colors`."""
    out, seen = [], set()
    for c in colors or []:
        h = normalize_hex(c)
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out[:max_colors]


def normalize_bbox(x0, y0, x1, y1, img_w, img_h):
    """Convierte una caja en píxeles (esquinas) a [ymin, xmin, ymax, xmax]
    enteros 0–1000. Ordena las esquinas y hace clamp al rango válido."""
    if img_w <= 0 or img_h <= 0:
        raise ValueError("Dimensiones de imagen inválidas")
    xa, xb = sorted((x0, x1))
    ya, yb = sorted((y0, y1))

    def scale(v, size):
        return max(BBOX_MIN, min(BBOX_MAX, int(round(v / size * BBOX_MAX))))

    return [scale(ya, img_h), scale(xa, img_w), scale(yb, img_h), scale(xb, img_w)]


# --- Ensamblado canónico ------------------------------------------------------
def build_style(*, aesthetics="", lighting="", medium="", art_style=None,
                photo=None, color_palette=None):
    """Construye el dict `style_description` en orden canónico.

    Para 3D/ilustración pasa `art_style` (ruta no-photo); para foto pasa `photo`.
    Debe darse exactamente uno de los dos.
    """
    if bool(art_style) == bool(photo):
        raise ValueError("style_description requiere exactamente uno de art_style XOR photo")
    sd = {"aesthetics": aesthetics, "lighting": lighting}
    if photo:
        sd["photo"] = photo
        sd["medium"] = medium
    else:
        sd["medium"] = medium
        sd["art_style"] = art_style
    palette = clamp_palette(color_palette, STYLE_PALETTE_MAX)
    if palette:
        sd["color_palette"] = palette
    return sd


def build_element(element):
    """Construye un elemento (obj|text) en orden canónico desde un dict crudo
    con claves: type, bbox, desc, color_palette y (si text) text."""
    etype = element.get("type")
    if etype not in ("obj", "text"):
        raise ValueError(f"Tipo de elemento inválido: {etype!r}")
    out = {"type": etype, "bbox": list(element["bbox"])}
    if etype == "text":
        out["text"] = element.get("text", "")
    out["desc"] = element.get("desc", "")
    palette = clamp_palette(element.get("color_palette"), ELEMENT_PALETTE_MAX)
    if palette:
        out["color_palette"] = palette
    return out


def canonical_caption(*, high_level_description="", style, background, elements):
    """Ensambla el caption final en orden canónico estricto.

    `style` es el dict ya construido por `build_style`; `elements` una lista de
    dicts crudos. Los elementos `text` se empujan al final, conservando el orden
    relativo de cada grupo. dict preserva orden de inserción (Python 3.7+).
    """
    cap = {}
    if high_level_description:
        cap["high_level_description"] = high_level_description
    cap["style_description"] = style
    objs = [e for e in elements if e.get("type") == "obj"]
    texts = [e for e in elements if e.get("type") == "text"]
    cap["compositional_deconstruction"] = {
        "background": background or "",
        "elements": [build_element(e) for e in (objs + texts)],
    }
    return cap


def dumps(caption):
    """Serializa a JSON con UTF-8 literal y SIN reordenar claves (crítico)."""
    return json.dumps(caption, ensure_ascii=False, sort_keys=False, indent=2)


# --- Validador (portado de ideogram-oss/ideogram4, Apache-2.0) ---------------
class CaptionVerifier:
    """Verifica el formato del caption de Ideogram 4.

    Cada `verify_*` devuelve una lista de warnings (vacía = pasó todo). No lanza
    excepciones: emite advertencias para que la UI las muestre.
    """

    _NON_ASCII_ESCAPE_RE = re.compile(
        r"\\u(?:00[89a-fA-F][0-9a-fA-F]|0[1-9a-fA-F][0-9a-fA-F]{2}|[1-9a-fA-F][0-9a-fA-F]{3})"
    )

    top_level_known_keys = frozenset(TOP_LEVEL_ORDER)
    style_known_keys = frozenset(
        {"aesthetics", "lighting", "photo", "art_style", "medium", "color_palette"}
    )
    element_known_keys = frozenset({"type", "bbox", "text", "desc", "color_palette"})
    element_types = frozenset({"obj", "text"})

    def verify(self, caption):
        warnings = []
        if not isinstance(caption, dict):
            return [f"root: se esperaba un objeto JSON (dict), se obtuvo {type(caption).__name__}"]

        self._check_unknown_keys(caption, self.top_level_known_keys, "root", warnings)

        if "high_level_description" in caption and not isinstance(
            caption["high_level_description"], str
        ):
            warnings.append("high_level_description: se esperaba un string")

        if "style_description" in caption:
            self._verify_style(caption["style_description"], warnings)

        if "compositional_deconstruction" in caption:
            self._verify_composition(caption["compositional_deconstruction"], warnings)
        else:
            warnings.append("root: falta 'compositional_deconstruction' (obligatorio)")

        return warnings

    def verify_raw(self, raw_text):
        warnings = self._check_ensure_ascii_false(raw_text)
        try:
            caption = json.loads(raw_text)
        except json.JSONDecodeError as e:
            warnings.append(f"JSON inválido: {e}")
            return warnings
        return warnings + self.verify(caption)

    def verify_file(self, path):
        return self.verify_raw(Path(path).read_text(encoding="utf-8"))

    # -- internos -------------------------------------------------------------
    def _verify_style(self, sd, warnings):
        if not isinstance(sd, dict):
            warnings.append("style_description: se esperaba un dict")
            return
        self._check_unknown_keys(sd, self.style_known_keys, "style_description", warnings)
        has_photo, has_art = "photo" in sd, "art_style" in sd
        if has_photo and has_art:
            warnings.append("style_description: contiene 'photo' y 'art_style'; se espera exactamente uno")
            return
        if not has_photo and not has_art:
            warnings.append("style_description: falta uno de 'photo' (foto) o 'art_style' (no-foto)")
            return
        order = STYLE_ORDER_PHOTO if has_photo else STYLE_ORDER_NON_PHOTO
        self._check_key_order(sd, [k for k in order if k != "color_palette" or "color_palette" in sd],
                              "style_description", warnings)
        if "color_palette" in sd:
            self._verify_palette(sd["color_palette"], "style_description.color_palette",
                                 STYLE_PALETTE_MAX, warnings)

    def _verify_composition(self, cd, warnings):
        if not isinstance(cd, dict):
            warnings.append("compositional_deconstruction: se esperaba un dict")
            return
        if "background" not in cd:
            warnings.append("compositional_deconstruction: falta 'background'")
            return
        if not isinstance(cd["background"], str):
            warnings.append(
                f"compositional_deconstruction.background: se esperaba un string, "
                f"se obtuvo {type(cd['background']).__name__}"
            )
            return
        if "elements" not in cd:
            warnings.append("compositional_deconstruction: falta 'elements'")
            return
        self._check_key_order(cd, COMPOSITION_ORDER, "compositional_deconstruction", warnings)
        elements = cd["elements"]
        if not isinstance(elements, list):
            warnings.append("compositional_deconstruction.elements: se esperaba una lista")
            return
        for i, elem in enumerate(elements):
            self._verify_element(i, elem, warnings)

    def _verify_element(self, i, elem, warnings):
        if not isinstance(elem, dict):
            warnings.append(f"elements[{i}]: se esperaba un dict")
            return
        self._check_unknown_keys(elem, self.element_known_keys, f"elements[{i}]", warnings)
        if "type" not in elem:
            warnings.append(f"elements[{i}]: falta 'type'")
            return
        if elem.get("type") not in self.element_types:
            warnings.append(f"elements[{i}]: 'type' debe ser uno de {set(self.element_types)}")
            return
        order = ELEMENT_ORDER_TEXT if elem["type"] == "text" else ELEMENT_ORDER_OBJ
        present_order = [k for k in order if k in ("type", "desc", "text") or k in elem]
        self._check_key_order(elem, present_order, f"elements[{i}]", warnings)
        if "bbox" in elem:
            self._verify_bbox(i, elem["bbox"], warnings)
        if "color_palette" in elem:
            self._verify_palette(elem["color_palette"], f"elements[{i}].color_palette",
                                 ELEMENT_PALETTE_MAX, warnings)

    def _verify_palette(self, palette, path, max_colors, warnings):
        if not isinstance(palette, list):
            warnings.append(f"{path}: se esperaba una lista")
            return
        if len(palette) > max_colors:
            warnings.append(f"{path}: demasiados colores ({len(palette)}), máximo {max_colors}")
            return
        for i, color in enumerate(palette):
            if (not isinstance(color, str) or len(color) != 7 or color[0] != "#"
                    or not all(c in "0123456789ABCDEF" for c in color[1:])):
                warnings.append(f"{path}[{i}]: '{color}' no es un color #RRGGBB válido")

    def _verify_bbox(self, i, bbox, warnings):
        if not isinstance(bbox, list) or len(bbox) != 4:
            warnings.append(f"elements[{i}].bbox: se esperaba [ymin, xmin, ymax, xmax]")
            return
        if not all(isinstance(v, int) for v in bbox):
            warnings.append(f"elements[{i}].bbox: todos los valores deben ser int")
            return
        ymin, xmin, ymax, xmax = bbox
        if not all(BBOX_MIN <= v <= BBOX_MAX for v in bbox):
            warnings.append(f"elements[{i}].bbox: valores fuera de [{BBOX_MIN}, {BBOX_MAX}]: {bbox}")
        if ymin > ymax:
            warnings.append(f"elements[{i}].bbox: ymin ({ymin}) > ymax ({ymax})")
        if xmin > xmax:
            warnings.append(f"elements[{i}].bbox: xmin ({xmin}) > xmax ({xmax})")

    def _check_ensure_ascii_false(self, raw_text, max_examples=3):
        matches = self._NON_ASCII_ESCAPE_RE.findall(raw_text)
        if not matches or any(ord(c) > 0x7F for c in raw_text):
            return []
        examples = ", ".join(sorted(set(matches))[:max_examples])
        return [
            f"texto crudo: hay {len(matches)} escape(s) unicode no-ASCII (p.ej. {examples}) "
            f"y ningún carácter no-ASCII literal. Suele indicar guardado con "
            f"ensure_ascii=True; re-guarda con json.dump(..., ensure_ascii=False)."
        ]

    @staticmethod
    def _check_key_order(obj, expected_order, path, warnings):
        present = tuple(k for k in obj if k in expected_order)
        if present != tuple(expected_order):
            warnings.append(f"{path}: orden de claves {present}, se esperaba {tuple(expected_order)}")
        extra = [k for k in obj if k not in expected_order]
        if extra:
            warnings.append(f"{path}: claves {extra} no permitidas en este contexto")

    @staticmethod
    def _check_unknown_keys(obj, known, path, warnings):
        unknown = [k for k in obj if k not in known]
        if unknown:
            warnings.append(f"{path}: claves desconocidas {unknown} (fuera del schema)")


def verify(caption):
    """Atajo: valida un caption ya parseado. Devuelve lista de warnings."""
    return CaptionVerifier().verify(caption)
