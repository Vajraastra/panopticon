"""
Herramientas de edición masiva sobre los sidecars .txt de un dataset ya
generado (la carpeta de salida del tagger).

Dos modos de operación:
- as_tag=True  -> trata cada .txt como lista de tags separadas por coma
  (formato booru): coincidencia exacta por token, case-insensitive, con
  deduplicación al reescribir. Ideal para datasets de tags.
- as_tag=False -> reemplazo por subcadena sobre el texto crudo. Útil para
  datasets en lenguaje natural.

Nada aquí toca las imágenes originales: solo los .txt de la carpeta de salida.
"""
import logging
from pathlib import Path

log = logging.getLogger(__name__)

SEP = ", "


def caption_files(folder, recursive=False):
    folder = Path(folder)
    it = folder.rglob("*.txt") if recursive else folder.glob("*.txt")
    return sorted(p for p in it if p.is_file())


def read(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as e:
        log.warning("No se pudo leer %s: %s", path, e)
        return ""


def write(path, text):
    Path(path).write_text(text or "", encoding="utf-8")


def split_tags(text):
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def join_tags(tags):
    # dedup case-insensitive preservando orden
    seen, out = set(), []
    for t in tags:
        k = t.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(t.strip())
    return SEP.join(out)


# -- búsqueda -----------------------------------------------------------------
def find(folder, needle, as_tag=True, recursive=False):
    """Devuelve [(path, texto)] de los .txt que contienen el tag/subcadena."""
    needle = (needle or "").strip()
    if not needle:
        return []
    nl = needle.lower()
    hits = []
    for p in caption_files(folder, recursive):
        text = read(p)
        if as_tag:
            if nl in (t.lower() for t in split_tags(text)):
                hits.append((p, text))
        elif nl in text.lower():
            hits.append((p, text))
    return hits


# -- reemplazo / eliminación --------------------------------------------------
def replace(folder, old, new, as_tag=True, recursive=False):
    """Reemplaza un tag/subcadena en todos los .txt. new='' => eliminar.
    Retorna el número de archivos modificados."""
    old = (old or "").strip()
    if not old:
        return 0
    new = (new or "").strip()
    changed = 0
    for p in caption_files(folder, recursive):
        text = read(p)
        if as_tag:
            ol = old.lower()
            tags = split_tags(text)
            if ol not in (t.lower() for t in tags):
                continue
            rebuilt = []
            for t in tags:
                if t.lower() == ol:
                    if new:
                        rebuilt.append(new)
                else:
                    rebuilt.append(t)
            updated = join_tags(rebuilt)
        else:
            if old.lower() not in text.lower():
                continue
            updated = _isubstr_replace(text, old, new)
        if updated != text:
            write(p, updated)
            changed += 1
    return changed


def remove(folder, target, as_tag=True, recursive=False):
    return replace(folder, target, "", as_tag=as_tag, recursive=recursive)


def apply_banned(folder, banned, as_tag=True, recursive=False):
    """Elimina cada tag de la lista de baneadas en todos los .txt.
    Retorna el total de archivos modificados (únicos)."""
    banned = [b.strip() for b in (banned or []) if b and b.strip()]
    if not banned:
        return 0
    touched = set()
    for p in caption_files(folder, recursive):
        text = read(p)
        original = text
        if as_tag:
            ban_l = {b.lower() for b in banned}
            tags = [t for t in split_tags(text) if t.lower() not in ban_l]
            text = join_tags(tags)
        else:
            for b in banned:
                text = _isubstr_replace(text, b, "")
            text = " ".join(text.split())  # colapsa espacios sobrantes
        if text != original:
            write(p, text)
            touched.add(str(p))
    return len(touched)


def _isubstr_replace(text, old, new):
    """Reemplazo de subcadena case-insensitive preservando el resto del texto."""
    result, low, oldl = [], text.lower(), old.lower()
    i = 0
    while True:
        j = low.find(oldl, i)
        if j < 0:
            result.append(text[i:])
            break
        result.append(text[i:j])
        result.append(new)
        i = j + len(old)
    return "".join(result)
