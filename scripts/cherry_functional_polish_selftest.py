"""Regresión sin red para la pulida funcional de Cherry-DL."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from modules.cherry.logic import profile_writes  # noqa: E402
from modules.cherry.ui.download_panel import DownloadPanel  # noqa: E402
from modules.cherry.ui.settings_dialog import SettingsDialog  # noqa: E402
from modules.cherry.ui.view import CherryView  # noqa: E402
from third_party.cherry_dl import config  # noqa: E402
from third_party.cherry_dl.catalog import (  # noqa: E402
    add_pending,
    get_pending_files,
    init_catalog,
)
from third_party.cherry_dl.index import (  # noqa: E402
    add_profile_url,
    create_profile,
    get_or_create_artist,
    get_or_create_site,
    get_profile,
    init_index,
)


async def main() -> None:
    checks = 0

    def check(condition: bool, message: str) -> None:
        nonlocal checks
        assert condition, message
        checks += 1

    with tempfile.TemporaryDirectory(prefix="cherry-polish-") as tmp:
        work = Path(tmp)
        old_dir = config.CHERRY_DIR
        old_file = config.CONFIG_FILE
        old_index = config.INDEX_DB
        try:
            config.CHERRY_DIR = work / "state"
            config.CONFIG_FILE = config.CHERRY_DIR / "config.toml"
            config.INDEX_DB = config.CHERRY_DIR / "index.db"

            defaults = config.UserConfig(download_dir=str(work))
            check(defaults.pawchive.downloads_per_window == 300,
                  "la cuota recomendada debe ser 300")
            check(defaults.pawchive.window_hours == 3.0,
                  "la ventana recomendada debe ser de 3 horas")
            custom = defaults.model_copy(update={
                "pawchive": config.PawchiveConfig(
                    downloads_per_window=425, window_hours=4.5,
                ),
            })
            config.save_config(custom)
            loaded = config.load_config()
            check(loaded.pawchive.downloads_per_window == 425,
                  "la cuota debe sobrevivir al TOML")
            check(loaded.pawchive.window_hours == 4.5,
                  "la ventana debe sobrevivir al TOML")
            text = config.CONFIG_FILE.read_text(encoding="utf-8")
            check("[pawchive]" in text, "Pawchive debe tener sección propia")

            folder = work / "artist"
            folder.mkdir()
            await init_index(config.INDEX_DB)
            profile_id = await create_profile(
                config.INDEX_DB, "Artist", folder, "kemono",
            )
            kemono_url = await add_profile_url(
                config.INDEX_DB, profile_id,
                "https://kemono.cr/patreon/user/7", "kemono", "7",
            )
            pawchive_url = await add_profile_url(
                config.INDEX_DB, profile_id,
                "https://pawchive.pw/patreon/user/7", "pawchive", "7",
            )
            for site in ("kemono", "pawchive"):
                site_id = await get_or_create_site(config.INDEX_DB, site)
                await get_or_create_artist(
                    config.INDEX_DB, site_id, "7", "Artist", folder,
                )

            await init_catalog(folder)
            await add_pending(
                folder, "dead", "https://dead/file", "dead.png",
                profile_url_id=kemono_url,
            )
            await add_pending(
                folder, "live", "https://live/file", "live.png",
                profile_url_id=pawchive_url,
            )
            downloaded = folder / "downloaded.png"
            downloaded.write_bytes(b"already safe")

            removed = await profile_writes.remove_url(
                profile_id, folder, kemono_url,
            )
            check(removed == 1, "debe retirar solo la cola de la fuente borrada")
            check([row["url_source"] for row in await get_pending_files(folder)] == ["live"],
                  "la cola de otras fuentes debe sobrevivir")
            check(downloaded.read_bytes() == b"already safe",
                  "los archivos descargados no se deben tocar")
            profile = await get_profile(config.INDEX_DB, profile_id)
            check([row["site"] for row in profile["urls"]] == ["pawchive"],
                  "la fuente obsoleta debe salir del índice")
            check(profile["primary_site"] == "pawchive",
                  "la fuente restante debe pasar a ser primaria")

            # La fila legacy de artists no debe reconstruir Kemono al reiniciar.
            await init_index(config.INDEX_DB)
            profile = await get_profile(config.INDEX_DB, profile_id)
            check([row["site"] for row in profile["urls"]] == ["pawchive"],
                  "la fuente eliminada no debe reaparecer al inicializar")
            with closing(sqlite3.connect(config.INDEX_DB)) as db:
                legacy = db.execute(
                    """
                    SELECT COUNT(*) FROM artists a
                    JOIN sites s ON s.id = a.site_id
                    WHERE s.name = 'kemono' AND a.artist_id = '7'
                    """
                ).fetchone()[0]
            check(legacy == 0, "debe retirar el artista legacy de Kemono")

            app = QApplication.instance() or QApplication([])
            dialog = SettingsDialog()
            check(dialog._pawchive_downloads.value() == 425,
                  "Ajustes debe mostrar la cuota persistida")
            check(dialog._pawchive_hours.value() == 4.5,
                  "Ajustes debe mostrar la ventana persistida")
            dialog.close()

            panel = DownloadPanel()
            panel.resize(700, 400)
            panel.show()
            for n in range(120):
                panel.append_log(f"event {n}")
            app.processEvents()
            bar = panel._log.verticalScrollBar()
            check(bar.value() == bar.maximum(),
                  "el seguimiento activo debe fijarse al último evento")
            panel._follow_log.setChecked(False)
            bar.setValue(maximum_before := bar.maximum() // 2)
            panel.append_log("event paused")
            app.processEvents()
            check(bar.value() == maximum_before,
                  "pausar el seguimiento debe conservar la posición")
            panel.close()

            class CreatedProbe:
                _selected_id = None

                def __init__(self):
                    self.starts = []
                    self.refreshed = False

                @staticmethod
                def tr(_key, default):
                    return default

                @staticmethod
                def _set_status(_text):
                    return None

                def refresh(self):
                    self.refreshed = True

                def _start(self, profile_ids, scan_only):
                    self.starts.append((profile_ids, scan_only))

            created = CreatedProbe()
            CherryView._on_profile_created(created, 77)
            check(created._selected_id == 77 and created.refreshed,
                  "el alta debe seleccionar y refrescar el proyecto")
            check(created.starts == [([77], True)],
                  "el alta debe escanear sin iniciar descargas")
        finally:
            config.CHERRY_DIR = old_dir
            config.CONFIG_FILE = old_file
            config.INDEX_DB = old_index

    print(f"cherry functional polish: {checks}/{checks}")


if __name__ == "__main__":
    asyncio.run(main())
