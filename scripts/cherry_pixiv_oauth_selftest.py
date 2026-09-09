"""Regresiones aisladas del OAuth y App API de Pixiv."""

from __future__ import annotations

import asyncio
import sys
import time
from base64 import urlsafe_b64encode
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from third_party.cherry_dl.auth import browser_login, pixiv as auth  # noqa: E402
from third_party.cherry_dl.templates import pixiv as template_module  # noqa: E402
from third_party.cherry_dl.templates.pixiv import PixivTemplate  # noqa: E402

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

ok = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global ok, failed
    if condition:
        ok += 1
        print(f"  ✓ {label}")
    else:
        failed += 1
        print(f"  ✗ {label}")


async def test_pkce_and_login() -> None:
    print("\n[1] OAuth PKCE y persistencia")
    verifier, challenge = auth.generate_pkce()
    expected = urlsafe_b64encode(
        sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    url = auth.build_login_url(challenge)
    params = parse_qs(urlsplit(url).query)
    check("challenge correcto", challenge == expected)
    check("endpoint app-api", url.startswith("https://app-api.pixiv.net/web/v1/login?"))
    check("método S256", params.get("code_challenge_method") == ["S256"])

    captured = {}
    saved = []
    old_capture = browser_login.capture_oauth_code
    old_request = auth._request_tokens
    old_save = auth.save_pixiv_tokens

    async def fake_capture(login_url, **_kwargs):
        captured["url"] = login_url
        return "authorization-code"

    async def fake_request(fields):
        captured["fields"] = dict(fields)
        return {"access_token": "access", "refresh_token": "refresh", "_expires_at": 9999999999}

    browser_login.capture_oauth_code = fake_capture
    auth._request_tokens = fake_request
    auth.save_pixiv_tokens = lambda tokens: saved.append(dict(tokens))
    try:
        tokens = await auth.guided_login_pixiv()
    finally:
        browser_login.capture_oauth_code = old_capture
        auth._request_tokens = old_request
        auth.save_pixiv_tokens = old_save

    fields = captured["fields"]
    used_verifier = fields["code_verifier"]
    used_challenge = parse_qs(urlsplit(captured["url"]).query)["code_challenge"][0]
    derived = urlsafe_b64encode(
        sha256(used_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    check("código y verifier corresponden", fields["code"] == "authorization-code" and used_challenge == derived)
    check("refresh_token persistido", bool(saved and tokens and tokens.get("refresh_token") == "refresh"))


async def test_callback_capture() -> None:
    print("\n[2] Captura automática del callback")
    event_type = type("RequestWillBeSent", (), {})

    class Tab:
        enabled = False
        handler = None
        url = ""

        def add_handler(self, received_type, handler):
            if received_type is event_type:
                self.handler = handler

        async def send(self, _command):
            self.enabled = True

        async def get(self, url):
            self.url = url
            self.handler(SimpleNamespace(request=SimpleNamespace(
                url="pixiv://account/login?code=captured-code&via=login"
            )))

    class Browser:
        def __init__(self):
            self.tab = Tab()
            self.stopped = False

        async def get(self, _url):
            return self.tab

        def stop(self):
            self.stopped = True

    browser = Browser()

    async def fake_start(**_kwargs):
        return browser

    fake_nodriver = SimpleNamespace(
        start=fake_start,
        cdp=SimpleNamespace(network=SimpleNamespace(
            RequestWillBeSent=event_type,
            enable=lambda: "network-enable",
        )),
    )
    old_module = sys.modules.get("nodriver")
    old_find = browser_login.find_browser
    statuses = []
    sys.modules["nodriver"] = fake_nodriver
    browser_login.find_browser = lambda: ("edge", "edge.exe")
    try:
        code = await browser_login.capture_oauth_code(
            "https://app-api.pixiv.net/web/v1/login?test=1",
            timeout=1,
            on_status=statuses.append,
        )
    finally:
        browser_login.find_browser = old_find
        if old_module is None:
            sys.modules.pop("nodriver", None)
        else:
            sys.modules["nodriver"] = old_module

    check("extrae code", code == "captured-code")
    check("Network activo antes de navegar", browser.tab.enabled)
    check("abre OAuth", browser.tab.url.startswith("https://app-api.pixiv.net/"))
    check("informa autorización", any("autorizó" in message for message in statuses))
    check("cierra después del callback", browser.stopped)


async def test_refresh() -> None:
    print("\n[3] Renovación automática")
    old_load = auth.load_pixiv_tokens
    old_request = auth._request_tokens
    old_save = auth.save_pixiv_tokens
    requests = []
    saved = []
    auth.load_pixiv_tokens = lambda: {
        "access_token": "expired", "refresh_token": "refresh", "_expires_at": 1,
    }

    async def fake_request(fields):
        requests.append(dict(fields))
        return {"access_token": "renewed", "_expires_at": int(time.time()) + 3600}

    auth._request_tokens = fake_request
    auth.save_pixiv_tokens = lambda tokens: saved.append(dict(tokens))
    try:
        token = await auth.ensure_pixiv_access_token()
    finally:
        auth.load_pixiv_tokens = old_load
        auth._request_tokens = old_request
        auth.save_pixiv_tokens = old_save

    check("usa refresh_token", requests[0]["grant_type"] == "refresh_token")
    check("conserva refresh_token", saved[0]["refresh_token"] == "refresh")
    check("entrega token renovado", token == "renewed")


async def test_app_api_mapping() -> None:
    print("\n[4] Mapeo App API")
    page_requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/user/detail":
            payload = {"user": {"id": 42, "name": "OAuth Artist"}}
        elif request.url.path == "/v1/user/illusts":
            page_requests.append(str(request.url))
            if request.url.params.get("offset") == "30":
                payload = {"illusts": [{
                    "id": 300, "type": "illust", "title": "second page",
                    "create_date": "2026-01-03T00:00:00+00:00", "meta_pages": [],
                    "meta_single_page": {
                        "original_image_url": "https://i.pximg.net/300_p0.jpg"
                    },
                }], "next_url": None}
            else:
                payload = {"illusts": [
                {"id": 100, "type": "manga", "title": "pages", "create_date": "2026-01-01T00:00:00+00:00", "meta_pages": [
                    {"image_urls": {"original": "https://i.pximg.net/100_p0.jpg"}},
                    {"image_urls": {"original": "https://i.pximg.net/100_p1.png"}},
                ], "meta_single_page": {}},
                {"id": 200, "type": "ugoira", "title": "motion", "create_date": "2026-01-02T00:00:00+00:00", "meta_pages": [], "meta_single_page": {}},
                ], "next_url": "https://app-api.pixiv.net/v1/user/illusts?user_id=42&offset=30"}
        else:
            payload = {"ugoira_metadata": {"zip_urls": {
                "medium": "https://i.pximg.net/200_ugoira600x600.zip"
            }}}
        return httpx.Response(200, json=payload, request=request)

    template = PixivTemplate(None)
    template._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    old_uniform = template_module.random.uniform
    template_module.random.uniform = lambda *_args: 0
    try:
        artist = await template.get_artist_info("https://www.pixiv.net/users/42")
        files = [item async for item in template.iter_files(artist)]
    finally:
        template_module.random.uniform = old_uniform

    check("artista autenticado", artist.name == "OAuth Artist")
    check("recorre todas las páginas", len(page_requests) == 2)
    check("sólo conserva ilustraciones estáticas", len(files) == 1)
    check("dedup compatible", [item.url_source for item in files] == [
        "pixiv://illust/300/p0",
    ])
    check("manga y ugoira no llegan a descarga",
          all(item.content_role == "page" for item in files))


async def main() -> int:
    await test_pkce_and_login()
    await test_callback_capture()
    await test_refresh()
    await test_app_api_mapping()
    print(f"\n{ok} ok, {failed} fallo(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
