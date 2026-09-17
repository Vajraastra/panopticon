"""Orquestador batch del mantenimiento por perfil de Cherry."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ArchiveBatchResult:
    profiles: int = 0
    processed: int = 0
    added: int = 0
    duplicates: int = 0
    organized: int = 0
    audited: int = 0
    duplicates_removed: int = 0
    compacted: int = 0
    registered: int = 0
    folders: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, profile: dict, result) -> None:
        self.profiles += 1
        for name in (
            "processed", "added", "duplicates", "organized", "audited",
            "duplicates_removed", "compacted", "registered",
        ):
            setattr(self, name, getattr(self, name) + int(getattr(result, name, 0)))
        folder = str(profile["folder_path"])
        self.folders.append(folder)
        self.errors.extend(
            f"{profile['display_name']}: {detail}" for detail in result.errors
        )


async def process_profiles(
    profiles: list[dict],
    *,
    force_audit: bool = False,
    on_profile: Callable[[int, int, int, str], None] = lambda *_: None,
    on_file: Callable[[str], None] = lambda _: None,
) -> ArchiveBatchResult:
    """Itera exactamente la misma operación que usa un perfil individual."""
    from third_party.cherry_dl.archives import process_project

    total = len(profiles)
    batch = ArchiveBatchResult()
    for position, profile in enumerate(profiles, 1):
        name = profile.get("display_name") or "artist"
        on_profile(position, total, int(profile.get("id") or -1), name)
        folder = Path(profile["folder_path"])
        if not folder.is_dir():
            batch.profiles += 1
            batch.errors.append(f"{name}: carpeta ausente: {folder}")
            continue
        try:
            result = await process_project(
                folder, name, on_file, force_audit=force_audit,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            batch.profiles += 1
            batch.errors.append(f"{name}: {type(exc).__name__}: {exc}")
        else:
            batch.add(profile, result)
    return batch
