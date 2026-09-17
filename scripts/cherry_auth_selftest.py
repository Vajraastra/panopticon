"""Regresiones de autenticación Patreon/Pixiv, sin red ni sesiones reales."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from third_party.cherry_dl.auth import patreon as patreon_auth  # noqa: E402
from third_party.cherry_dl.auth import pixiv as pixiv_auth  # noqa: E402
from third_party.cherry_dl.auth.patreon import NeedsManualAuth  # noqa: E402
from third_party.cherry_dl.auth.pixiv import NeedsPixivAuth  # noqa: E402
from third_party.cherry_dl.config import UserConfig  # noqa: E402
from third_party.cherry_dl.templates.base import ArtistInfo, FileInfo  # noqa: E402
from third_party.cherry_dl.templates.patreon import PatreonTemplate  # noqa: E402
from third_party.cherry_dl.templates.pixiv import PixivTemplate  # noqa: E402
import third_party.cherry_dl.templates.patreon as patreon_template  # noqa: E402
import third_party.cherry_dl.templates.pixiv as pixiv_template  # noqa: E402
import third_party.cherry_dl.download_service as service  # noqa: E402
import third_party.cherry_dl.auth.browser_login as browser_login  # noqa: E402
from third_party.cherry_dl.auth.browser_login import _wanted_cookies  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

_ok = 0
_fail = 0


def check(label: str, condition: bool) -> None:
    global _ok, _fail
    if condition:
        _ok += 1
        print(f"  ✓ {label}")
    else:
        _fail += 1
        print(f"  ✗ {label}")


def response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", "https://example.test"))


def test_browser_compatibility_order() -> None:
    print("\n[-2] El login prioriza navegadores sin bloqueadores integrados")
    names = [name for name, _path in browser_login._windows_candidates()]
    first = {name: names.index(name) for name in ("edge", "chrome", "brave")}
    check("Edge y Chrome se prueban antes que Brave",
          first["edge"] < first["brave"] and first["chrome"] < first["brave"])


async def test_content_encoding_negotiation() -> None:
    print("\n[-1] HTTPX negocia únicamente compresiones que puede decodificar")
    check("Pixiv no fuerza Brotli", "Accept-Encoding" not in pixiv_template._API_HEADERS)
    check("Patreon no fuerza Brotli", "Accept-Encoding" not in patreon_template._BASE_HEADERS)

    async with httpx.AsyncClient(headers=pixiv_template._API_HEADERS) as client:
        request = client.build_request("GET", "https://example.test")
    check("HTTPX aporta su Accept-Encoding compatible", bool(request.headers.get("accept-encoding")))


async def test_stale_browser_cookie() -> None:
    print("\n[0] Una cookie anterior no puede cerrar el navegador de login")

    class Cookie:
        def __init__(self, name, value, domain):
            self.name = name
            self.value = value
            self.domain = domain

    old = _wanted_cookies(
        [Cookie("PHPSESSID", "stale", ".pixiv.net")],
        "pixiv.net",
        {"PHPSESSID"},
    )
    renewed = _wanted_cookies(
        [Cookie("PHPSESSID", "fresh", ".pixiv.net")],
        "pixiv.net",
        {"PHPSESSID"},
    )
    wrong_domain = _wanted_cookies(
        [Cookie("PHPSESSID", "foreign", ".example.net")],
        "pixiv.net",
        {"PHPSESSID"},
    )
    check("detecta el valor previo", old.get("PHPSESSID") == "stale")
    check("distingue una sesión renovada", renewed.get("PHPSESSID") != old.get("PHPSESSID"))
    check("no acepta una cookie de otro dominio", not wrong_domain)

    class CookieJar:
        def __init__(self):
            self.calls = 0

        async def get_all(self):
            self.calls += 1
            value = "stale" if self.calls < 3 else "fresh"
            return [Cookie("PHPSESSID", value, ".pixiv.net")]

    class Browser:
        def __init__(self):
            self.cookies = CookieJar()
            self.stopped = False

        async def get(self, _url):
            return None

        def stop(self):
            self.stopped = True

    browser = Browser()

    async def fake_start(**_kwargs):
        return browser

    async def no_wait(_seconds):
        return None

    statuses = []
    old_nodriver = sys.modules.get("nodriver")
    old_find = browser_login.find_browser
    old_sleep = browser_login.asyncio.sleep
    sys.modules["nodriver"] = SimpleNamespace(start=fake_start)
    browser_login.find_browser = lambda: ("test", "browser.exe")
    browser_login.asyncio.sleep = no_wait
    validations = []

    async def validate(cookies):
        validations.append(cookies.get("PHPSESSID"))
        return len(validations) > 1

    try:
        captured = await browser_login.capture_cookies(
            "https://www.pixiv.net/login.php",
            "pixiv.net",
            {"PHPSESSID"},
            require="PHPSESSID",
            timeout=12,
            on_status=statuses.append,
            require_fresh=True,
            validate=validate,
        )
    finally:
        browser_login.find_browser = old_find
        browser_login.asyncio.sleep = old_sleep
        if old_nodriver is None:
            sys.modules.pop("nodriver", None)
        else:
            sys.modules["nodriver"] = old_nodriver

    check("ignora la cookie previa y espera confirmación", browser.cookies.calls == 4)
    check("no acepta sólo el cambio de cookie", validations == ["fresh", "fresh"])
    check("solo devuelve la cookie nueva",
          bool(captured and captured.get("PHPSESSID") == "fresh"))
    check("explica que espera una renovación",
          any("sesión anterior" in message for message in statuses))
    check("cierra el navegador después de capturar la nueva", browser.stopped)


async def test_pixiv_rejected_cookie() -> None:
    print("\n[1] Pixiv no reutiliza ni vuelve a guardar una cookie rechazada")
    import third_party.cherry_dl.templates.pixiv as pixiv_template

    template = PixivTemplate(None)
    template._http = httpx.AsyncClient(cookies={"PHPSESSID": "expired"})
    template._auth_invalid = False
    cleared = []
    saved = []
    old_clear = pixiv_auth.clear_pixiv_session
    old_save = pixiv_template.save_pixiv_cookies
    pixiv_auth.clear_pixiv_session = lambda: cleared.append(True)
    pixiv_template.save_pixiv_cookies = lambda cookies: saved.append(dict(cookies))
    try:
        try:
            template._check_response(response(403))
        except NeedsPixivAuth:
            raised = True
        else:
            raised = False
        await template._close_client()
    finally:
        pixiv_auth.clear_pixiv_session = old_clear
        pixiv_template.save_pixiv_cookies = old_save

    check("403 se convierte en NeedsPixivAuth", raised)
    check("la sesión persistida se elimina", bool(cleared))
    check("el cierre no resucita PHPSESSID", not saved)
    check("el cliente rechazado queda liberado", template._http is None)


async def test_guided_pixiv_uses_server_validation() -> None:
    print("\n[1a] El navegador espera confirmación real de Pixiv")
    options = {}
    saved = []
    old_capture = browser_login.capture_cookies
    old_save = pixiv_auth.save_pixiv_cookies

    async def fake_capture(**kwargs):
        options.update(kwargs)
        return {"PHPSESSID": "confirmed"}

    browser_login.capture_cookies = fake_capture
    pixiv_auth.save_pixiv_cookies = lambda cookies: saved.append(dict(cookies))
    try:
        result = await pixiv_auth.guided_login_pixiv()
    finally:
        browser_login.capture_cookies = old_capture
        pixiv_auth.save_pixiv_cookies = old_save

    check("el login entrega un validador al capturador", callable(options.get("validate")))
    check("abre la página actual de accounts.pixiv.net",
          options.get("login_url", "").startswith("https://accounts.pixiv.net/login?"))
    check("la validación sustituye la heurística de cookie nueva",
          not options.get("require_fresh", False) and result == {"PHPSESSID": "confirmed"})


async def test_pixiv_requires_authenticated_user() -> None:
    print("\n[1b] Pixiv distingue una cookie anónima de un usuario autenticado")

    async def anonymous(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=httpx.Request("GET", "https://example.test"))

    template = PixivTemplate(None)
    cleared = []
    old_clear = pixiv_auth.clear_pixiv_session
    pixiv_auth.clear_pixiv_session = lambda: cleared.append(True)
    client = httpx.AsyncClient(transport=httpx.MockTransport(anonymous))
    try:
        try:
            await template._validate_authenticated_client(client)
        except NeedsPixivAuth:
            rejected = True
        else:
            rejected = False
    finally:
        await client.aclose()
        pixiv_auth.clear_pixiv_session = old_clear

    check("un PHPSESSID anónimo solicita login", rejected)
    check("la cookie anónima se elimina", bool(cleared))

    async def authenticated(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": False, "body": {"userId": "123"}},
            request=httpx.Request("GET", "https://example.test"),
        )

    valid_template = PixivTemplate(None)
    valid_client = httpx.AsyncClient(transport=httpx.MockTransport(authenticated))
    try:
        await valid_template._validate_authenticated_client(valid_client)
        accepted = True
    except NeedsPixivAuth:
        accepted = False
    finally:
        await valid_client.aclose()
    check("un usuario reconocido puede continuar", accepted)


async def test_patreon_rejected_cookie() -> None:
    print("\n[2] Patreon también entra al flujo común de reautenticación")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return response(403)

    template = PatreonTemplate(None)
    template._http = httpx.AsyncClient(
        cookies={"session_id": "expired"}, transport=httpx.MockTransport(handler),
    )
    template._auth_invalid = False
    cleared = []
    refreshed = []
    old_clear = patreon_auth.clear_patreon_session
    import third_party.cherry_dl.templates.patreon as patreon_template
    old_refresh = patreon_template.refresh_patreon_cookies
    old_uniform = patreon_template.random.uniform
    patreon_auth.clear_patreon_session = lambda: cleared.append(True)
    patreon_template.refresh_patreon_cookies = lambda cookies: refreshed.append(dict(cookies))
    patreon_template.random.uniform = lambda *_args: 0
    try:
        try:
            await template.get_artist_info("https://www.patreon.com/example")
        except NeedsManualAuth:
            raised = True
        else:
            raised = False
        await template._close_client()
    finally:
        patreon_auth.clear_patreon_session = old_clear
        patreon_template.refresh_patreon_cookies = old_refresh
        patreon_template.random.uniform = old_uniform

    check("401/403 se convierte en NeedsManualAuth", raised)
    check("la sesión persistida se elimina", bool(cleared))
    check("el cierre no resucita session_id", not refreshed)
    check("el cliente rechazado queda liberado", template._http is None)


async def test_mid_scan_reauth() -> None:
    print("\n[3] Una sesión Pixiv expirada durante el paginado se renueva y retoma")

    class FakePixiv:
        name = "pixiv"
        max_workers = 1
        download_enabled = True
        calls = 0

        def __init__(self):
            self.discards = 0

        async def discard_auth_client(self):
            self.discards += 1

        async def get_artist_info(self, url):
            return ArtistInfo("42", "Pixiv Test", "pixiv", "pixiv", url)

        async def iter_files(self, _artist, since=None):
            self.calls += 1
            yield FileInfo(
                "https://i.pximg.net/one.jpg", "one.jpg", "42", "Pixiv Test", "1",
                url_source="pixiv://illust/1/p0",
            )
            if self.calls == 1:
                raise NeedsPixivAuth("sesión vencida a mitad del scan")
            yield FileInfo(
                "https://i.pximg.net/two.jpg", "two.jpg", "42", "Pixiv Test", "2",
                url_source="pixiv://illust/2/p0",
            )

    fake = FakePixiv()
    resolved = []
    events = []
    old_find = service.find_template
    old_get = service.get_template
    old_db = service.INDEX_DB

    async def resolve(site: str) -> bool:
        resolved.append(site)
        return True

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        service.find_template = lambda _url: FakePixiv
        service.get_template = lambda _url, _engine: fake
        service.INDEX_DB = root / "index.db"
        profile = {
            "id": -1,
            "folder_path": str(root / "artist"),
            "urls": [{
                "id": -1,
                "url": "https://www.pixiv.net/users/42",
                "site": "pixiv",
                "enabled": 1,
                "last_synced": None,
                "file_count": 0,
            }],
        }
        try:
            await service.run_profile_download(
                profile,
                workers=1,
                ext_filter=set(),
                scan_only=True,
                emit=events.append,
                config=UserConfig(download_dir=str(root)),
                resolve_auth=resolve,
            )
            pending = await service.pending_count(root / "artist", -1)
        finally:
            service.find_template = old_find
            service.get_template = old_get
            service.INDEX_DB = old_db

    messages = [ev.msg for ev in events if isinstance(ev, service.Log)]
    check("el resolver recibe el sitio Pixiv", resolved == ["pixiv"])
    check("se descarta el cliente anterior", fake.discards == 1)
    check("el escaneo se reinicia una sola vez", fake.calls == 2)
    check("no duplica lo ya visto y conserva ambos archivos", pending == 2)
    check("la UI recibe el aviso de reanudación",
          any("retomando el escaneo" in msg for msg in messages))


async def test_initial_pixiv_reauth_forces_full_scan() -> None:
    print("\n[3b] Renovar Pixiv recupera obras ocultas por un scan anónimo")

    class FakePixiv:
        name = "pixiv"
        max_workers = 1
        download_enabled = True

        def __init__(self):
            self.artist_calls = 0
            self.scan_since = []

        async def discard_auth_client(self):
            return None

        async def get_artist_info(self, url):
            self.artist_calls += 1
            if self.artist_calls == 1:
                raise NeedsPixivAuth("cookie anónima")
            return ArtistInfo("42", "Pixiv Test", "pixiv", "pixiv", url)

        async def iter_files(self, _artist, since=None):
            self.scan_since.append(since)
            yield FileInfo(
                "https://i.pximg.net/restricted.jpg",
                "restricted.jpg",
                "42",
                "Pixiv Test",
                "restricted",
                url_source="pixiv://illust/restricted/p0",
            )

    fake = FakePixiv()
    resolved = []
    events = []
    old_find = service.find_template
    old_get = service.get_template
    old_db = service.INDEX_DB

    async def resolve(site: str) -> bool:
        resolved.append(site)
        return True

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        folder = root / "artist"
        await service.init_catalog(folder)
        await service.set_meta_int(folder, "full_scan_done", 1)
        service.find_template = lambda _url: FakePixiv
        service.get_template = lambda _url, _engine: fake
        service.INDEX_DB = root / "index.db"
        profile = {
            "id": -2,
            "folder_path": str(folder),
            "urls": [{
                "id": -2,
                "url": "https://www.pixiv.net/users/42",
                "site": "pixiv",
                "enabled": 1,
                "last_synced": "2026-09-08 02:04:39",
                "file_count": 1438,
            }],
        }
        try:
            await service.run_profile_download(
                profile,
                workers=1,
                ext_filter=set(),
                scan_only=True,
                emit=events.append,
                config=UserConfig(download_dir=str(root)),
                resolve_auth=resolve,
            )
            pending = await service.pending_count(folder, -2)
        finally:
            service.find_template = old_find
            service.get_template = old_get
            service.INDEX_DB = old_db

    messages = [ev.msg for ev in events if isinstance(ev, service.Log)]
    check("la sesión se renueva antes del scan", resolved == ["pixiv"])
    check("ignora last_synced y revisa todo el historial", fake.scan_since == [None])
    check("la obra antes oculta entra en la cola", pending == 1)
    check("la UI explica el rescan de recuperación",
          any("historial completo" in msg for msg in messages))


async def main() -> int:
    test_browser_compatibility_order()
    await test_content_encoding_negotiation()
    await test_patreon_rejected_cookie()
    await test_mid_scan_reauth()
    await test_initial_pixiv_reauth_forces_full_scan()
    print(f"\n{_ok} ok, {_fail} fallo(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
