"""
Motor de autocompletado de tags — soporte multi-CSV (sin LLM).

Portado del módulo Painter de ai-hub (mismo formato y algoritmo). Permite que
cada arquitectura de tags use su propia base de datos optimizada
(p. ej. Pony → e621, Illustrious/SDXL → danbooru+e621 merged).

CSVs almacenados fuera del repo (gitignored); el caller resuelve la ruta y la
pasa explícita a `load_csv` — este módulo NO conoce `core.paths` para mantenerse
puro y reutilizable.

Formato de columnas del CSV: tag, category, post_count, aliases
  category: entero estilo Danbooru (0=general, 1=artist, 3=copyright,
            4=character, 5=meta). Se usa solo para colorear en la UI.

Solo stdlib. Búsqueda en memoria, instantánea (no requiere hilo).
"""
import csv
import math
import re
import unicodedata
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Tag:
    name:       str
    category:   int
    post_count: int
    aliases:    list[str] = field(default_factory=list)


# Registro de CSVs cargados: csv_name → {tags, word_index, count}
# word_index: token_completo → set de índices de tags que contienen ese token
_engines: dict[str, dict] = {}
_lock    = threading.Lock()


def _norm(s: str) -> str:
    """Normaliza: sin diacríticos, lowercase, underscores → espacios."""
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    return s.lower().replace('_', ' ').strip()


def _tokens(s: str) -> list[str]:
    """Extrae tokens normalizados de un nombre o alias."""
    return _norm(s).split()


def load_csv(csv_name: str, path: Path) -> int:
    """
    Carga el CSV en `path` y lo indexa bajo `csv_name`.
    Devuelve el número de tags cargados (0 si el archivo no existe).

    Es seguro re-llamar: reemplaza el índice existente para ese nombre.
    """
    p = Path(path)
    if not p.exists():
        return 0

    tags: list[Tag] = []
    with open(p, encoding='utf-8', newline='', errors='replace') as f:
        reader = csv.reader(f)
        next(reader, None)                     # saltar cabecera
        for row in reader:
            if len(row) < 3:
                continue
            name = row[0].strip()
            if not name:
                continue
            try:
                category   = int(row[1])
                post_count = int(row[2])
            except ValueError:
                continue
            aliases = (
                [a.strip() for a in row[3].split(',') if a.strip()]
                if len(row) > 3 and row[3].strip() else []
            )
            tags.append(Tag(name=name, category=category,
                            post_count=post_count, aliases=aliases))

    # Índice invertido por token completo (palabra).
    # Cubre nombre y todos los aliases → resuelve búsquedas en cualquier orden.
    word_index: dict[str, set[int]] = {}
    for i, tag in enumerate(tags):
        # Tokens del nombre (sin suffix de serie para indexar)
        name_clean = re.sub(r'_\([^)]+\)$', '', tag.name.lower())
        for tok in _tokens(name_clean):
            word_index.setdefault(tok, set()).add(i)
        # También indexar tokens del nombre completo (incluye serie)
        for tok in _tokens(tag.name):
            word_index.setdefault(tok, set()).add(i)
        # Tokens de cada alias
        for alias in tag.aliases:
            for tok in _tokens(alias):
                word_index.setdefault(tok, set()).add(i)

    with _lock:
        _engines[csv_name] = {"tags": tags, "word_index": word_index, "count": len(tags)}
    return len(tags)


