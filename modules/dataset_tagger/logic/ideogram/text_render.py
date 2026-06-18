"""
Compositing de texto con Pillow para el captioner Ideogram 4 (Caso A: texto
NUEVO en flyers). Renderiza el texto sobre una COPIA de la imagen de salida —
nunca toca el original (Regla de Oro #9) — y devuelve la bbox AUTOMÁTICA del
texto renderizado (ground truth perfecto: string, color y caja concuerdan con
lo que la imagen realmente muestra).

Por qué manual y no VLM/k-means: el VLM alucinaría el string y el k-means
capturaría el fondo en vez del glifo. Aquí el texto es ground truth.

Efectos autoaplicables:
- STRAIGHT     : texto recto.
- ARC          : arco ascendente (glifos sobre una parábola, rotados tangentes).
- ARC_DOWN     : arco descendente (sonrisa invertida).
- PERSPECTIVE  : trapecio tipo Star Wars crawl (borde superior más angosto).
- WAVE         : onda senoidal (desplazamiento vertical por glifo).
- CIRCLE       : glifos sobre un arco circular (tipo arcoíris).
- ROTATE       : bloque entero inclinado (0..45°).
- VERTICAL     : glifos apilados verticalmente.

La bbox automática se obtiene de `text_layer.getbbox()` (envolvente axis-aligned
del contenido no transparente) — válido para los tres efectos por igual.
"""
import logging
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

FONTS_DIR = Path(__file__).resolve().parent.parent.parent / "presets" / "fonts"

# Efectos (locale-safe: nunca comparar por texto)
STRAIGHT = 0
ARC = 1
PERSPECTIVE = 2
ARC_DOWN = 3      # arco descendente (sonrisa invertida)
WAVE = 4          # onda senoidal
CIRCLE = 5        # texto sobre un arco circular (tipo arcoíris)
ROTATE = 6        # bloque entero inclinado
VERTICAL = 7      # glifos apilados verticalmente


def list_fonts():
    """Lista [(etiqueta, ruta_str)] de las fuentes incluidas en presets/fonts/.

    La etiqueta combina familia y subfamilia ("Poppins Bold") para que dos
    cortes de la misma familia no se vean idénticos en el selector.
    """
    out = []
    for f in sorted(FONTS_DIR.glob("*.ttf")):
        try:
            family, style = ImageFont.truetype(str(f), 16).getname()[:2]
        except OSError:
            family, style = f.stem, ""
        label = family if not style or style.lower() == "regular" else f"{family} {style}"
        out.append((label, str(f)))
    return out


def _hex_to_rgba(hex_color, alpha=255):
    s = (hex_color or "#FFFFFF").lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    r, g, b = (int(s[i:i + 2], 16) for i in (0, 2, 4))
    return (r, g, b, alpha)


def _load_font(font_path, size):
    return ImageFont.truetype(str(font_path), int(size))


def _draw_straight(layer, text, font, xy, fill, align, stroke_w, stroke_fill):
    draw = ImageDraw.Draw(layer)
    draw.text(xy, text, font=font, fill=fill, align=align,
              stroke_width=stroke_w, stroke_fill=stroke_fill)


def _char_tile(ch, font, fill, stroke_w, stroke_fill):
    """Renderiza un carácter en su propia imagen RGBA ajustada."""
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = probe.textbbox((0, 0), ch, font=font, stroke_width=stroke_w)
    w = max(box[2] - box[0], 1) + 2 * stroke_w + 2
    h = max(box[3] - box[1], 1) + 2 * stroke_w + 2
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    d.text((-box[0] + stroke_w + 1, -box[1] + stroke_w + 1), ch, font=font,
           fill=fill, stroke_width=stroke_w, stroke_fill=stroke_fill)
    return tile


