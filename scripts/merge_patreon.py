#!/usr/bin/env python
r"""
merge_patreon.py — Fusión de carpetas huérfanas hacia la carpeta madre vigilada.

Flujo (diseño acordado 2026-07-03):
  1. Escanea nombres de subcarpetas en SRC (huérfanas) y DST (colecciones).
  2. Menú interactivo: muestra pares idénticos/similares y pedís visto bueno.
  3. Pares aprobados → hasheo SHA-256 de los archivos SRC y comparación contra
     TODOS los hashes de panopticon.db (hash_original y hash_mod):
       - duplicado  → cuarentena SRC/_duplicados/<carpeta>/... (reversible)
       - único      → DST/<colección>-patreon/... si la colección es un catálogo
                      cherry (catalog.db presente: el indexer la lee solo del
                      catálogo, un archivo ajeno adentro sería invisible);
                      si es carpeta normal, se fusiona directo en ella.
  4. Carpetas SIN correspondencia → se mueven enteras a DST (sin hashear;
     el dedup M4 las cubrirá después).
  5. Todo movimiento queda registrado en SRC/_merge_log.jsonl.

Uso (desde la raíz del repo, con el venv):
  venv\Scripts\python.exe scripts\merge_patreon.py [--dry-run]
      [--src G:/images/patreon] [--dst G:/images/cherry-dl] [--db panopticon.db]

--dry-run: recorre y reporta TODO (incluido el hasheo) sin mover un solo archivo.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from difflib import SequenceMatcher

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JUNK_NAMES = {'thumbs.db', 'desktop.ini', '.ds_store', 'dt_thumbnails.db'}
SIM_THRESHOLD = 0.70
CHUNK = 1024 * 1024  # streaming SHA-256, mismo enfoque que HashBackfillWorker


# ---------------------------------------------------------------- utilidades

def wp(path: str) -> str:
    """Ruta extended-length en Windows para esquivar el límite de 260 chars."""
    p = os.path.abspath(path)
    if sys.platform == 'win32' and not p.startswith('\\\\?\\'):
        return '\\\\?\\' + p
    return p


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(wp(path), 'rb') as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def norm_name(name: str) -> str:
    """Normaliza para comparar: casefold + solo alfanuméricos."""
    return ''.join(c for c in name.casefold() if c.isalnum())


def similarity(a: str, b: str) -> float:
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    # Contención (p.ej. "kananbun" ⊂ "kanabun305" no, pero "shift" ⊂ "shiftart" sí)
    if na in nb or nb in na:
        ratio = max(ratio, 0.85)
    return ratio


def count_files(folder: str) -> int:
    total = 0
    for _, _, files in os.walk(wp(folder)):
        total += len(files)
    return total


def unique_target(path: str) -> str:
    """Si el destino existe, añade sufijo _1, _2… antes de la extensión."""
    if not os.path.exists(wp(path)):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(wp(f"{base}_{i}{ext}")):
        i += 1
    return f"{base}_{i}{ext}"


def prune_empty_dirs(root: str) -> None:
    """Elimina subdirectorios vacíos bajo root (bottom-up). No toca root."""
    for dirpath, dirnames, filenames in os.walk(wp(root), topdown=False):
        if not dirnames and not filenames and os.path.abspath(dirpath) != os.path.abspath(wp(root)):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass


class MergeLog:
    def __init__(self, path: str, dry_run: bool):
        self.path = path
        self.dry_run = dry_run
        self._fh = None

    def write(self, **fields):
        fields['ts'] = datetime.now().isoformat(timespec='seconds')
        fields['dry_run'] = self.dry_run
        if self._fh is None:
            self._fh = open(self.path, 'a', encoding='utf-8')
        self._fh.write(json.dumps(fields, ensure_ascii=False) + '\n')
        self._fh.flush()

    def close(self):
        if self._fh:
            self._fh.close()


# ---------------------------------------------------------------- fases

def load_db_hashes(db_path: str) -> set:
    """Todos los hashes conocidos (original y mod) — identidad de contenido."""
    con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        cur = con.cursor()
        hashes = {row[0] for row in cur.execute(
            'SELECT hash_original FROM files WHERE hash_original IS NOT NULL')}
        hashes |= {row[0] for row in cur.execute(
            'SELECT hash_mod FROM files WHERE hash_mod IS NOT NULL')}
        return hashes
    finally:
        con.close()


def match_folders(src_dirs: list, dst_dirs: list):
    """Devuelve (pares_sospechosos, sin_correspondencia).
    Cada par: (src_name, dst_name, similitud). Un src matchea a lo sumo un dst
    (el de mayor similitud sobre el umbral)."""
    pairs, orphans = [], []
    for s in src_dirs:
        best, best_sim = None, 0.0
        for d in dst_dirs:
            sim = similarity(s, d)
            if sim > best_sim:
                best, best_sim = d, sim
        if best_sim >= SIM_THRESHOLD:
            pairs.append((s, best, best_sim))
        else:
            orphans.append(s)
    pairs.sort(key=lambda p: -p[2])
    return pairs, orphans


def review_menu(pairs, src_root) -> list:
    """Muestra los pares y pide visto bueno uno a uno. Devuelve los aprobados."""
    print('\n=== PARES SOSPECHOSOS (huérfana → colección existente) ===')
    print(f'{"#":>3}  {"sim":>5}  {"archivos":>8}  par')
    infos = []
    for i, (s, d, sim) in enumerate(pairs, 1):
        n = count_files(os.path.join(src_root, s))
        infos.append(n)
        print(f'{i:>3}  {sim:>5.2f}  {n:>8}  {s}  →  {d}')
    approved = []
    print('\nVisto bueno por par: [s]í = escanear por hashes / [n]o = tratar como '
          'carpeta sin correspondencia (migra entera) / [q] abortar todo.')
    for i, (s, d, sim) in enumerate(pairs, 1):
        while True:
            ans = input(f'  {i}/{len(pairs)} {s} → {d} (sim {sim:.2f}, {infos[i-1]} archivos) [s/n/q]: ').strip().lower()
            if ans in ('s', 'n', 'q'):
                break
        if ans == 'q':
            print('Abortado por el usuario. No se movió nada.')
            sys.exit(0)
        if ans == 's':
            approved.append((s, d))
    return approved


def process_pair(src_folder: str, dst_folder: str, known_hashes: set,
                 quarantine_root: str, log: MergeLog, dry_run: bool):
    """Hashea cada archivo de src_folder y lo mueve a destino o cuarentena."""
    src_name = os.path.basename(src_folder)
    is_catalog = os.path.isfile(os.path.join(dst_folder, 'catalog.db'))
    dest_root = dst_folder + '-patreon' if is_catalog else dst_folder
    stats = {'total': 0, 'dup': 0, 'unique': 0, 'junk': 0, 'error': 0}
    t0 = time.time()

    for dirpath, _, filenames in os.walk(src_folder):
        rel_dir = os.path.relpath(dirpath, src_folder)
        for fname in filenames:
            if fname.casefold() in JUNK_NAMES:
                stats['junk'] += 1
                continue
            stats['total'] += 1
            fpath = os.path.join(dirpath, fname)
            rel = os.path.normpath(os.path.join(rel_dir, fname))
            try:
                digest = sha256_of(fpath)
            except OSError as e:
                stats['error'] += 1
                log.write(action='error', src=fpath, error=str(e))
                print(f'    ⚠ error leyendo {rel}: {e}')
                continue

            if digest in known_hashes:
                target = unique_target(os.path.join(quarantine_root, src_name, rel))
                action = 'quarantine'
                stats['dup'] += 1
            else:
                target = unique_target(os.path.join(dest_root, rel))
                action = 'migrate'
                stats['unique'] += 1
                known_hashes.add(digest)  # dedup acumulativo entre huérfanas

            log.write(action=action, src=fpath, dst=target, sha256=digest)
            if not dry_run:
                os.makedirs(wp(os.path.dirname(target)), exist_ok=True)
                shutil.move(wp(fpath), wp(target))

            done = stats['dup'] + stats['unique']
            if done % 500 == 0:
                print(f'    … {done} archivos ({stats["dup"]} dup / {stats["unique"]} únicos)')

    if not dry_run:
        prune_empty_dirs(src_folder)
        try:
            os.rmdir(wp(src_folder))  # solo si quedó vacía
        except OSError:
            pass

    dt = time.time() - t0
    err_txt = f', {stats["error"]} errores' if stats['error'] else ''
    print(f'  ✔ {src_name}: {stats["total"]} archivos → {stats["dup"]} duplicados '
          f'(cuarentena), {stats["unique"]} únicos → {os.path.basename(dest_root)}'
          f'{err_txt} [{dt:.0f}s]')
    log.write(action='pair_done', folder=src_name, dest=dest_root, **stats)
    return stats


def migrate_whole(src_folder: str, dst_root: str, log: MergeLog, dry_run: bool):
    """Mueve la carpeta entera (sin hashear) a la carpeta madre destino."""
    name = os.path.basename(src_folder)
    target = os.path.join(dst_root, name)
    if os.path.exists(wp(target)):
        target = target + '-patreon'
    log.write(action='migrate_folder', src=src_folder, dst=target)
    if not dry_run:
        shutil.move(wp(src_folder), wp(target))
    print(f'  ✔ {name} → {target}')


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description='Fusión de carpetas huérfanas hacia la carpeta madre.')
    ap.add_argument('--src', default='G:/images/patreon')
    ap.add_argument('--dst', default='G:/images/cherry-dl')
    ap.add_argument('--db', default=os.path.join(REPO_ROOT, 'panopticon.db'))
    ap.add_argument('--dry-run', action='store_true',
                    help='reporta todo (incluido hasheo) sin mover archivos')
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    for p, tag in ((args.src, '--src'), (args.dst, '--dst')):
        if not os.path.isdir(p):
            sys.exit(f'ERROR: {tag} no existe: {p}')
    if not os.path.isfile(args.db):
        sys.exit(f'ERROR: --db no existe: {args.db}')

    src_dirs = sorted(d for d in os.listdir(args.src)
                      if os.path.isdir(os.path.join(args.src, d))
                      and d not in ('_duplicados',))
    dst_dirs = sorted(d for d in os.listdir(args.dst)
                      if os.path.isdir(os.path.join(args.dst, d)))
    print(f'Huérfanas en {args.src}: {len(src_dirs)} · Colecciones en {args.dst}: {len(dst_dirs)}'
          + (' · MODO DRY-RUN (no se mueve nada)' if args.dry_run else ''))

    pairs, orphans = match_folders(src_dirs, dst_dirs)
    approved = review_menu(pairs, args.src) if pairs else []
    rejected = [s for (s, _, _) in pairs if s not in {a for a, _ in approved}]
    wholesale = orphans + rejected

    print(f'\nPlan: {len(approved)} pares a escanear por hash · '
          f'{len(wholesale)} carpetas migran enteras.')
    if wholesale:
        print('  Enteras: ' + ', '.join(sorted(wholesale)))
    ans = input('¿Ejecutar? [s/n]: ').strip().lower()
    if ans != 's':
        print('Cancelado. No se movió nada.')
        return

    log = MergeLog(os.path.join(args.src, '_merge_log.jsonl'), args.dry_run)
    quarantine_root = os.path.join(args.src, '_duplicados')

    print('\nCargando hashes conocidos de panopticon.db…')
    known = load_db_hashes(args.db)
    print(f'  {len(known)} hashes en memoria.')

    totals = {'dup': 0, 'unique': 0, 'error': 0}
    for s, d in approved:
        print(f'\n— Escaneando {s} → {d}')
        st = process_pair(os.path.join(args.src, s), os.path.join(args.dst, d),
                          known, quarantine_root, log, args.dry_run)
        for k in totals:
            totals[k] += st[k]

    if wholesale:
        print('\n— Migrando carpetas enteras')
        for s in sorted(wholesale):
            migrate_whole(os.path.join(args.src, s), args.dst, log, args.dry_run)

    log.write(action='run_done', **totals)
    log.close()
    print(f'\n=== RESUMEN ===\nDuplicados a cuarentena: {totals["dup"]} · '
          f'Únicos migrados: {totals["unique"]} · Errores: {totals["error"]}')
    print(f'Log: {os.path.join(args.src, "_merge_log.jsonl")}')
    print('\nSiguientes pasos: 🚀 Run Indexer en Librarian (ingesta lo nuevo por walk), '
          'luego 🧬 Calcular hashes, y de ahí M4 (dedup DB-wide).')


if __name__ == '__main__':
    main()
