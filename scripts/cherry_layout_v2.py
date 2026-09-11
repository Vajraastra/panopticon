"""Audita o aplica nombres v2 y contadores por categoría. Panopticon debe estar cerrado."""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from third_party.cherry_dl.layout_migration import migrate_project


def readonly(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    index_path = Path.home() / ".cherry-dl" / "index.db"
    with readonly(index_path) as db:
        projects = db.execute(
            "SELECT display_name, folder_path FROM profiles ORDER BY id"
        ).fetchall()

    report: list[dict] = []
    for artist, location in projects:
        item = {"project": artist, "folder": location}
        try:
            plan = await migrate_project(Path(location), artist, apply=args.apply)
            changed = [row for row in plan["updates"] if row["old"] != row["new"]]
            item.update(
                status=plan["status"],
                files=len(plan["updates"]),
                renames=len(changed),
                physical=sum(1 for row in changed if row["exists"]),
                categories=dict(Counter(row["route"] for row in plan["updates"])),
                counters=plan["counters"],
                errors=plan["errors"],
            )
        except Exception as exc:
            item.update(status="error", errors=[f"{type(exc).__name__}: {exc}"])
        report.append(item)
        print(json.dumps(item, ensure_ascii=True), flush=True)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        "projects": len(report),
        "files": sum(item.get("files", 0) for item in report),
        "renames": sum(item.get("renames", 0) for item in report),
        "physical": sum(item.get("physical", 0) for item in report),
        "errors": sum(len(item.get("errors", [])) for item in report),
    }
    print(json.dumps(summary, ensure_ascii=True))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
