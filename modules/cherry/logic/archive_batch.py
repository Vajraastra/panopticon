"""Orquestador batch del mantenimiento por perfil de Cherry."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from third_party.cherry_dl.maintenance_progress import MaintenanceProgress, ProgressReporter


class MaintenanceBlocked(ValueError):
    """A checked refusal before the engine can organize, extract or rename."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__('cherry_maintenance_' + reason)


async def require_unindexed(folders, data_service):
    if data_service is None:
        raise MaintenanceBlocked('review_required')
    from core.path_identity import observe_path

    def check():
        paths = []
        for folder in folders:
            observed = observe_path(str(folder))
            if observed.status != 'resolved' or observed.kind != 'directory':
                raise MaintenanceBlocked('review_required')
            paths.extend([observed.path, observed.canonical])
        if not paths:
            return
        if data_service.read('has_maintenance_history', sorted(set(paths))):
            raise MaintenanceBlocked('indexed')

    try:
        await asyncio.to_thread(check)
    except MaintenanceBlocked:
        raise
    except Exception as exc:
        raise MaintenanceBlocked('review_required') from exc


async def process_unindexed_project(folder, artist, *, data_service=None, **options):
    await require_unindexed([folder], data_service)
    from third_party.cherry_dl.archives import process_project
    return await process_project(folder, artist, **options)


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
    on_progress: Callable[[MaintenanceProgress], None] = lambda _: None,
    data_service=None,
) -> ArchiveBatchResult:
    """Itera exactamente la misma operación que usa un perfil individual."""
    from third_party.cherry_dl.archives import process_project

    # Check the complete selection before touching even its first project.
    await require_unindexed([profile['folder_path'] for profile in profiles], data_service)
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
                progress=ProgressReporter(on_progress),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            batch.profiles += 1
            batch.errors.append(f"{name}: {type(exc).__name__}: {exc}")
        else:
            batch.add(profile, result)
    return batch
