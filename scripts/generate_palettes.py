#!/usr/bin/env python
"""Genera y audita las paletas de `core/theme_manager.py`.

Las paletas no se eligen a ojo: se derivan de un puñado de parámetros por tema y se
validan contra WCAG antes de existir. Los diez temas anteriores fallaban los MISMOS
siete pares de contraste, que es la señal de que el problema era el método y no un
tema concreto: las superficies no se separaban entre sí (`bg_panel` sobre `bg_main`
daba 1.15:1, imperceptible), los bordes eran invisibles (1.2-1.6:1) y `text_dim` no
llegaba a AA en ninguno.

El método, en tres reglas:

1. **Superficies por luminancia, no por tono.** Cuatro niveles con una luminancia
   objetivo cada uno (input hundido < sidebar < main < panel elevado). Cerca del negro
   la fórmula de WCAG se aplasta —el +0.05 del denominador domina—, así que los saltos
   entre superficies contiguas son modestos (1.10-1.42) y el trabajo de delimitar lo
   terminan los bordes.
2. **Dos niveles de borde.** `border` separa superficies; `border_strong` dibuja lo que
   se puede accionar o escribir, con >=3:1 contra su fondo. Un control pintado con
   `border` se pierde: es lo que le pasaba a las casillas.
3. **Croma bajo en lo grande, tono repartido en lo pequeño.** Las superficies llevan
   poco croma (una superficie amplia muy saturada cansa la vista en sesiones largas) y
   los cinco acentos se reparten por el círculo cromático con >=38 grados entre sí, para
   que ninguno se confunda con otro ni con el tono de identidad del tema.

Uso:
    venv/Scripts/python.exe scripts/generate_palettes.py           # audita y reporta
    venv/Scripts/python.exe scripts/generate_palettes.py --print   # vuelca el bloque THEMES

No escribe en `theme_manager.py`: el volcado se pega a mano, para que un retoque
posterior hecho a mano no se pierda en la siguiente corrida.
"""
from __future__ import annotations

import colorsys
import sys
from pathlib import Path

# ── Escalas ───────────────────────────────────────────────────────────────────
# Luminancia relativa (WCAG) objetivo de cada rol. Ver regla 1 del docstring.
DARK_SURF = {"bg_input": .0035, "bg_sidebar": .0068, "bg_main": .0125, "bg_panel": .0260}
LIGHT_SURF = {"bg_input": .960, "bg_sidebar": .870, "bg_main": .780, "bg_panel": 1.00}

DARK_FG = {"text_primary": .720, "text_secondary": .490, "text_dim": .300,
           "border": .056, "border_strong": .140}
LIGHT_FG = {"text_primary": .035, "text_secondary": .085, "text_dim": .130,
            "border": .469, "border_strong": .227}

# En claro el acento se oscurece hasta cumplir AA contra el blanco; en oscuro no se
# fija la luminancia (ver `build`), porque ahí un acento más claro contrasta más.
LIGHT_ACC = .130

# Tono de identidad y croma de cada tema. `s_bg` bajo a propósito: ver regla 3.
SPEC = {
    "cyberpunk": dict(hue=168, s_bg=.15, s_fg=.10, s_acc=.62, dark=True),
    "midnight":  dict(hue=214, s_bg=.24, s_fg=.20, s_acc=.66, dark=True),
    "forest":    dict(hue=140, s_bg=.20, s_fg=.15, s_acc=.58, dark=True),
    "slate":     dict(hue=220, s_bg=.07, s_fg=.05, s_acc=.34, dark=True),
    "light":     dict(hue=214, s_bg=.10, s_fg=.16, s_acc=.72, dark=False),
    "sepia":     dict(hue=36,  s_bg=.22, s_fg=.24, s_acc=.58, dark=True),
    "cosmic":    dict(hue=272, s_bg=.26, s_fg=.24, s_acc=.64, dark=True),
    "grape":     dict(hue=285, s_bg=.24, s_fg=.22, s_acc=.66, dark=True),
    "ocean":     dict(hue=196, s_bg=.25, s_fg=.22, s_acc=.66, dark=True),
    "aurora":    dict(hue=176, s_bg=.22, s_fg=.18, s_acc=.60, dark=True),
}