def _draw_arc(layer, text, font, xy, fill, align, stroke_w, stroke_fill, bend=0.35):
    """Coloca los glifos sobre una parábola ascendente, cada uno rotado según la
    pendiente local. `bend` (0..1) controla cuánto sube el arco.

    Cada glifo se ancla por su CENTRO a (centro del slot horizontal, curva), lo
    que evita saltos verticales y solapes al rotar tiles de distinto tamaño.
    """
    x0, y0 = xy
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    widths = [max(probe.textlength(ch, font=font), 1) for ch in text]
    total = sum(widths) or 1
    ascent, descent = font.getmetrics()
    half_glyph = (ascent + descent) / 2.0
    height = bend * total / 2.0  # flecha del arco

    cursor = 0.0
    for ch, w in zip(text, widths):
        t = (cursor + w / 2) / total            # 0..1 a lo largo del texto
        dy = -height * (1 - (2 * t - 1) ** 2)   # arco ascendente (y crece hacia abajo)
        slope = height * 4 * (2 * t - 1) / total
        angle = math.degrees(math.atan(slope))
        if ch.strip():
            tile = _char_tile(ch, font, fill, stroke_w, stroke_fill)
            rot = tile.rotate(angle, expand=True, resample=Image.BICUBIC)
            cx = x0 + cursor + w / 2.0           # centro del slot del glifo
            cy = y0 + half_glyph + dy
            layer.alpha_composite(rot, (int(cx - rot.width / 2), int(cy - rot.height / 2)))
        cursor += w


def _draw_wave(layer, text, font, xy, fill, align, stroke_w, stroke_fill, amp=0.4):
    """Desplaza cada glifo verticalmente sobre una senoidal (sin rotar). `amp`
    (0..1) escala la amplitud respecto a la altura del glifo; ~2 ciclos."""
    x0, y0 = xy
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    widths = [max(probe.textlength(ch, font=font), 1) for ch in text]
    total = sum(widths) or 1
    ascent, descent = font.getmetrics()
    half_glyph = (ascent + descent) / 2.0
    amplitude = amp * (ascent + descent) * 0.6
    cycles = 2.0

    cursor = 0.0
    for ch, w in zip(text, widths):
        t = (cursor + w / 2) / total
        dy = amplitude * math.sin(2 * math.pi * cycles * t)
        if ch.strip():
            tile = _char_tile(ch, font, fill, stroke_w, stroke_fill)
            cx = x0 + cursor + w / 2.0
            cy = y0 + half_glyph + dy
            layer.alpha_composite(tile, (int(cx - tile.width / 2), int(cy - tile.height / 2)))
        cursor += w


def _draw_circle(layer, text, font, xy, fill, align, stroke_w, stroke_fill, span=0.5):
    """Coloca los glifos sobre un arco CIRCULAR (tipo arcoíris), cada uno rotado
    radialmente. `span` (0..1) crece el ángulo abarcado (más curvatura)."""
    x0, y0 = xy
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    widths = [max(probe.textlength(ch, font=font), 1) for ch in text]
    total = sum(widths) or 1
    arc_angle = math.pi * (0.25 + span)        # ángulo total abarcado
    radius = total / arc_angle
    cx_center = x0 + total / 2.0
    cy_center = y0 + radius                     # centro del círculo, debajo

    cursor = 0.0
    for ch, w in zip(text, widths):
        frac = (cursor + w / 2) / total
        theta = (frac - 0.5) * arc_angle        # 0 en la cima del arco
        if ch.strip():
            tile = _char_tile(ch, font, fill, stroke_w, stroke_fill)
            rot = tile.rotate(-math.degrees(theta), expand=True, resample=Image.BICUBIC)
            gx = cx_center + radius * math.sin(theta)
            gy = cy_center - radius * math.cos(theta)
            layer.alpha_composite(rot, (int(gx - rot.width / 2), int(gy - rot.height / 2)))
        cursor += w


def _draw_rotate(layer, text, font, xy, fill, align, stroke_w, stroke_fill, amount=0.4):
    """Renderiza el texto recto en un tile y lo inclina como bloque. `amount`
    (0..1) → ángulo 0..45° (antihorario)."""
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = probe.textbbox((0, 0), text, font=font, stroke_width=stroke_w, align=align)
    tw = max(box[2] - box[0], 1) + 2 * stroke_w + 2
    th = max(box[3] - box[1], 1) + 2 * stroke_w + 2
    tile = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text(
        (-box[0] + stroke_w + 1, -box[1] + stroke_w + 1), text, font=font,
        fill=fill, align=align, stroke_width=stroke_w, stroke_fill=stroke_fill)
    rot = tile.rotate(amount * 45.0, expand=True, resample=Image.BICUBIC)
    layer.alpha_composite(rot, (int(xy[0]), int(xy[1])))


def _draw_vertical(layer, text, font, xy, fill, align, stroke_w, stroke_fill):
    """Apila los glifos verticalmente, centrados sobre un eje común."""
    x0, y0 = xy
    ascent, descent = font.getmetrics()
    line_h = int((ascent + descent) * 0.92)
    tiles = [_char_tile(ch, font, fill, stroke_w, stroke_fill) if ch.strip() else None
             for ch in text]
    axis = x0 + max((t.width for t in tiles if t), default=line_h) / 2.0
    cy = y0
    for tile in tiles:
        if tile is not None:
            layer.alpha_composite(tile, (int(axis - tile.width / 2), int(cy)))
        cy += line_h


