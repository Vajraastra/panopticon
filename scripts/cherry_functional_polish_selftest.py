"""Regresión sin red para la pulida funcional de Cherry-DL."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

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
            check(defaults.pawchive.downloads_per_window == 500,
                  "la cuota recomendada debe ser 500")
            check(defaults.pawchive.resume_headroom == 50,
                  "la recuperación recomendada debe acumular 50 slots")
            check(defaults.pawchive.gigabytes_per_window == 25.0,
                  "la cuota recomendada debe ser 25 GB")
            check(defaults.pawchive.window_hours == 2.0,
                  "la ventana recomendada debe ser de 2 horas")
            custom = defaults.model_copy(update={
                "pawchive": config.PawchiveConfig(
                    downloads_per_window=425, gigabytes_per_window=18.5,
                    window_hours=4.5, resume_headroom=40,
                ),
            })
            config.save_config(custom)
            loaded = config.load_config()
            check(loaded.pawchive.downloads_per_window == 425,
                  "la cuota debe sobrevivir al TOML")
            check(loaded.pawchive.resume_headroom == 40,
                  "el margen de reanudación debe sobrevivir al TOML")
            check(loaded.pawchive.gigabytes_per_window == 18.5,
                  "la cuota en GB debe sobrevivir al TOML")
            check(loaded.pawchive.window_hours == 4.5,
                  "la ventana debe sobrevivir al TOML")
            text = config.CONFIG_FILE.read_text(encoding="utf-8")
            check("[pawchive]" in text, "Pawchive debe tener sección propia")
            config.set_config_value("pawchive_resume_headroom", "30")
            check(config.load_config().pawchive.resume_headroom == 30,
                  "el margen se puede cambiar por la API de configuración")
            config.set_config_value("pawchive_downloads_per_window", "20")
            adjusted = config.load_config().pawchive
            check(adjusted.downloads_per_window == 20
                  and adjusted.resume_headroom == 19,
                  "bajar la cuota ajusta el margen para mantenerlo válido")
            config.save_config(custom)

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
            check(dialog._pawchive_headroom.value() == 85,
                  "Ajustes muestra el margen efectivo mínimo de 20% para 425 archivos")
            check(dialog._pawchive_gigabytes.value() == 18.5,
                  "Ajustes debe mostrar la cuota persistida en GB")
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
            panel._on_file_progress(7, 12)
            check(panel._file_bar.isVisible() and panel._file_bar.value() == 7,
                  "la barra de archivos debe reflejar el avance real")
            panel._on_job_progress(3, 8)
            check(panel._batch_bar.isVisible() and panel._batch_bar.value() == 3,
                  "la barra batch debe contar trabajos programados")
            from third_party.cherry_dl.site_throttle import QuotaSnapshot
            snapshot = QuotaSnapshot(
                "pawchive", 20, 500, 102_606_226, 25 * 1024**3,
                1, 10_800, 9_900,
            )
            panel._set_quota(snapshot, 2**20)
            check("24.903 GB" in panel._quota.text()
                  and "479 files" in panel._quota.text(),
                  "el medidor Pawchive descuenta cuota persistida y bytes en vuelo")
            check("2 h 45 min" in panel._quota.text(),
                  "el medidor muestra cuándo se libera la próxima cuota")
            panel.close()

            view = CherryView()
            view._profiles = [
                {
                    "id": 2, "display_name": "Zeta", "folder_path": "X:/Zeta",
                    "primary_site": "pixiv", "url_count": 1,
                    "last_checked": None, "folder_exists": True,
                    "awaiting_archive_count": 0,
                },
                {
                    "id": 1, "display_name": "Alpha", "folder_path": "X:/Alpha",
                    "primary_site": "patreon", "url_count": 2,
                    "last_checked": None, "folder_exists": True,
                    "awaiting_archive_count": 3,
                },
            ]
            view._rebuild_site_combo()
            view._apply_filters()
            check(view._table.horizontalHeaderItem(0).text() == "Name",
                  "la primera columna debe identificarse como nombre")
            check([p["display_name"] for p in view._profiles_in_table_order()]
                  == ["Alpha", "Zeta"],
                  "la tabla debe iniciar ordenada por nombre aunque haya marcadores")
            view._on_active_profile_changed(2)
            zeta_row = next(
                row for row in range(view._table.rowCount())
                if view._table.item(row, 0).data(Qt.UserRole) == 2
            )
            check(view._table.item(zeta_row, 0).font().bold(),
                  "el perfil activo debe destacar su nombre")
            check(view._table.item(zeta_row, 1).background().color().alpha() > 0,
                  "el perfil activo debe resaltar la fila completa")
            view._on_active_profile_changed(None)
            check(not view._table.item(zeta_row, 0).font().bold(),
                  "el resaltado debe retirarse al terminar el trabajo")
            view.close()

            class BatchRuntimeProbe:
                def __init__(self):
                    self.submitted = []

                @staticmethod
                def start():
                    return True

                def submit(self, operation):
                    self.submitted.append(operation)
                    return "future"

            class BatchProbe:
                _maintenance_busy = False
                _preparing_download = False
                _archive_check_busy = False

                def __init__(self):
                    self._queue = type("Queue", (), {"is_running": False})()
                    self._bridge = type("Bridge", (), {"is_busy": False})()
                    self._runtime = BatchRuntimeProbe()
                    self._btn_console = type(
                        "ConsoleButton", (), {"setChecked": lambda _self, _checked: None}
                    )()
                    self.active = []
                    self.actions_updated = False

                @staticmethod
                def tr(_key, default):
                    return default

                @staticmethod
                def _profiles_in_table_order():
                    return [
                        {"id": 1, "display_name": "Alpha", "folder_exists": True},
                        {"id": 2, "display_name": "Zeta", "folder_exists": True},
                    ]

                @staticmethod
                def _archives_batch_async(profiles):
                    return ("batch", profiles)

                def _on_active_profile_changed(self, profile_id):
                    self.active.append(profile_id)

                def _update_actions(self):
                    self.actions_updated = True

                @staticmethod
                def _on_archives_finished(_folder, _result, _error):
                    raise AssertionError("el arranque batch no debe fallar")

            batch = BatchProbe()
            with patch(
                "modules.cherry.ui.view.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                CherryView._process_archives_batch(batch)
            check(batch.active == [1],
                  "el batch debe resaltar el primer perfil visible antes de iniciar")
            check(batch._runtime.submitted == [
                ("batch", batch._profiles_in_table_order())
            ], "el batch debe enviar todos los perfiles visibles en orden")
            check(batch._maintenance_busy and batch.actions_updated,
                  "el batch debe entrar en estado ocupado y actualizar acciones")

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
