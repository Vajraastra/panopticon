"""Vista previa o ejecución auditable del mantenimiento batch de Cherry."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audita, descomprime, deduplica y compacta proyectos Cherry.",
    )
    parser.add_argument("--apply", action="store_true", help="aplica los cambios; sin esto sólo planea")
    parser.add_argument("--force-audit", action="store_true", help="recalcula SHA-256 aunque el perfil ya fue auditado")
    parser.add_argument("--profile", type=int, action="append", help="limita a uno o más ids de perfil")
    parser.add_argument("--report", type=Path, help="ruta del reporte JSON")
    return parser.parse_args()


def write_report(target: Path, report: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)


async def run(args: argparse.Namespace, target: Path) -> dict:
    from third_party.cherry_dl.config import INDEX_DB
    from third_party.cherry_dl.index import get_profile, list_profiles

    rows = await list_profiles(INDEX_DB)
    wanted = set(args.profile or [])
    if wanted:
        rows = [row for row in rows if row["id"] in wanted]
    profiles = []
    for row in rows:
        detail = await get_profile(INDEX_DB, row["id"])
        if detail is not None:
            profiles.append(detail)

    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "apply" if args.apply else "plan",
        "force_audit": bool(args.force_audit),
        "projects": [],
    }
    if args.apply:
        from modules.cherry.logic.archive_batch import process_profiles

        def profile_progress(position: int, total: int, _profile_id: int, name: str) -> None:
            print(f"[{position}/{total}] {name}", flush=True)

        result = await process_profiles(
            profiles,
            force_audit=args.force_audit,
            on_profile=profile_progress,
            on_file=lambda name: print(f"    {name}", flush=True),
        )
        report["summary"] = vars(result)
        write_report(target, report)
    else:
        from third_party.cherry_dl.project_maintenance import plan_project

        for position, profile in enumerate(profiles, 1):
            print(f"[{position}/{len(profiles)}] {profile['display_name']}", flush=True)
            seen = 0

            def file_progress(_name: str) -> None:
                nonlocal seen
                seen += 1
                if seen % 500 == 0:
                    print(f"    {seen} archivos hasheados", flush=True)

            try:
                plan = await plan_project(
                    Path(profile["folder_path"]),
                    profile["display_name"],
                    force_audit=args.force_audit,
                    emit=file_progress,
                )
            except Exception as exc:
                report["projects"].append({
                    "id": profile["id"], "name": profile["display_name"],
                    "folder": profile["folder_path"],
                    "audited": 0, "duplicates": 0, "renames": 0,
                    "errors": [f"{type(exc).__name__}: {exc}"],
                })
            else:
                report["projects"].append({
                    "id": profile["id"],
                    "name": profile["display_name"],
                    "folder": profile["folder_path"],
                    "audited": len(plan["files"]) + len(plan["duplicates"])
                    if plan["audit_version"] else 0,
                    "duplicates": len(plan["duplicates"]),
                    "renames": sum(item["old"] != item["new"] for item in plan["files"]),
                    "errors": plan["errors"],
                })
            write_report(target, report)
    return report


def main() -> None:
    args = arguments()
    target = args.report or ROOT / ".work" / "cherry-archive-batch-report.json"
    report = asyncio.run(run(args, target))
    write_report(target, report)
    print(f"Reporte: {target}")


if __name__ == "__main__":
    main()