def _find_coeffs(target, source):
    """Coeficientes PERSPECTIVE: mapea cada punto de `target` (en la salida) al
    correspondiente de `source` (en la entrada)."""
    matrix = []
    for (tx, ty), (sx, sy) in zip(target, source):
        matrix.append([tx, ty, 1, 0, 0, 0, -sx * tx, -sx * ty])
        matrix.append([0, 0, 0, tx, ty, 1, -sy * tx, -sy * ty])
    A = np.array(matrix, dtype=float)
    B = np.array(source, dtype=float).reshape(8)
    return np.linalg.solve(A, B)


def _draw_perspective(layer, text, font, xy, fill, align, stroke_w, stroke_fill, taper=0.4):
    """Renderiza el texto recto en un tile y lo deforma a un trapecio (borde
    superior más angosto). `taper` (0..1) = cuánto se estrecha arriba."""
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = probe.textbbox((0, 0), text, font=font, stroke_width=stroke_w, align=align)
    tw = max(box[2] - box[0], 1) + 2 * stroke_w + 2
    th = max(box[3] - box[1], 1) + 2 * stroke_w + 2
    tile = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text(
        (-box[0] + stroke_w + 1, -box[1] + stroke_w + 1), text, font=font,
        fill=fill, align=align, stroke_width=stroke_w, stroke_fill=stroke_fill)

    inset = taper * tw / 2.0
    target = [(inset, 0), (tw - inset, 0), (tw, th), (0, th)]  # trapecio en salida
    source = [(0, 0), (tw, 0), (tw, th), (0, th)]              # rect del tile
    warped = tile.transform((tw, th), Image.PERSPECTIVE, _find_coeffs(target, source),
                            resample=Image.BICUBIC)
    layer.alpha_composite(warped, (int(xy[0]), int(xy[1])))


def render_text(base_image, *, text, font_path, size, color="#FFFFFF",
                position=(0, 0), effect=STRAIGHT, align="left",
                stroke_width=0, stroke_color="#000000", strength=0.4):
    """Compone `text` sobre una copia de `base_image` y devuelve (imagen, bbox_px).

    base_image : ruta, PIL.Image o ndarray; se trabaja sobre una COPIA.
    position   : (x, y) en píxeles, esquina superior-izquierda del bloque.
    effect     : STRAIGHT | ARC | PERSPECTIVE.
    strength   : intensidad del efecto (bend del arco / taper de la perspectiva).
    bbox_px    : [x0, y0, x1, y1] envolvente del texto, o None si quedó vacío.
    """
    if isinstance(base_image, np.ndarray):
        base = Image.fromarray(base_image)
    elif isinstance(base_image, Image.Image):
        base = base_image
    else:
        base = Image.open(base_image)
    orig_mode = base.mode if base.mode in ("RGB", "RGBA") else "RGB"
    base = base.convert("RGBA")

    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    font = _load_font(font_path, size)
    fill = _hex_to_rgba(color)
    stroke_fill = _hex_to_rgba(stroke_color) if stroke_width else None

    if effect == ARC:
        _draw_arc(layer, text, font, position, fill, align, stroke_width, stroke_fill, strength)
    elif effect == ARC_DOWN:
        _draw_arc(layer, text, font, position, fill, align, stroke_width, stroke_fill, -strength)
    elif effect == PERSPECTIVE:
        _draw_perspective(layer, text, font, position, fill, align, stroke_width, stroke_fill, strength)
    elif effect == WAVE:
        _draw_wave(layer, text, font, position, fill, align, stroke_width, stroke_fill, strength)
    elif effect == CIRCLE:
        _draw_circle(layer, text, font, position, fill, align, stroke_width, stroke_fill, strength)
    elif effect == ROTATE:
        _draw_rotate(layer, text, font, position, fill, align, stroke_width, stroke_fill, strength)
    elif effect == VERTICAL:
        _draw_vertical(layer, text, font, position, fill, align, stroke_width, stroke_fill)
    else:
        _draw_straight(layer, text, font, position, fill, align, stroke_width, stroke_fill)

    bbox = layer.getbbox()  # envolvente del contenido no transparente
    out = Image.alpha_composite(base, layer).convert(orig_mode)
    return out, (list(bbox) if bbox else None)
