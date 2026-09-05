"""
Migración de Kemono a Pawchive: re-apuntar las fuentes y purgar la cola muerta.

Kemono ya no descarga —el 2026-09-05 ni siquiera conectaba: timeout de TCP, no
un 404— y `pawchive.pw` es un clon suyo con la misma API. Este script hace las
dos mitades de la mudanza:

  1. **Re-apuntar** — añade a cada proyecto la fuente equivalente de Pawchive,
     dejando la de Kemono como está (decisión de David, 2026-09-05).
  2. **Purgar** — saca de `pending_queue` las filas que apuntan a hosts de
     Kemono, que no se limpian solas: un timeout es diferible, así que cada una
     se reintenta para siempre sin poder terminar nunca.

**El id de creador propone; el nombre confirma.** El id de Pawchive es el del
servicio original, así que la traducción parece directa — pero no siempre lo es:
el proyecto `𝕮𝖗𝖔𝖘𝖘` apuntaba a `patreon/61047323`, que en Pawchive es otro
creador («视野环球财经»), confirmado a mano por David. Por eso solo se añaden las
fuentes cuyo nombre acompaña al id; para el resto se buscan candidatos por
nombre y se dejan por escrito, sin tocarlas.

Ambas fases enseñan los números y **piden confirmación** antes de escribir, y
ninguna toca un archivo del disco: la primera añade una fila a `index.db` y la
segunda borra filas de cola. Lo ya descargado no se toca en ningún caso.

Al aplicar se escribe un informe (`--informe`, por defecto
`MIGRACION_PAWCHIVE.txt` en la raíz) con lo añadido, lo dudoso y lo excluido,
para revisarlo con calma después.

Cortesía con Pawchive: verificar todos los creadores cuesta **una sola
petición** —su índice se pide una vez y el resto se resuelve en memoria— y esa
petición pasa por el pacer de 1/s como cualquier otra.

Uso:
    venv/Scripts/python.exe scripts/cherry_kemono_to_pawchive.py
    venv/Scripts/python.exe scripts/cherry_kemono_to_pawchive.py --aplicar
    venv/Scripts/python.exe scripts/cherry_kemono_to_pawchive.py --aplicar --excluir "𝕮𝖗𝖔𝖘𝖘"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "third_party"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from modules.cherry.logic import pending_purge, repoint    # noqa: E402

EXCLUIDO = "excluido"      # lo dejó fuera el usuario con --excluir

_ETIQUETAS = {
    repoint.LISTO:         "id y nombre cuadran — se añaden",
    repoint.OTRO_NOMBRE:   "el id es de otro creador — NO se tocan",
    repoint.NO_ESTA:       "el id no está en Pawchive",
    repoint.AMBIGUO:       "el id existe en varios servicios — NO se tocan",
    repoint.OTRO_SERVICIO: "servicio que Pawchive no tiene",
    repoint.SIN_URL:       "sin URL y sin artist_id — no hay por dónde empezar",
    repoint.YA_ESTA:       "el proyecto ya tenía la fuente",
    EXCLUIDO:              "excluidos a mano",
}


def confirmar(pregunta: str) -> bool:
    try:
        return input(f"\n{pregunta} [escribí SI para continuar]: ").strip() == "SI"
    except (EOFError, KeyboardInterrupt):
        print("\ncancelado")
        return False


def _sugerencias(c) -> list[str]:
    """Las alternativas por nombre de un candidato, listas para imprimir."""
    if not c.sugerencias:
        return ["      (ningún creador de Pawchive se parece por nombre)"]
    return [
        f"      {s.score:.2f}  {s.nombre[:28]:<29} {s.url}"
        for s in c.sugerencias
    ]


# ── Fase 1 ────────────────────────────────────────────────────────────────────

async def fase_repoint(aplicar: bool, excluir: list[str]) -> list:
    print("\n" + "=" * 74)
    print("FASE 1 — re-apuntar las fuentes de Kemono a Pawchive")
    print("=" * 74)
    print("\nConsultando el índice de creadores de Pawchive (1 petición)…")

    candidatos = await repoint.plan()
    if not candidatos:
        print("No hay ninguna fuente de Kemono en el índice. Nada que hacer.")
        return []

    excluidos = {e.strip().casefold() for e in excluir}
    for c in candidatos:
        if c.listo and c.profile_name.strip().casefold() in excluidos:
            c.estado = EXCLUIDO

    conteo = repoint.resumen(candidatos)
    print(f"\n{len(candidatos)} fuentes de Kemono:")
    for estado, etiqueta in _ETIQUETAS.items():
        if conteo.get(estado):
            print(f"  {conteo[estado]:>4}  {etiqueta}")

    dudosos = [c for c in candidatos if c.estado == repoint.OTRO_NOMBRE]
    if dudosos:
        print(f"\n{'-' * 74}")
        print("EL ID EXISTE PERO ES DE OTRO CREADOR — no se añaden")
        print(f"{'-' * 74}")
        for c in dudosos:
            print(f"\n  {c.profile_name}")
            print(f"      id {c.service}/{c.creator_id} en Pawchive es "
                  f"«{c.nombre_pawchive}» (parecido {c.score:.2f})")
            print("      candidatos por nombre:")
            for linea in _sugerencias(c):
                print(linea)

    fuera = [c for c in candidatos if c.estado == EXCLUIDO]
    if fuera:
        print(f"\nExcluidos a mano ({len(fuera)}), no se tocan:")
        for c in fuera:
            print(f"  {c.profile_name[:26]:<27} {c.pawchive_url}")

    ambiguos = [c for c in candidatos if c.estado == repoint.AMBIGUO]
    if ambiguos:
        print(f"\nEl id existe en varios servicios ({len(ambiguos)}) — "
              f"hay que elegir a mano:")
        for c in ambiguos:
            print(f"\n  {c.profile_name}  (id {c.creator_id})")
            for linea in _sugerencias(c):
                print(linea)

    sin_id = [c for c in candidatos if c.estado == repoint.NO_ESTA]
    if sin_id:
        print(f"\nEl id no está en Pawchive ({len(sin_id)}):")
        for c in sin_id:
            print(f"\n  {c.profile_name}  ({c.service}/{c.creator_id})")
            for linea in _sugerencias(c):
                print(linea)

    listos = [c for c in candidatos if c.listo]
    if listos:
        print(f"\n{'-' * 74}")
        print(f"SE AÑADIRÍAN ESTAS {len(listos)} FUENTES")
        print(f"{'-' * 74}")
        for c in sorted(listos, key=lambda x: x.profile_name.casefold()):
            print(f"  {c.profile_name[:26]:<27} → {c.nombre_pawchive[:24]:<25} "
                  f"{c.score:.2f}  {c.service}/{c.creator_id}")

    if not listos:
        print("\nNada que añadir.")
        return candidatos
    if not aplicar:
        print("\n(simulación — ejecutá con --aplicar para escribir)")
        return candidatos
    if not confirmar(f"¿Añadir {len(listos)} fuentes de Pawchive?"):
        print("cancelado")
        return candidatos

    resultados = await repoint.apply(listos)
    ok = sum(1 for _, err in resultados if not err)
    print(f"\n✓ {ok} fuentes añadidas.")
    for c, err in resultados:
        if err:
            print(f"  ✗ {c.profile_name}: {err}")
            c.estado = "error"
    return candidatos


# ── Fase 2 ────────────────────────────────────────────────────────────────────

async def fase_purga(aplicar: bool) -> int:
    print("\n" + "=" * 74)
    print("FASE 2 — purgar la cola que apunta a Kemono")
    print("=" * 74)
    print("\nLeyendo las colas de todos los proyectos…")

    plan_ = await pending_purge.plan()
    print(f"\n{plan_.total:,} filas de cola en total, "
          f"{plan_.a_purgar:,} apuntan a un host muerto "
          f"({', '.join(pending_purge.HOSTS_MUERTOS)}).")

    if not plan_.a_purgar:
        print("Nada que purgar.")
        return 0

    print(f"\nPor proyecto ({len(plan_.afectados)} afectados):")
    print(f"  {'proyecto':<30} {'se borran':>10} {'quedan':>9}")
    for p in sorted(plan_.afectados, key=lambda x: -x.a_purgar):
        print(f"  {p.nombre[:29]:<30} {p.a_purgar:>10,} {p.quedan:>9,}")

    print("\nNo se borra ningún archivo del disco ni nada del catálogo de lo ya")
    print("descargado: solo la anotación «esto está pendiente». Si el host")
    print("revive, un re-scan la repuebla entera.")

    if not aplicar:
        print("\n(simulación — ejecutá con --aplicar para borrar)")
        return 0
    if not confirmar(f"¿Borrar {plan_.a_purgar:,} filas de cola?"):
        print("cancelado")
        return 0

    def progreso(nombre: str, hechas: int, total: int) -> None:
        print(f"  {nombre[:28]:<29} {hechas:>7,}/{total:,}", end="\r")

    borradas = await pending_purge.aplicar(plan_, on_progress=progreso)
    print(f"\n\n✓ {borradas:,} filas fuera de la cola.")
    return borradas


# ── Informe ───────────────────────────────────────────────────────────────────

def escribir_informe(ruta: Path, candidatos: list, purgadas: int) -> None:
    """Deja por escrito qué se añadió y qué quedó pendiente de revisar.

    Existe porque la parte que un programa no puede cerrar —¿este id es de este
    creador?— hay que mirarla a mano, y sin una lista escrita eso se olvida en
    cuanto termina la sesión.
    """
    aplicados = [c for c in candidatos if c.listo]
    dudosos = [c for c in candidatos if c.estado == repoint.OTRO_NOMBRE]
    fuera = [c for c in candidatos if c.estado == EXCLUIDO]
    sin_id = [c for c in candidatos if c.estado == repoint.NO_ESTA]
    ambiguos = [c for c in candidatos if c.estado == repoint.AMBIGUO]
    sin_destino = [c for c in candidatos
                   if c.estado in (repoint.OTRO_SERVICIO, repoint.SIN_URL)]

    L: list[str] = []
    add = L.append
    add("MIGRACIÓN KEMONO → PAWCHIVE")
    add(f"Generado el {datetime.now():%Y-%m-%d %H:%M}")
    add("=" * 78)
    add("")
    add("Qué se hizo: a cada proyecto se le AÑADIÓ la fuente equivalente de")
    add("Pawchive, solo cuando el id y el nombre del creador cuadraban. La")
    add("fuente de Kemono se dejó encendida y sin tocar, así que nada de esto")
    add("es irreversible: para deshacer uno, se desactiva su fuente de Pawchive")
    add("desde la app.")
    add("")
    add(f"Fuentes añadidas ............... {len(aplicados)}")
    add(f"Id ocupado por otro creador .... {len(dudosos)}   <- REVISAR")
    add(f"Id que no está en Pawchive ..... {len(sin_id)}   <- REVISAR")
    add(f"Id en varios servicios ......... {len(ambiguos)}   <- REVISAR")
    add(f"Excluidas a mano ............... {len(fuera)}")
    add(f"Sin destino posible ............ {len(sin_destino)}")
    add(f"Filas de cola purgadas ......... {purgadas:,}")
    add("")

    if dudosos or sin_id or ambiguos or fuera:
        add("=" * 78)
        add("PENDIENTE DE REVISIÓN MANUAL")
        add("=" * 78)
        add("")
        add("El id de creador de Pawchive es el del servicio original, pero los")
        add("dos archivadores indexan por su cuenta y un id puede haberse")
        add("reciclado o registrado mal. Cuando el nombre no acompaña al id,")
        add("esta herramienta NO añade nada: hay que abrir la URL y mirar.")
        add("")
        add("Si al abrirla el creador es el correcto, la fuente se añade desde")
        add("la app (proyecto → añadir fuente) con esa misma URL.")
        add("")

    def bloque(titulo: str, filas: list, con_sugerencias: bool) -> None:
        if not filas:
            return
        add("-" * 78)
        add(titulo)
        add("-" * 78)
        add("")
        for c in filas:
            add(f"  proyecto  : {c.profile_name}")
            add(f"  kemono    : {c.kemono_url}")
            add(f"  id igual  : {c.pawchive_url}")
            if c.nombre_pawchive:
                add(f"              ese id en Pawchive es «{c.nombre_pawchive}» "
                    f"(parecido {c.score:.2f})")
            if con_sugerencias:
                if c.sugerencias:
                    add("  por nombre:")
                    for s in c.sugerencias:
                        add(f"      {s.score:.2f}  {s.nombre[:28]:<29} {s.url}")
                else:
                    add("  por nombre: ningún creador se parece")
            add("")

    bloque("EL ID EXISTE PERO ES DE OTRO CREADOR", dudosos, True)
    bloque("EL ID NO ESTÁ EN PAWCHIVE", sin_id, True)
    bloque("EL ID EXISTE EN VARIOS SERVICIOS", ambiguos, True)
    bloque("EXCLUIDOS A MANO", fuera, True)

    add("=" * 78)
    add("FUENTES AÑADIDAS")
    add("=" * 78)
    add("")
    add(f"  {'proyecto':<30} {'nombre en pawchive':<26} {'sim':<5} URL")
    for c in sorted(aplicados, key=lambda x: x.profile_name.casefold()):
        add(f"  {c.profile_name[:29]:<30} {c.nombre_pawchive[:25]:<26} "
            f"{c.score:.2f}  {c.pawchive_url}")

    if sin_destino:
        add("")
        add("=" * 78)
        add("SIN DESTINO POSIBLE")
        add("=" * 78)
        add("")
        for c in sin_destino:
            add(f"  {c.profile_name[:29]:<30} "
                f"{_ETIQUETAS.get(c.estado, c.estado)}")

    add("")
    ruta.write_text("\n".join(L), encoding="utf-8")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Migra las fuentes de Kemono a Pawchive y purga la cola muerta."
    )
    ap.add_argument("--aplicar", action="store_true",
                    help="escribe de verdad (sin esto, solo simula)")
    ap.add_argument("--solo", choices=("repoint", "purga"),
                    help="ejecutar solo una de las dos fases")
    ap.add_argument("--excluir", action="append", default=[], metavar="PROYECTO",
                    help="no re-apuntar este proyecto (repetible)")
    ap.add_argument("--informe", default=str(ROOT / "MIGRACION_PAWCHIVE.txt"),
                    help="dónde escribir el informe de revisión manual")
    args = ap.parse_args()

    if not args.aplicar:
        print("MODO SIMULACIÓN — no se escribe nada.")

    candidatos: list = []
    if args.solo != "purga":
        candidatos = await fase_repoint(args.aplicar, args.excluir)

    purgadas = 0
    if args.solo != "repoint":
        purgadas = await fase_purga(args.aplicar)

    if args.aplicar and candidatos:
        ruta = Path(args.informe)
        escribir_informe(ruta, candidatos, purgadas)
        print(f"\nInforme de revisión manual: {ruta}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
