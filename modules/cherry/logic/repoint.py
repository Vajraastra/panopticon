"""
Re-apuntar a Pawchive las fuentes que hoy están en Kemono.

## Por qué

Kemono sigue en pie pero ya no descarga — el 2026-09-05 ni siquiera conectaba:
timeout de TCP. `pawchive.pw` se declara «a public archiver based on Kemono» y
su API es la misma campo por campo, así que una fuente
`kemono.cr/patreon/user/12345` tiene un candidato obvio en
`pawchive.pw/patreon/user/12345`.

## Por qué el id NO basta

Ese candidato obvio es una **hipótesis, no una traducción**. Comprobado en la
biblioteca real (David, 2026-09-05): el proyecto `𝕮𝖗𝖔𝖘𝖘` apuntaba a
`patreon/61047323`, y en Pawchive ese mismo id es «视野环球财经», que no tiene
nada que ver. Los dos archivadores indexan de forma independiente, y un id puede
haberse reciclado, corregido o registrado mal en cualquiera de los dos.

Aceptar el id a ciegas habría metido obra de otro autor en esa carpeta, y como
la biblioteca deduplica por hash del contenido, deshacerlo después no es borrar
una fuente: es separar archivos ya mezclados.

Por eso la regla es **id Y nombre**. El id propone; el nombre confirma. Cuando
el nombre no acompaña, la fuente no se añade: se marca para revisión y se buscan
candidatos por nombre en el índice de creadores —que ya está en memoria, así que
sugerir no cuesta ninguna petición extra—. La decisión final es de un humano
mirando la página.

## Qué hace y qué no

**Añade** la fuente de Pawchive al mismo proyecto y **deja la de Kemono como
está** (decisión de David, 2026-09-05): si Kemono revive, sigue en su sitio.

No borra, no fusiona proyectos y no toca el disco. Lo único que escribe es una
fila nueva en `profile_urls`, por la puerta de siempre —`add_url_to_profile`, la
API de alto nivel del motor— sin SQL propio ni cambios de schema.

## Cortesía con el sitio

Verificar todos los candidatos cuesta **una sola petición**: se pide
`/api/v1/creators` una vez (el template lo cachea a nivel de clase) y todo lo
demás —confirmación por id, comprobación de nombre y búsqueda de alternativas—
se resuelve en memoria. Preguntar creador por creador serían decenas de
peticiones para saber lo mismo. Esa única petición pasa por el pacer de 1/s.

Dos fases separadas a propósito: `plan()` no escribe nada y `apply()` no toca la
red. Así los números se pueden enseñar antes de tocar el índice.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

log = logging.getLogger(__name__)

# Los dos únicos servicios que existen en Pawchive (patreon 68.947, fanbox
# 24.625, medidos el 2026-09-05). Una fuente de Kemono de otro servicio
# —discord, fantia— no tiene destino posible.
SERVICIOS = ("patreon", "fanbox")

_KEMONO_RE = re.compile(
    r"https?://kemono\.[a-z]+/([a-z]+)/user/([^/?#]+)",
    re.IGNORECASE,
)

_PAWCHIVE = "https://pawchive.pw"

# Umbral de parecido para dar un nombre por bueno sin intervención. El 0.72 sale
# de los casos reales: acepta «MereoleonArt»/«Mereoleon» y «cubedcoconut»/«Cubed
# Coconut», y rechaza «𝕮𝖗𝖔𝖘𝖘»/«视野环球财经». Ante la duda conviene que rechace:
# un falso rechazo cuesta una revisión, un falso acierto mezcla la obra de dos
# autores en una misma carpeta.
UMBRAL = 0.72

# Estados de un candidato.
LISTO         = "listo"          # id y nombre cuadran: se puede añadir
SIN_URL       = "sin_url"        # entrada migrada de Fase 1, sin URL que traducir
OTRO_SERVICIO = "otro_servicio"  # discord/fantia: Pawchive no los tiene
NO_ESTA       = "no_esta"        # el id no está indexado en Pawchive
OTRO_NOMBRE   = "otro_nombre"    # el id existe pero es de otro creador
YA_ESTA       = "ya_esta"        # el proyecto ya tiene esa fuente


def normalizar(s: str) -> str:
    """Nombre reducido a lo comparable: sin acentos, símbolos ni mayúsculas.

    Pasa por NFKD a propósito: los nombres de artista usan mucho las variantes
    matemáticas de Unicode (𝕮𝖗𝖔𝖘𝖘 → cross), y sin descomponerlas dos escrituras
    del mismo nombre no se parecerían en nada.
    """
    desc = unicodedata.normalize("NFKD", s or "")
    return "".join(
        ch for ch in desc.casefold()
        if ch.isalnum() and not unicodedata.combining(ch)
    )


# Una contención solo cuenta como identidad si el nombre contenido es
# sustancial: al menos 4 caracteres y la mitad del que lo contiene. Sin esas dos
# condiciones, «RO» y «r» puntuaban 1.00 contra «Cross» —están dentro de c-r-o-s-s—
# y llenaban las sugerencias de ruido justo donde más falta hace la señal.
_MIN_CONTENIDO = 4
_MIN_PROPORCION = 0.5


def parecido(a: str, b: str) -> float:
    """0.0–1.0 entre dos nombres. La contención sustancial vale 1.0.

    Que «Mereoleon» esté contenido en «MereoleonArt» es señal fuerte —los
    archivadores añaden y quitan sufijos como «Art», «60» o «AI»—, así que ahí
    no se castiga la diferencia de longitud, que es justo lo que haría el ratio
    solo. Una subcadena corta dentro de un nombre largo no es esa señal, y cae
    al ratio como cualquier otro parecido casual.
    """
    na, nb = normalizar(a), normalizar(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        corto, largo = sorted((na, nb), key=len)
        if len(corto) >= _MIN_CONTENIDO and len(corto) / len(largo) >= _MIN_PROPORCION:
            return 1.0
    return SequenceMatcher(None, na, nb).ratio()


@dataclass
class Sugerencia:
    """Un creador de Pawchive que se parece por nombre al proyecto."""
    service: str
    creator_id: str
    nombre: str
    score: float

    @property
    def url(self) -> str:
        return f"{_PAWCHIVE}/{self.service}/user/{self.creator_id}"


@dataclass
class Candidato:
    """Una fuente de Kemono y su destino propuesto en Pawchive."""
    profile_id: int
    profile_name: str
    url_id: int
    kemono_url: str
    service: str
    creator_id: str
    pawchive_url: str
    estado: str
    nombre_pawchive: str = ""
    score: float = 0.0
    sugerencias: list[Sugerencia] = field(default_factory=list)
    enabled: bool = True

    @property
    def listo(self) -> bool:
        return self.estado == LISTO


def traducir(url: str) -> tuple[str, str] | None:
    """`kemono.cr/patreon/user/123` a `('patreon', '123')`, o None.

    Acepta cualquier TLD de Kemono (`.su`, `.cr`, …): el proyecto ha cambiado de
    dominio más de una vez y las fuentes viejas conservan el de su alta.
    """
    m = _KEMONO_RE.match(url or "")
    if not m:
        return None
    return m.group(1).lower(), m.group(2)


def buscar_por_nombre(
    indice: dict[tuple[str, str], str],
    nombre: str,
    service: str = "",
    tope: int = 5,
) -> list[Sugerencia]:
    """Creadores de Pawchive cuyo nombre se parece a `nombre`.

    Trabaja sobre el índice ya cargado, así que no cuesta ninguna petición.
    Prioriza el mismo servicio pero no lo exige: un creador puede estar
    archivado desde Fanbox y desde Patreon a la vez, y quien revisa querrá verlo.
    """
    objetivo = normalizar(nombre)
    if not objetivo:
        return []
    fuera: list[Sugerencia] = []
    for (svc, cid), nom in indice.items():
        score = parecido(nombre, nom)
        if score < 0.6:
            continue
        if svc == service:
            score = min(score + 0.05, 1.0)   # desempate a favor del servicio
        fuera.append(Sugerencia(svc, cid, nom, score))
    fuera.sort(key=lambda s: -s.score)
    return fuera[:tope]


async def plan(db_path=None) -> list[Candidato]:
    """Qué fuentes de Kemono tienen equivalente confirmado en Pawchive.

    **Toca la red**: una petición al índice de creadores, ni una más. No escribe
    nada. Devuelve TODOS los candidatos, incluidos los descartados, porque el
    descarte es justo lo que hay que enseñar antes de aplicar.
    """
    from third_party.cherry_dl.config import load_config
    from third_party.cherry_dl.engine import DownloadEngine
    from third_party.cherry_dl.index import get_profile, list_profiles
    from third_party.cherry_dl.templates.pawchive import PawchiveTemplate

    from .profiles import index_db_path

    db = db_path or index_db_path()

    # ── 1. Recolectar los candidatos, sin red ────────────────────────────────
    crudos: list[Candidato] = []
    for p in await list_profiles(db):
        detalle = await get_profile(db, p["id"])
        if not detalle:
            continue
        ya_en_pawchive = {
            (u.get("url") or "").lower()
            for u in detalle["urls"]
            if u.get("site") == "pawchive"
        }
        for u in detalle["urls"]:
            if u.get("site") != "kemono":
                continue
            par = traducir(u.get("url") or "")
            if par is None:
                estado, service, creator_id, destino = SIN_URL, "", "", ""
            else:
                service, creator_id = par
                destino = f"{_PAWCHIVE}/{service}/user/{creator_id}"
                if service not in SERVICIOS:
                    estado = OTRO_SERVICIO
                elif destino.lower() in ya_en_pawchive:
                    estado = YA_ESTA
                else:
                    estado = LISTO      # provisional: falta confirmar el nombre
            crudos.append(Candidato(
                profile_id=p["id"],
                profile_name=detalle.get("display_name", ""),
                url_id=u["id"],
                kemono_url=u.get("url") or "",
                service=service,
                creator_id=creator_id,
                pawchive_url=destino,
                estado=estado,
                enabled=bool(u.get("enabled")),
            ))

    pendientes = [c for c in crudos if c.estado == LISTO]
    if not pendientes:
        return crudos

    # ── 2. Una única petición para confirmarlos todos ────────────────────────
    async with DownloadEngine(load_config()) as engine:
        engine.set_pace(PawchiveTemplate.min_request_interval)
        template = PawchiveTemplate(engine)
        indice = await template._get_creators_index()

    # ── 3. El id propone, el nombre confirma ─────────────────────────────────
    for c in pendientes:
        nombre = indice.get((c.service, c.creator_id))
        if nombre is None:
            c.estado = NO_ESTA
            c.sugerencias = buscar_por_nombre(indice, c.profile_name, c.service)
            continue
        c.nombre_pawchive = nombre
        c.score = parecido(c.profile_name, nombre)
        if c.score < UMBRAL:
            # El id existe, pero es de otro: no se toca y se buscan alternativas.
            c.estado = OTRO_NOMBRE
            c.sugerencias = buscar_por_nombre(indice, c.profile_name, c.service)

    log.info(
        "cherry/repoint: %s fuentes kemono, %s confirmadas por nombre, "
        "%s con el id ocupado por otro creador",
        len(crudos),
        sum(1 for c in crudos if c.listo),
        sum(1 for c in crudos if c.estado == OTRO_NOMBRE),
    )
    return crudos


async def apply(candidatos: list[Candidato]) -> list[tuple[Candidato, str]]:
    """Añade las fuentes de Pawchive de los candidatos en estado `listo`.

    **No toca la red** y no desactiva nada: la fuente de Kemono se queda como
    estaba. Devuelve `(candidato, error)` por cada uno intentado; `error` vacío
    si salió bien. Un fallo no aborta el resto — son proyectos independientes y
    dejar la mitad a medias por culpa de uno malo no ayuda a nadie.
    """
    from .profile_writes import add_url

    resultados: list[tuple[Candidato, str]] = []
    for c in candidatos:
        if not c.listo:
            continue
        try:
            await add_url(c.profile_id, c.pawchive_url)
            resultados.append((c, ""))
        except Exception as exc:                      # noqa: BLE001
            log.exception("cherry/repoint: falló %s", c.pawchive_url)
            resultados.append((c, f"{type(exc).__name__}: {exc}"))
    return resultados


def resumen(candidatos: list[Candidato]) -> dict[str, int]:
    """Cuenta por estado, para enseñar los números antes de aplicar."""
    conteo: dict[str, int] = {}
    for c in candidatos:
        conteo[c.estado] = conteo.get(c.estado, 0) + 1
    return conteo