# Tono canónico de cada acento semántico. Se respeta salvo que choque con otro ya
# colocado, en cuyo caso se aparta lo mínimo (ver `build`).
SEMANTIC = {"accent_danger": 5, "accent_warning": 38, "accent_success": 152,
            "accent_info": 212}
SEP = 38  # grados mínimos entre dos acentos cualesquiera

ORDER = ["bg_main", "bg_sidebar", "bg_panel", "bg_input",
         "border", "border_strong", "border_highlight",
         "text_primary", "text_secondary", "text_dim",
         "accent_main", "accent_hover", "accent_info",
         "accent_success", "accent_warning", "accent_danger"]

GROUPS = [ORDER[0:4], ORDER[4:7], ORDER[7:10], ORDER[10:13], ORDER[13:16]]


# ── Color ─────────────────────────────────────────────────────────────────────
def _hex(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def luminance(h: str) -> float:
    """Luminancia relativa WCAG."""
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = map(lin, _rgb(h))
    return .2126 * r + .7152 * g + .0722 * b


def ratio(a: str, b: str) -> float:
    """Razón de contraste WCAG entre dos colores."""
    l1, l2 = luminance(a), luminance(b)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + .05) / (l2 + .05)


def at_luminance(hue: float, sat: float, target: float) -> str:
    """Color de tono y croma dados cuya luminancia relativa es `target`.

    Por bisección sobre la claridad HSL: no hay inversa cerrada de la fórmula WCAG.
    """
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if luminance(_hex(*colorsys.hls_to_rgb(hue / 360, mid, sat))) < target:
            lo = mid
        else:
            hi = mid
    return _hex(*colorsys.hls_to_rgb(hue / 360, (lo + hi) / 2, sat))


def build(spec: dict) -> dict[str, str]:
    """Deriva la paleta completa de un tema a partir de su especificación."""
    dark = spec["dark"]
    h, s_bg, s_fg, s_acc = spec["hue"], spec["s_bg"], spec["s_fg"], spec["s_acc"]
    p: dict[str, str] = {}

    for key, y in (DARK_SURF if dark else LIGHT_SURF).items():
        p[key] = at_luminance(h, s_bg, y)
    for key, y in (DARK_FG if dark else LIGHT_FG).items():
        p[key] = at_luminance(h, s_fg if key.startswith("text") else s_bg * 1.25, y)

    def accent(hue: float, sat: float, light: float) -> str:
        """Sobre fondo oscuro un acento MÁS claro contrasta más, así que ahí la
        luminancia es un mínimo y no un objetivo: se fija la claridad HSL y solo se
        sube si no alcanza AA contra la superficie más clara. Fijarla como objetivo
        apagaba la identidad del tema sin ganar legibilidad."""
        if not dark:
            return at_luminance(hue, sat, LIGHT_ACC)
        c = _hex(*colorsys.hls_to_rgb(hue / 360, light, sat))
        while light < 0.95 and ratio(c, p["bg_panel"]) < 4.6:
            light += 0.01
            c = _hex(*colorsys.hls_to_rgb(hue / 360, light, sat))
        return c

    p["accent_main"] = accent(h, s_acc, 0.66)
    p["accent_hover"] = accent(h, s_acc, 0.78 if dark else 0.42)
    p["border_highlight"] = p["accent_main"]

    # Los semánticos se reparten EN CONJUNTO: cada uno arranca en su tono canónico y se
    # desplaza lo mínimo para quedar a >=SEP grados de todos los ya colocados (el acento
    # del tema incluido). Resolverlos de a uno los amontonaba entre sí al esquivar el
    # principal — en Midnight `info` y `success` acababan a 8 grados, indistinguibles.
    taken = [h]
    for key, canonical in SEMANTIC.items():
        for delta in range(0, 91):
            for cand in ((canonical + delta) % 360, (canonical - delta) % 360):
                if all(min((cand - t) % 360, (t - cand) % 360) >= SEP for t in taken):
                    break
            else:
                continue
            break
        taken.append(cand)
        p[key] = accent(cand, min(max(s_acc, 0.52), 0.62), 0.66)

    return {k: p[k] for k in ORDER}


