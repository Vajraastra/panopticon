"""
Presupuesto diario de bytes por sitio.

Por qué existe
--------------
El pacer limita cuántas veces se pide y el cubo de fichas limita el caudal
instantáneo, pero ninguno de los dos acota el TOTAL. Un caudal de 2 MB/s
sostenido son 172 GB al día, y la queja que provocó el bloqueo de Pawchive el
2026-09-05 estaba escrita en esa unidad: «TBs a day is just abusive». Quien
paga el ancho de banda piensa en gigas por día, no en peticiones por segundo.

Dónde vive
----------
En `~/.cherry-dl/site_budget.json`, un sidecar propio junto al estado del
motor. No toca `index.db` ni ningún `catalog.db`: no es información del
catálogo, es una cortesía nuestra con quien nos deja entrar, y perder el
archivo no corrompe nada — el peor caso es un día con el contador a cero.

Forma del archivo, deliberadamente aburrida::

    {"pawchive": {"day": "2026-09-05", "bytes": 4038201344}}

Un solo día por sitio: al cambiar la fecha, el contador se reemplaza. No se
guarda histórico porque nadie lo consultaría, y un archivo que solo crece
acabaría siendo un problema propio.

Qué NO hace
-----------
No reserva por adelantado ni parte archivos. Un archivo empezado se termina
aunque se pase del presupuesto: cortar a la mitad gastaría el ancho de banda
igual y encima no dejaría nada. El presupuesto decide si se EMPIEZA el
siguiente.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .config import CHERRY_DIR

BUDGET_FILE = CHERRY_DIR / "site_budget.json"


def _load(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # Un sidecar ilegible no puede tumbar una descarga: se empieza de cero.
        return {}


def spent_today(site: str, path: Path | None = None) -> int:
    """Bytes ya traídos hoy de `site`. 0 si es otro día o no hay registro."""
    entry = _load(path or BUDGET_FILE).get(site)
    if not isinstance(entry, dict) or entry.get("day") != date.today().isoformat():
        return 0
    try:
        return max(0, int(entry.get("bytes", 0)))
    except (TypeError, ValueError):
        return 0


def add(site: str, nbytes: int, path: Path | None = None) -> int:
    """Suma `nbytes` a lo gastado hoy y devuelve el nuevo total.

    Escritura atómica (temporal + `os.replace`): el archivo se toca una vez por
    descarga y un corte de luz a media escritura no puede dejarlo a medias.
    """
    if nbytes <= 0:
        return spent_today(site, path)

    target = path or BUDGET_FILE
    data = _load(target)
    total = spent_today(site, path) + nbytes
    data[site] = {"day": date.today().isoformat(), "bytes": total}

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        tmp.replace(target)
    except OSError:
        pass   # ver arriba: no poder anotar no es motivo para no descargar

    return total


def remaining(site: str, budget: int, path: Path | None = None) -> int:
    """Bytes que quedan hoy. `budget <= 0` significa sin tope: devuelve -1."""
    if budget <= 0:
        return -1
    return max(0, budget - spent_today(site, path))
