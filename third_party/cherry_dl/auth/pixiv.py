"""Autenticación OAuth2 PKCE de Pixiv mediante el navegador del usuario."""

from __future__ import annotations

import time
from base64 import urlsafe_b64encode
from hashlib import sha256
from secrets import token_urlsafe
from urllib.parse import urlencode

import httpx

from ..config import load_session, save_session

_SESSION_KEY = "pixiv"
_LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
_CALLBACK_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
_ACCOUNT_STATE_URL = "https://app-api.pixiv.net/v1/user/me/state"
_VIEWING_SETTINGS_URL = "https://www.pixiv.net/settings/viewing"

# Credenciales públicas de la app oficial, usadas también por gallery-dl/PixivPy.
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
TOKEN_USER_AGENT = "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)"
API_HEADERS = {
    "App-OS": "ios",
    "App-OS-Version": "16.7.2",
    "App-Version": "7.19.1",
    "User-Agent": "PixivIOSApp/7.19.1 (iOS 16.7.2; iPhone12,8)",
    "Referer": "https://app-api.pixiv.net/",
    "Accept-Language": "en-US",
}


class NeedsPixivAuth(Exception):
    """Pixiv necesita un login OAuth interactivo o renovar sus tokens."""


def load_pixiv_tokens() -> dict[str, object] | None:
    data = load_session()
    block = data.get(_SESSION_KEY)
    if not isinstance(block, dict) or not block.get("refresh_token"):
        return None
    return dict(block)


def save_pixiv_tokens(tokens: dict[str, object]) -> None:
    data = load_session()
    block = dict(tokens)
    block["_saved_at"] = int(time.time())
    data[_SESSION_KEY] = block
    save_session(data)


def load_pixiv_cookies() -> dict[str, object] | None:
    """Alias temporal: el bloque Pixiv contiene tokens, no cookies web."""
    return load_pixiv_tokens()


def save_pixiv_cookies(tokens: dict[str, object]) -> None:
    """Alias temporal para callers antiguos."""
    save_pixiv_tokens(tokens)


def clear_pixiv_session() -> None:
    data = load_session()
    data.pop(_SESSION_KEY, None)
    save_session(data)


def generate_pkce() -> tuple[str, str]:
    verifier = token_urlsafe(32)
    digest = sha256(verifier.encode("ascii")).digest()
    challenge = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_login_url(challenge: str) -> str:
    params = {
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "client": "pixiv-android",
    }
    return f"{_LOGIN_URL}?{urlencode(params)}"


def _token_body(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    wrapped = payload.get("response")
    return dict(wrapped) if isinstance(wrapped, dict) else dict(payload)


async def _request_tokens(fields: dict[str, str]) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
        response = await client.post(
            _TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "include_policy": "true",
                **fields,
            },
            headers={
                "User-Agent": TOKEN_USER_AGENT,
                "App-OS": "ios",
                "App-OS-Version": "14.6",
            },
        )
    response.raise_for_status()
    body = _token_body(response.json())
    if not body.get("access_token"):
        raise ValueError("Pixiv no devolvió un access_token")
    expires_in = int(body.get("expires_in") or 3600)
    body["_expires_at"] = int(time.time()) + expires_in
    return body


async def ensure_pixiv_access_token() -> str:
    tokens = load_pixiv_tokens()
    if not tokens:
        raise NeedsPixivAuth("Pixiv necesita autorización OAuth.")

    access_token = str(tokens.get("access_token") or "")
    expires_at = int(tokens.get("_expires_at") or 0)
    if access_token and expires_at > int(time.time()) + 60:
        return access_token

    refresh_token = str(tokens.get("refresh_token") or "")
    try:
        renewed = await _request_tokens({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "get_secure_url": "1",
        })
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        clear_pixiv_session()
        raise NeedsPixivAuth("El acceso OAuth de Pixiv venció.") from exc

    if not renewed.get("refresh_token"):
        renewed["refresh_token"] = refresh_token
    save_pixiv_tokens(renewed)
    return str(renewed["access_token"])


async def pixiv_x_restrict(access_token: str) -> int:
    """Devuelve el nivel adulto habilitado: 0=ninguno, 1=R-18, 2=R-18G."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
        response = await client.get(
            _ACCOUNT_STATE_URL,
            headers={**API_HEADERS, "Authorization": f"Bearer {access_token}"},
        )
    response.raise_for_status()
    body = response.json()
    profile = body.get("profile") if isinstance(body, dict) else None
    if not isinstance(profile, dict) or "x_restrict" not in profile:
        raise ValueError("Pixiv no informó la configuración de contenido adulto")
    return int(profile["x_restrict"])


async def ensure_pixiv_content_access() -> str:
    """Garantiza OAuth y que la cuenta no oculte las obras R-18."""
    access_token = await ensure_pixiv_access_token()
    try:
        restriction = await pixiv_x_restrict(access_token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (400, 401, 403):
            clear_pixiv_session()
            raise NeedsPixivAuth("Pixiv rechazó la sesión OAuth.") from exc
        raise
    if restriction < 1:
        raise NeedsPixivAuth(
            "La cuenta Pixiv oculta obras R-18 en sus ajustes de visualización."
        )
    return access_token


async def ensure_pixiv_session() -> dict[str, object]:
    """Compatibilidad: garantiza OAuth y devuelve el bloque persistido."""
    await ensure_pixiv_access_token()
    return load_pixiv_tokens() or {}


async def guided_login_pixiv(on_status=None) -> dict[str, object] | None:
    from .browser_login import capture_oauth_code, wait_for_pixiv_content_settings

    async def _finish_content_access(
        tokens: dict[str, object], access_token: str,
    ) -> dict[str, object] | None:
        try:
            enabled = await pixiv_x_restrict(access_token) >= 1
        except (httpx.HTTPError, ValueError, TypeError):
            enabled = False
        if enabled:
            return tokens
        if on_status:
            on_status(
                "La cuenta oculta obras R-18; activa “Contenido explícito” "
                "en los ajustes que se abrirán."
            )
        changed = await wait_for_pixiv_content_settings(
            _VIEWING_SETTINGS_URL,
            is_enabled=lambda: pixiv_x_restrict(access_token),
            timeout=300,
            on_status=on_status,
        )
        return tokens if changed else None

    existing = load_pixiv_tokens()
    if existing:
        try:
            access_token = await ensure_pixiv_access_token()
        except NeedsPixivAuth:
            existing = None
        else:
            return await _finish_content_access(
                load_pixiv_tokens() or existing, access_token,
            )

    verifier, challenge = generate_pkce()
    code = await capture_oauth_code(
        build_login_url(challenge),
        timeout=300,
        on_status=on_status,
    )
    if not code:
        return None
    try:
        tokens = await _request_tokens({
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": _CALLBACK_URI,
        })
    except (httpx.HTTPError, ValueError, TypeError):
        if on_status:
            on_status("Pixiv rechazó el código de autorización; vuelve a intentarlo.")
        return None
    save_pixiv_tokens(tokens)
    return await _finish_content_access(tokens, str(tokens["access_token"]))


def load_from_browser() -> None:
    """Compatibilidad: OAuth no puede recuperarse desde cookies del browser."""
    return None
