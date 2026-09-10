"""
Login guiado por el navegador real del usuario (vía CDP / nodriver).

Por qué: en Windows el cifrado App-Bound de Brave/Chrome impide leer las cookies
del navegador con browser_cookie3 (requiere admin). Y los logins con Google
bloquean los navegadores embebidos/automatizados. La solución que esquiva ambos
problemas es abrir el navegador REAL del usuario (Brave/Edge/Chrome) con un perfil
dedicado, dejar que se loguee normalmente, y leer la cookie por el protocolo de
depuración (CDP) — sin descifrar archivos ni descargar un navegador.

Flujo no técnico: el usuario hace clic, se loguea, listo.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..config import CHERRY_DIR

# Perfil dedicado y persistente → el login sobrevive entre sesiones.
_PROFILE_DIR = CHERRY_DIR / "browser-profile"


# ── Detección del navegador instalado (cross-OS) ───────────────────────────────

def _windows_candidates() -> list[tuple[str, str]]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    out = []
    # Los callbacks de login cruzan subdominios. Edge/Chrome con un perfil
    # dedicado parten sin bloqueadores; Brave Shields puede cortar ese retorno.
    for name, sub in [
        ("edge",  r"Microsoft\Edge\Application\msedge.exe"),
        ("chrome", r"Google\Chrome\Application\chrome.exe"),
        ("brave", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]:
        for root in (pf, pfx86, local):
            if root:
                out.append((name, str(Path(root) / sub)))
    return out


def find_browser() -> tuple[str, str] | None:
    """Devuelve (nombre, ruta_ejecutable) del primer navegador Chromium hallado."""
    import sys

    if sys.platform == "win32":
        for name, path in _windows_candidates():
            if Path(path).is_file():
                return name, path
        return None

    if sys.platform == "darwin":
        mac = [
            ("brave", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
            ("edge",  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ("chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
        for name, path in mac:
            if Path(path).is_file():
                return name, path
        return None

    # Linux / otros: buscar en PATH
    for name, exe in [
        ("brave", "brave-browser"), ("brave", "brave"),
        ("chrome", "google-chrome"), ("chrome", "google-chrome-stable"),
        ("chromium", "chromium"), ("chromium", "chromium-browser"),
        ("edge", "microsoft-edge"),
    ]:
        found = shutil.which(exe)
        if found:
            return name, found
    return None


# ── Captura de cookies por CDP ─────────────────────────────────────────────────

async def capture_cookies(
    login_url: str,
    domain: str,
    wanted: set[str],
    *,
    require: str,
    timeout: int = 240,
    on_status=None,
    require_fresh: bool = False,
    validate=None,
) -> dict[str, str] | None:
    """
    Abre el navegador real en `login_url`, espera a que el usuario se loguee y
    devuelve las cookies de `wanted` presentes en `domain`. Termina en cuanto
    aparece la cookie `require` (o None si vence `timeout`). Si `require_fresh`
    está activo, una cookie que ya existía al abrir no cuenta: espera a que el
    login genere un valor nuevo. Si `validate` está definido, la cookie sólo
    se acepta cuando `await validate(cookies)` confirma la sesión.

    `on_status(msg)` opcional: callback para reportar progreso a la UI/CLI.
    """
    import nodriver as uc

    def _say(msg: str) -> None:
        if on_status:
            on_status(msg)

    info = find_browser()
    if info is None:
        _say("No se encontró Brave/Edge/Chrome instalado.")
        return None
    browser_name, exe = info
    _say(f"Abriendo {browser_name}…")

    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    browser = await uc.start(
        browser_executable_path=exe,
        user_data_dir=str(_PROFILE_DIR),
        headless=False,
    )
    try:
        previous_required = None
        if require_fresh:
            try:
                before = await browser.cookies.get_all()
                previous_required = _wanted_cookies(before, domain, wanted).get(require)
            except Exception:
                pass

        await browser.get(login_url)
        _say("Iniciá sesión en la ventana del navegador…")
        stale_reported = False
        invalid_reported = False
        for i in range(timeout // 3):
            await asyncio.sleep(3)
            try:
                cookies = await browser.cookies.get_all()
            except Exception:
                continue
            found = _wanted_cookies(cookies, domain, wanted)
            required_value = found.get(require)
            if required_value and (
                not require_fresh or required_value != previous_required
            ):
                if validate is not None:
                    try:
                        valid = await validate(found)
                    except Exception:
                        valid = False
                    if not valid:
                        if not invalid_reported:
                            _say("Cookie detectada; esperando que el sitio confirme el login…")
                            invalid_reported = True
                        continue
                _say("Sesión capturada.")
                return found
            if required_value and not stale_reported:
                _say("La sesión anterior no cuenta; completa el login para renovarla.")
                stale_reported = True
        _say("Tiempo agotado sin detectar la sesión.")
        return None
    finally:
        try:
            browser.stop()
        except Exception:
            pass


def _wanted_cookies(cookies, domain: str, wanted: set[str]) -> dict[str, str]:
    """Filtra las cookies CDP del dominio sin depender de clases de nodriver."""
    found = {}
    for cookie in cookies:
        name = getattr(cookie, "name", None)
        cookie_domain = getattr(cookie, "domain", "") or ""
        if name in wanted and domain in cookie_domain:
            found[name] = getattr(cookie, "value", "")
    return found


async def capture_oauth_code(
    login_url: str,
    *,
    timeout: int = 300,
    on_status=None,
) -> str | None:
    """Abre un OAuth PKCE de Pixiv y captura el código del callback CDP."""
    import nodriver as uc

    def _say(msg: str) -> None:
        if on_status:
            on_status(msg)

    info = find_browser()
    if info is None:
        _say("No se encontró Edge/Chrome/Brave instalado.")
        return None
    browser_name, exe = info
    _say(f"Abriendo {browser_name} para autenticar Pixiv…")

    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    browser = await uc.start(
        browser_executable_path=exe,
        user_data_dir=str(_PROFILE_DIR),
        headless=False,
    )
    captured: dict[str, str] = {}

    def _on_request(event) -> None:
        url = getattr(getattr(event, "request", None), "url", "") or ""
        if not (
            url.startswith("pixiv://")
            or "/web/v1/users/auth/pixiv/callback" in url
        ):
            return
        code = (parse_qs(urlsplit(url).query).get("code") or [""])[0]
        if code:
            captured["code"] = code

    try:
        tab = await browser.get("about:blank")
        tab.add_handler(uc.cdp.network.RequestWillBeSent, _on_request)
        await tab.send(uc.cdp.network.enable())
        await tab.get(login_url)
        _say("Inicia sesión en Pixiv; esta ventana se cerrará al autorizar.")
        for _ in range(timeout):
            if captured.get("code"):
                _say("Pixiv autorizó la sesión.")
                return captured["code"]
            await asyncio.sleep(1)
        _say("Tiempo agotado sin recibir autorización de Pixiv.")
        return None
    finally:
        try:
            browser.stop()
        except Exception:
            pass


async def wait_for_pixiv_content_settings(
    settings_url: str,
    *,
    is_enabled,
    timeout: int = 300,
    on_status=None,
) -> bool:
    """Abre ajustes web y espera a que la App API confirme acceso R-18."""
    import nodriver as uc

    def _say(msg: str) -> None:
        if on_status:
            on_status(msg)

    info = find_browser()
    if info is None:
        _say("No se encontró Edge/Chrome/Brave instalado.")
        return False
    browser_name, exe = info
    _say(f"Abriendo ajustes de Pixiv en {browser_name}…")
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    browser = await uc.start(
        browser_executable_path=exe,
        user_data_dir=str(_PROFILE_DIR),
        headless=False,
    )
    try:
        await browser.get(settings_url)
        _say("Activa Contenido explícito (R-18) y guarda el ajuste.")
        for _ in range(timeout // 2):
            try:
                if int(await is_enabled()) >= 1:
                    _say("Pixiv confirmó el acceso a obras R-18.")
                    return True
            except Exception:
                pass
            await asyncio.sleep(2)
        _say("Tiempo agotado sin habilitar las obras R-18.")
        return False
    finally:
        try:
            browser.stop()
        except Exception:
            pass