def _score(q: str, q_tokens: set[str], tag: Tag) -> Optional[float]:
    name_raw   = tag.name.lower()
    name_spc   = name_raw.replace('_', ' ')
    name_clean = re.sub(r'_\([^)]+\)$', '', name_raw).replace('_', ' ')
    nct        = set(name_clean.split())
    aliases    = [a.lower().replace('_', ' ') for a in tag.aliases]

    # Tipos de coincidencia (de más a menos fuerte).
    is_exact    = (q == name_spc or q in aliases)
    is_allwords = bool(q_tokens) and q_tokens == nct
    name_pref   = name_spc.startswith(q) or any(a.startswith(q) for a in aliases)
    word_pref   = bool(q_tokens) and all(
        any(nt.startswith(qt) for nt in nct) for qt in q_tokens)
    substr      = q in name_spc or any(q in a for a in aliases)
    if not (is_exact or is_allwords or name_pref or word_pref or substr):
        return None

    # Ranking estilo booru: domina la POPULARIDAD (post_count). Solo la
    # coincidencia exacta (y "todas las palabras") se fija arriba; el resto
    # compite por popularidad, de modo que variantes muy usadas
    # (large_breasts 1.5M, huge_breasts 209k…) afloren al teclear la palabra
    # base, en vez de quedar sepultadas bajo prefijos literales poco usados
    # (breasts_day, 502). Un empujón pequeño premia el prefijo contiguo del
    # nombre y penaliza la subcadena suelta, SIN romper el orden por popularidad.
    pop = math.log10(max(tag.post_count, 1))
    if is_exact:
        return 100 + pop
    if is_allwords:
        return 50 + pop
    bonus = 0.0
    if name_pref:
        bonus += 0.5
    elif not word_pref and substr:
        bonus -= 0.5
    return pop + bonus


def search(csv_name: str, query: str, limit: int = 8) -> list[dict]:
    """
    Busca tags en el CSV indicado. Devuelve lista vacía si no está cargado.
    Usa índice invertido por token: "futaba" encuentra sakura_futaba_(persona_5)
    independientemente del orden en que el usuario escriba las palabras.

    Cada resultado: {name, category, post_count, matched_alias}.
    """
    with _lock:
        engine = _engines.get(csv_name)
        if not engine:
            return []
        tags       = engine["tags"]
        word_index = engine["word_index"]

    q = _norm(query)
    if not q:
        return []
    q_parts  = q.split()
    q_tokens = set(q_parts)

    # Candidatos: intersección de los sets de cada token de la query.
    # Tokens completados (no el último si es parcial) → lookup exacto.
    # Último token (puede ser parcial) → también busca por prefijo.
    candidate_ids: set[int] | None = None

    for i, tok in enumerate(q_parts):
        is_last = (i == len(q_parts) - 1)

        if is_last:
            # Buscar tags que contienen un token que empieza con este prefijo
            tok_hits: set[int] = set()
            tok_hits |= word_index.get(tok, set())          # exacto
            if len(tok) >= 2:                                # prefijo solo si ≥ 2 chars
                for word, ids in word_index.items():
                    if word != tok and word.startswith(tok):
                        tok_hits |= ids
        else:
            tok_hits = word_index.get(tok, set())           # exacto

        if candidate_ids is None:
            candidate_ids = set(tok_hits)
        else:
            candidate_ids &= tok_hits                       # AND entre tokens

    if not candidate_ids:
        candidate_ids = set()

    candidates = [tags[i] for i in candidate_ids]

    results = []
    for tag in candidates:
        sc = _score(q, q_tokens, tag)
        if sc is None:
            continue
        matched_alias = None
        for a in tag.aliases:
            a_norm = a.lower().replace('_', ' ')
            if a_norm.startswith(q) or a_norm == q:
                matched_alias = a
                break
        results.append({
            'name':          tag.name,
            'category':      tag.category,
            'post_count':    tag.post_count,
            'matched_alias': matched_alias,
            '_sc':           sc,
        })

    results.sort(key=lambda x: -x['_sc'])
    for r in results:
        del r['_sc']
    return results[:limit]


def is_loaded(csv_name: str) -> bool:
    with _lock:
        return csv_name in _engines


def tag_count(csv_name: str) -> int:
    with _lock:
        return _engines.get(csv_name, {}).get('count', 0)


def loaded_names() -> list[str]:
    with _lock:
        return list(_engines.keys())