# ── Auditoría ─────────────────────────────────────────────────────────────────
# (primer plano, fondo, razón mínima). 4.5 = AA para texto; 3.0 = AA para controles.
# Los pares de superficie llevan umbrales bajos a propósito: ver regla 1.
CHECKS = [
    ("text_primary", "bg_panel", 7.0), ("text_primary", "bg_main", 7.0),
    ("text_primary", "bg_sidebar", 7.0),
    ("text_secondary", "bg_panel", 4.5), ("text_secondary", "bg_main", 4.5),
    ("text_secondary", "bg_sidebar", 4.5),
    ("text_dim", "bg_panel", 4.5), ("text_dim", "bg_main", 4.5),
    ("text_dim", "bg_sidebar", 4.5),
    ("accent_main", "bg_panel", 4.5), ("accent_main", "bg_main", 4.5),
    ("accent_hover", "bg_panel", 3.0),
    ("accent_info", "bg_panel", 4.5), ("accent_success", "bg_panel", 4.5),
    ("accent_warning", "bg_panel", 4.5), ("accent_danger", "bg_panel", 4.5),
    ("border", "bg_main", 1.6), ("border", "bg_panel", 1.30), ("border", "bg_sidebar", 1.5),
    ("border_strong", "bg_main", 3.0), ("border_strong", "bg_panel", 2.40),
    ("border_strong", "bg_input", 3.0),
    ("bg_panel", "bg_main", 1.20), ("bg_main", "bg_input", 1.15),
    ("bg_main", "bg_sidebar", 1.08), ("bg_panel", "bg_sidebar", 1.10),
]


def hue_of(h: str) -> int:
    r, g, b = _rgb(h)
    return round(colorsys.rgb_to_hls(r, g, b)[0] * 360)


def audit(name: str, p: dict[str, str]) -> int:
    fails = [(a, b, ratio(p[a], p[b]), t) for a, b, t in CHECKS if ratio(p[a], p[b]) < t]
    accents = ["accent_main", "accent_info", "accent_success", "accent_warning",
               "accent_danger"]
    hues = [hue_of(p[k]) for k in accents]
    gap = min(min((a - b) % 360, (b - a) % 360)
              for i, a in enumerate(hues) for b in hues[i + 1:])
    # -1 de tolerancia: el tono se asigna exacto pero se mide sobre el hex ya
    # cuantizado a 8 bits, que puede caer un grado corto.
    tight = gap < SEP - 1
    print(f"{name:10s} {'OK' if not (fails or tight) else str(len(fails)) + ' FALLOS':10s} "
          f"separacion minima entre acentos: {gap} grados")
    for a, b, r, t in fails:
        print(f"    {r:5.2f} < {t}   {a} sobre {b}")
    if tight:
        print(f"    acentos a {gap} grados (minimo {SEP})")
    return len(fails) + (1 if tight else 0)


def dump(palettes: dict[str, dict[str, str]]) -> str:
    out = ["    THEMES = {"]
    for name, p in palettes.items():
        out.append(f'        "{name}": {{')
        for group in GROUPS:
            out.append("            " + " ".join(f'"{k}": "{p[k]}",' for k in group))
        out.append("        },")
    out.append("    }")
    return "\n".join(out)


def check_in_sync(palettes: dict[str, dict[str, str]]) -> None:
    """Avisa si `theme_manager.py` se aparto de lo que genera este script.

    Un retoque a mano es legitimo —por eso el volcado se pega y no se escribe solo—
    pero conviene saber que existe antes de tocar los parametros de arriba.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from core.theme_manager import ThemeManager
    except Exception as e:
        print(f"(no se pudo comparar con theme_manager: {e})")
        return

    drift = 0
    for name, generated in palettes.items():
        live = ThemeManager.THEMES.get(name)
        if live is None:
            print(f"  {name}: no esta en theme_manager")
            drift += 1
            continue
        diff = sorted(k for k in generated if live.get(k) != generated[k])
        if diff:
            print(f"  {name}: retocado a mano en {diff}")
            drift += 1
    print("theme_manager.py coincide con el generador" if not drift
          else f"{drift} tema(s) difieren del generador")


def main() -> int:
    palettes = {name: build(spec) for name, spec in SPEC.items()}
    if "--print" in sys.argv:
        print(dump(palettes))
        return 0
    bad = sum(audit(name, p) for name, p in palettes.items())
    print(f"\n{'TODO OK' if not bad else str(bad) + ' FALLOS'} en {len(palettes)} temas")
    check_in_sync(palettes)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
