"""Retira previews descargadas y conserva sólo su marcador en catalog.db."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "third_party"))

from cherry_dl.config import INDEX_DB, load_config  # noqa: E402
from cherry_dl.index import export_all_meta  # noqa: E402
from cherry_dl.preview_cleanup import (  # noqa: E402
    apply_cleanup,
    inspect_tree,
    reconcile_index_from_quarantine,
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root", nargs="?", type=Path,
        help="carpeta madre; por defecto usa download_dir de Cherry-DL",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="aplica la limpieza; sin esta opción sólo muestra el plan",
    )
    parser.add_argument(
        "--include-untracked", action="store_true",
        help="también pone en cuarentena preview_* sin una fila is_preview",
    )
    parser.add_argument(
        "--reconcile-quarantine", type=Path,
        help="resta del índice central los archivos de esa cuarentena",
    )
    args = parser.parse_args()
    root = (args.root or load_config().download_path).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"no existe la carpeta madre: {root}")

    if args.reconcile_quarantine:
        reconciled = reconcile_index_from_quarantine(
            INDEX_DB, root, args.reconcile_quarantine,
        )
        if reconciled.errors:
            for error in reconciled.errors:
                print(f"ERROR: {error}")
            return 1
        if reconciled.already_applied:
            print("El ajuste de esa cuarentena ya estaba aplicado.")
            return 0
        asyncio.run(export_all_meta(INDEX_DB))
        print(
            f"Índice reconciliado: {reconciled.adjusted_profiles} perfil(es), "
            f"{reconciled.removed_from_counts} preview(s) restadas."
        )
        return 0

    report = inspect_tree(root)
    print(f"Carpeta madre: {root}")
    print(
        f"Catálogos: {len(report.catalogs)} | previews descargadas: "
        f"{report.downloaded} | en cola: {report.queued} | en espera: "
        f"{report.awaiting}"
    )
    untracked = sum(len(item.preview_like_untracked) for item in report.catalogs)
    action = "se pondrán en cuarentena" if args.include_untracked else "no se tocarán"
    print(f"Archivos preview_* sin marca de DB ({action}): {untracked}")
    for item in report.catalogs:
        affected = len(item.downloaded) + item.queued + item.awaiting
        if affected or item.error:
            suffix = f" ERROR: {item.error}" if item.error else ""
            print(
                f"- {item.catalog.parent}: {len(item.downloaded)} archivo(s), "
                f"{item.queued} en cola, {item.awaiting} en espera{suffix}"
            )

    if not args.apply:
        print("Simulación terminada. Usa --apply para ejecutar la reversión.")
        return 0

    apply_cleanup(report, include_untracked=args.include_untracked)
    errors = [item for item in report.catalogs if item.error]
    if report.quarantine:
        print(f"Archivos y respaldos recuperables: {report.quarantine}")
    print(f"Reversión aplicada con {len(errors)} error(es).")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
