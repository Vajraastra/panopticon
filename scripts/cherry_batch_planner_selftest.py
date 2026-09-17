"""Regresión sin red del planificador batch por categorías (O3)."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QObject, Signal  # noqa: E402

from modules.cherry.logic.download_bridge import DownloadBridge  # noqa: E402
from modules.cherry.logic.download_queue import DownloadQueue  # noqa: E402
from third_party.cherry_dl.download_service import DownloadSummary  # noqa: E402
from third_party.cherry_dl import download_service  # noqa: E402
from third_party.cherry_dl.auth.patreon import NeedsManualAuth  # noqa: E402
from third_party.cherry_dl.index import (  # noqa: E402
    add_profile_url,
    create_profile,
    get_profile,
    init_index,
    set_profile_url_enabled,
)
from third_party.cherry_dl.site_policy import SourceCategory  # noqa: E402
from third_party.cherry_dl.templates.base import ArtistInfo, SiteTemplate  # noqa: E402


class FakeBridge(QObject):
    finished_signal = Signal(object)
    failed_signal = Signal(str)
    cancelled_signal = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.is_busy = False
        self.starts: list[tuple[str, SourceCategory, bool, set[str]]] = []

    def start(self, profile: dict, **params) -> bool:
        category = next(iter(params["source_categories"]))
        self.starts.append((
            profile["display_name"], category, params["finalize_profile"],
            params.get("ext_filter", set()),
        ))
        self.is_busy = True
        return True

    def finish(self, summary: DownloadSummary | None = None) -> None:
        self.is_busy = False
        self.finished_signal.emit(summary or DownloadSummary())

    def fail(self, message: str) -> None:
        self.is_busy = False
        self.failed_signal.emit(message)

    def cancel(self) -> bool:
        self.is_busy = False
        self.cancelled_signal.emit()
        return True


def source(url: str, sid: int) -> dict:
    return {
        "id": sid, "url": url, "site": "test", "enabled": 1,
        "auto_disabled": False, "auto_disabled_reason": None,
        "last_synced": None, "file_count": 0,
    }


def profile(pid: int, name: str, urls: list[dict]) -> dict:
    return {
        "id": pid, "display_name": name, "folder_path": f"X:/{name}",
        "urls": urls,
    }


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_global_order_and_aggregation() -> None:
    bridge = FakeBridge()
    queue = DownloadQueue(bridge)
    finished: list[tuple[str, object]] = []
    ended: list[object] = []
    progress: list[tuple[int, int]] = []
    active: list[int | None] = []
    queue.profile_finished.connect(
        lambda item, summary: finished.append((item["display_name"], summary))
    )
    queue.queue_finished.connect(ended.append)
    queue.job_progress.connect(lambda done, total: progress.append((done, total)))
    queue.active_profile_changed.connect(active.append)
    rows = [
        profile(1, "A", [
            source("https://pawchive.pw/patreon/user/1", 11),
            source("https://www.patreon.com/creator-a", 12),
        ]),
        profile(2, "B", [
            source("https://www.pixiv.net/users/2", 21),
            source("https://www.patreon.com/creator-b", 22),
        ]),
    ]
    asked: list[str] = []

    def params_for(item: dict) -> dict:
        asked.append(item["display_name"])
        return {"ext_filter": {f".{item['display_name'].lower()}"}}

    check(queue.start(rows, params_for=params_for), "la cola no arrancó")
    for amount in (1, 2, 3, 4):
        bridge.finish(DownloadSummary(downloaded=amount))

    expected = [
        ("A", SourceCategory.PAID_OFFICIAL),
        ("B", SourceCategory.PAID_OFFICIAL),
        ("B", SourceCategory.FREE_OFFICIAL),
        ("A", SourceCategory.REPOSITORY),
    ]
    check([(name, category) for name, category, _, _ in bridge.starts] == expected,
          f"orden incorrecto: {bridge.starts}")
    check([final for _, _, final, _ in bridge.starts] == [False, False, True, True],
          "last_checked no quedó reservado para la última pasada")
    check(asked == ["A", "B", "B", "A"], "params_for no siguió los trabajos")
    check([name for name, _ in finished] == ["B", "A"],
          f"cada perfil debe cerrar una sola vez: {finished}")
    check(finished[0][1].downloaded == 5 and finished[1][1].downloaded == 5,
          "los resúmenes parciales no se agregaron por perfil")
    check(len(ended) == 1 and ended[0].done == 2 and ended[0].downloaded == 10,
          "el resumen final no contó perfiles completos")
    check(progress[0] == (0, 4), f"el total de trabajos no es exacto: {progress}")
    check(progress[-1] == (4, 4), f"la barra no terminó completa: {progress}")
    check(active == [1, 2, 2, 1, None],
          f"el perfil visualmente activo no siguió el batch: {active}")


def test_failure_isolation() -> None:
    bridge = FakeBridge()
    queue = DownloadQueue(bridge)
    ended: list[object] = []
    profiles: list[tuple[str, object]] = []
    queue.profile_finished.connect(
        lambda item, summary: profiles.append((item["display_name"], summary))
    )
    queue.queue_finished.connect(ended.append)
    rows = [
        profile(1, "A", [
            source("https://www.patreon.com/a", 11),
            source("https://pawchive.pw/patreon/user/1", 12),
        ]),
        profile(2, "B", [source("https://www.patreon.com/b", 21)]),
    ]
    check(queue.start(rows), "la cola de fallo no arrancó")
    bridge.fail("auth denegada")
    bridge.finish(DownloadSummary(downloaded=2))
    bridge.finish(DownloadSummary(downloaded=3))
    check([name for name, *_ in bridge.starts] == ["A", "B", "A"],
          "un fallo detuvo las pasadas restantes")
    check(len(profiles) == 2 and profiles[0] == ("B", profiles[0][1]),
          "los perfiles no cerraron de forma independiente")
    check(profiles[0][1] is not None and profiles[1] == ("A", None),
          "el perfil fallido no conservó su marca")
    check(len(ended) == 1 and ended[0].done == 1 and len(ended[0].failed) == 1,
          "el fallo no llegó al resumen final")


def test_cancellation_announces_touched_profiles() -> None:
    bridge = FakeBridge()
    queue = DownloadQueue(bridge)
    finished: list[str] = []
    cancelled: list[object] = []
    queue.profile_finished.connect(lambda item, _: finished.append(item["display_name"]))
    queue.queue_cancelled.connect(cancelled.append)
    rows = [
        profile(1, "A", [
            source("https://www.patreon.com/a", 11),
            source("https://pawchive.pw/patreon/user/1", 12),
        ]),
        profile(2, "B", [source("https://www.patreon.com/b", 21)]),
    ]
    check(queue.start(rows), "la cola cancelable no arrancó")
    bridge.finish(DownloadSummary(downloaded=1))
    check(queue.cancel(), "cancel() no aceptó la operación")
    check(finished == ["A", "B"], f"no se anunciaron perfiles tocados: {finished}")
    check(len(cancelled) == 1 and cancelled[0].downloaded == 1,
          "se perdió el resumen parcial al cancelar")


def test_auth_single_flight() -> None:
    async def run() -> None:
        first = DownloadBridge(object())
        second = DownloadBridge(object())
        prompts: list[tuple[DownloadBridge, int]] = []
        first.auth_required.connect(lambda _, token: prompts.append((first, token)))
        second.auth_required.connect(lambda _, token: prompts.append((second, token)))
        one = asyncio.create_task(first._resolve_auth("pixiv"))
        await asyncio.sleep(0)
        two = asyncio.create_task(second._resolve_auth("pixiv"))
        await asyncio.sleep(0)
        check(len(prompts) == 1, f"se abrieron {len(prompts)} logins para el mismo sitio")
        owner, token = prompts[0]
        owner._set_auth_result(token, True)
        check(await asyncio.gather(one, two) == [True, True],
              "los dos trabajos no compartieron el resultado del login")

    asyncio.run(run())


def test_source_failure_stays_inside_profile() -> None:
    class BrokenTemplate:
        name = "broken"
        download_enabled = True

        async def get_artist_info(self, _url: str):
            raise RuntimeError("fuente caída")

    async def run() -> None:
        original = download_service.get_template
        events: list[object] = []
        try:
            download_service.get_template = lambda _url, _engine: BrokenTemplate()
            with tempfile.TemporaryDirectory() as folder:
                item = profile(
                    99, "Rota", [source("https://example.invalid/a", 991)]
                )
                item["folder_path"] = folder
                summary = await download_service.run_profile_download(
                    item,
                    workers=1,
                    ext_filter=set(),
                    source_categories={SourceCategory.UNKNOWN},
                    finalize_profile=False,
                    emit=events.append,
                )
            check(summary.errors == 0, "el fallo de fuente contaminó el resumen")
            check(any(
                isinstance(event, download_service.Log)
                and "Error resolviendo fuente" in event.msg
                for event in events
            ), "el fallo aislado no se informó")
        finally:
            download_service.get_template = original

    asyncio.run(run())


def test_automatic_source_availability() -> None:
    class BrokenTemplate:
        name = "broken"
        download_enabled = True

        async def get_artist_info(self, _url: str):
            raise RuntimeError("servicio sin respuesta")

    class HealthyTemplate(SiteTemplate):
        name = "healthy"

        @classmethod
        def can_handle(cls, _url: str) -> bool:
            return True

        async def get_artist_info(self, url: str) -> ArtistInfo:
            return ArtistInfo("artist-1", "Disponible", "test", self.name, url)

        async def iter_files(self, _artist, since=None):
            if False:
                yield

    class SubscriptionDeniedTemplate:
        name = "patreon"
        download_enabled = True

        def __init__(self) -> None:
            self.calls = 0

        async def get_artist_info(self, _url: str):
            self.calls += 1
            raise NeedsManualAuth("sesión válida, pero sin suscripción")

    async def run() -> None:
        original_template = download_service.get_template
        original_db = download_service.INDEX_DB
        try:
            with tempfile.TemporaryDirectory() as root:
                root_path = Path(root)
                db_path = root_path / "index.db"
                folder = root_path / "profile"
                await init_index(db_path)
                pid = await create_profile(db_path, "A", folder, "test")
                sid = await add_profile_url(
                    db_path, pid, "https://example.invalid/a", "test"
                )
                download_service.INDEX_DB = db_path

                download_service.get_template = lambda _url, _engine: BrokenTemplate()
                item = await get_profile(db_path, pid)
                await download_service.run_profile_download(
                    item,
                    workers=1,
                    ext_filter=set(),
                    validate_sources=True,
                    scan_only=True,
                    emit=lambda _event: None,
                )
                disabled = await get_profile(db_path, pid)
                state = disabled["urls"][0]
                check(not state["enabled"] and state["auto_disabled"],
                      "el batch no desactivó la fuente sin respuesta")
                check("sin respuesta" in state["auto_disabled_reason"],
                      "no se conservó el motivo de la desactivación")

                await set_profile_url_enabled(db_path, sid, True)
                manual_active = await get_profile(db_path, pid)
                check(manual_active["urls"][0]["enabled"],
                      "el usuario no pudo reactivar la fuente")
                check(not manual_active["urls"][0]["auto_disabled"],
                      "la reactivación manual conservó el veto automático")

                denied_template = SubscriptionDeniedTemplate()
                download_service.get_template = lambda _url, _engine: denied_template
                await download_service.run_profile_download(
                    manual_active,
                    workers=1,
                    ext_filter=set(),
                    validate_sources=True,
                    scan_only=True,
                    resolve_auth=lambda _site: asyncio.sleep(0, result=True),
                    emit=lambda _event: None,
                )
                disabled = await get_profile(db_path, pid)
                check(denied_template.calls == 2,
                      "no se volvió a comprobar la fuente tras renovar sesión")
                check(disabled["urls"][0]["auto_disabled"],
                      "la falta de suscripción no desactivó la fuente")

                bridge = FakeBridge()
                queue = DownloadQueue(bridge)
                check(queue.start([disabled]), "Sync simple no aceptó el perfil")
                check(not bridge.starts,
                      "Sync simple volvió a intentar una fuente auto-desactivada")

                download_service.get_template = (
                    lambda _url, engine: HealthyTemplate(engine)
                )
                await download_service.run_profile_download(
                    disabled,
                    workers=1,
                    ext_filter=set(),
                    force_full=True,
                    scan_only=True,
                    emit=lambda _event: None,
                )
                recovered = await get_profile(db_path, pid)
                state = recovered["urls"][0]
                check(state["enabled"] and not state["auto_disabled"],
                      "Rescan completo no reactivó la fuente recuperada")
                check(state["auto_disabled_reason"] is None,
                      "la recuperación dejó un motivo obsoleto")

                await set_profile_url_enabled(db_path, sid, False)
                manual = await get_profile(db_path, pid)
                check(not manual["urls"][0]["auto_disabled"],
                      "la desactivación manual se confundió con la automática")
                bridge = FakeBridge()
                queue = DownloadQueue(bridge)
                check(queue.start([manual], force_full=True),
                      "Rescan no aceptó un perfil sin fuentes elegibles")
                check(not bridge.starts,
                      "Rescan intentó una fuente desactivada manualmente")
        finally:
            download_service.get_template = original_template
            download_service.INDEX_DB = original_db

    asyncio.run(run())


if __name__ == "__main__":
    test_global_order_and_aggregation()
    test_failure_isolation()
    test_cancellation_announces_touched_profiles()
    test_auth_single_flight()
    test_source_failure_stays_inside_profile()
    test_automatic_source_availability()
    print("O3 planner: 36/36 comprobaciones")
