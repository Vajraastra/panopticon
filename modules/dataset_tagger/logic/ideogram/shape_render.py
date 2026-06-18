"""
Compositing de FORMAS/MARCOS con Pillow para el captioner Ideogram 4.

Mismo principio que `text_render`: dibuja sobre una COPIA de la imagen de salida
(nunca toca el original, Regla de Oro #9) y devuelve la bbox AUTOMÁTICA de lo
realmente pintado (ground truth: la caja del elemento concuerda con los píxeles).

Cuatro formas, deliberadamente pocas (alcance acotado):
- RECT      : marco rectangular.
- ROUNDED   : marco de esquinas redondeadas (paneles).
- ELLIPSE   : marco ovalado / circular.
- BUBBLE    : globo de diálogo (rect redondeado + cola apuntando al hablante).

Cada forma se compone como un elemento `obj`: el borde define el marco y, si se
pide, un relleno (útil para que el texto de un globo se lea sobre fondo sólido).
"""
import logging

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

# Formas (locale-safe: nunca comparar por texto)
RECT = 0
ROUNDED = 1
ELLIPSE = 2
BUBBLE = 3


def _hex_to_rgba(hex_color, alpha=255):
    s = (hex_color or "#000000").lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    r, g, b = (int(s[i:i + 2], 16) for i in (0, 2, 4))
    return (r, g, b, alpha)


def _norm_rect(rect):
    x0, y0, x1, y1 = rect
    x0, x1 = sorted((int(round(x0)), int(round(x1))))
    y0, y1 = sorted((int(round(y0)), int(round(y1))))
    return x0, y0, x1, y1


def _draw_tail(draw, body, tail, stroke_width, outline, fill):
    """Cola triangular del globo desde el borde inferior del cuerpo hacia `tail`.

    El triángulo se rellena (tapa el borde del cuerpo bajo la cola) y luego se
    repintan sus dos lados como contorno para empatar con el borde del globo.
    """
    x0, y0, x1, y1 = body
    tx, ty = tail
    w = x1 - x0
    # Ancla la base de la cola dentro del borde inferior, cerca de tx.
    base_w = max(w * 0.12, 10)
    bx = min(max(tx, x0 + w * 0.12), x1 - w * 0.12)
    p1 = (bx - base_w / 2, y1)
    p2 = (bx + base_w / 2, y1)
    tip = (tx, ty)
    draw.polygon([p1, p2, tip], fill=fill or (0, 0, 0, 0))
    if outline:
        draw.line([p1, tip], fill=outline, width=stroke_width)
        draw.line([p2, tip], fill=outline, width=stroke_width)


def render_shape(base_image, *, shape=RECT, rect, stroke_width=4,
                 stroke_color="#000000", fill_color=None, fill_alpha=255,
                 radius=24, tail=None):
    """Compone una forma sobre una copia de `base_image` y devuelve (imagen, bbox_px).

    base_image : ruta, PIL.Image o ndarray; se trabaja sobre una COPIA.
    rect       : (x0, y0, x1, y1) área de la forma en píxeles.
    fill_color : HEX de relleno o None (solo contorno).
    radius     : radio de esquina (ROUNDED / BUBBLE).
    tail       : (x, y) punta de la cola (solo BUBBLE); None = abajo-izquierda.
    bbox_px    : [x0, y0, x1, y1] envolvente de lo pintado, o None si quedó vacío.
    """
    if isinstance(base_image, Image.Image):
        base = base_image
    else:
        base = Image.open(base_image)
    orig_mode = base.mode if base.mode in ("RGB", "RGBA") else "RGB"
    base = base.convert("RGBA")

    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    x0, y0, x1, y1 = _norm_rect(rect)
    sw = max(int(stroke_width), 0)
    outline = _hex_to_rgba(stroke_color) if sw > 0 else None
    fill = _hex_to_rgba(fill_color, fill_alpha) if fill_color else None
    rad = max(int(radius), 0)

    if shape == ELLIPSE:
        draw.ellipse([x0, y0, x1, y1], outline=outline, width=sw or 1, fill=fill)
    elif shape == ROUNDED:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=rad, outline=outline,
                               width=sw or 1, fill=fill)
    elif shape == BUBBLE:
        if tail is None:
            tail = (x0 + (x1 - x0) * 0.25, y1 + (y1 - y0) * 0.5)
        draw.rounded_rectangle([x0, y0, x1, y1], radius=rad, outline=outline,
                               width=sw or 1, fill=fill)
        _draw_tail(draw, (x0, y0, x1, y1), tail, sw or 1, outline, fill)
    else:  # RECT
        draw.rectangle([x0, y0, x1, y1], outline=outline, width=sw or 1, fill=fill)

    bbox = layer.getbbox()
    out = Image.alpha_composite(base, layer).convert(orig_mode)
    return out, (list(bbox) if bbox else None)
