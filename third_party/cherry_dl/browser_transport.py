"""Transporte de archivos como subrecursos de un navegador Chromium.

Algunos hosts sirven correctamente un archivo cuando nace como subrecurso de
su propia página, pero rechazan un GET directo. Este módulo reproduce ese flujo
normal sin copiar perfiles ni cookies: abre un Chromium headless con perfil
temporal, navega al origen y crea un elemento ``<img>`` por archivo.

El cuerpo no pasa entero por RAM. CDP pausa la respuesta al recibir cabeceras,
``Fetch.takeResponseBodyAsStream`` entrega un stream secuencial y ``IO.read``
lo vuelca al ``.tmp`` mientras calcula SHA-256.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from websockets.asyncio.client import connect


_READ_SIZE = 64 * 1024
_FLUSH_EVERY = 8 * 1024 * 1024
_REDIRECTS = {301, 302, 303, 307, 308}


@dataclass(slots=True)
class BrowserDownload:
    dest: Path | None = None
    file_hash: str | None = None
    file_size: int = 0
    status: int | None = None
    error: str | None = None


def find_chromium() -> Path | None:
    """Encuentra Brave primero; Edge/Chrome quedan como respaldo Windows."""
    override = os.environ.get("CHERRY_BROWSER_PATH")
    candidates = [
        override,
        shutil.which("brave"),
        shutil.which("brave-browser"),
        shutil.which("msedge"),
        shutil.which("chrome"),
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _canonical(url: str) -> str:
    """Normaliza como Chromium los espacios que aparecen en query strings."""
    parts = urlsplit(url)
    return parts._replace(path=parts.path.replace(" ", "%20"),
                          query=parts.query.replace(" ", "%20")).geturl()


class BrowserImageTransport:
    """Una sesión Chromium/CDP compartida por todos los workers del engine."""

    def __init__(self, root_url: str, file_url: str) -> None:
        self.root_url = root_url
        parts = urlsplit(file_url)
        self.file_origin = f"{parts.scheme}://{parts.netloc}"
        self._profile: Path | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._session_id: str | None = None
        self._target_id: str | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._paused: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        self._start_lock = asyncio.Lock()
        self._configured_rate: float | None = None

    async def start(self, bytes_per_sec: float = 0) -> None:
        async with self._start_lock:
            if self._ws is None:
                await self._launch()
            rate = float(bytes_per_sec or 0)
            if rate != self._configured_rate:
                await self._send("Network.emulateNetworkConditions", {
                    "offline": False,
                    "latency": 0,
                    "downloadThroughput": rate if rate > 0 else -1,
                    "uploadThroughput": rate if rate > 0 else -1,
                    "connectionType": "wifi",
                })
                self._configured_rate = rate

    async def _launch(self) -> None:
        browser = find_chromium()
        if browser is None:
            raise RuntimeError(
                "No se encontró Brave, Edge ni Chrome; define CHERRY_BROWSER_PATH"
            )

        self._profile = Path(tempfile.mkdtemp(prefix="cherry-browser-"))
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW
        self._process = await asyncio.create_subprocess_exec(
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--disable-background-networking",
            "--remote-debugging-port=0",
            f"--user-data-dir={self._profile}",
            "about:blank",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=flags,
        )

        active = self._profile / "DevToolsActivePort"
        for _ in range(80):
            if active.is_file():
                lines = active.read_text(encoding="utf-8").splitlines()
                if len(lines) >= 2:
                    ws_url = f"ws://127.0.0.1:{lines[0]}{lines[1]}"
                    break
            if self._process.returncode is not None:
                raise RuntimeError("Chromium terminó antes de abrir CDP")
            await asyncio.sleep(0.125)
        else:
            raise RuntimeError("Chromium no abrió CDP en 10 segundos")

        self._ws = await connect(
            ws_url, max_size=None, ping_interval=None, proxy=None,
        )
        self._recv_task = asyncio.create_task(self._receive())
        target = await self._send_browser("Target.createTarget", {"url": "about:blank"})
        self._target_id = target["targetId"]
        attached = await self._send_browser(
            "Target.attachToTarget", {"targetId": self._target_id, "flatten": True}
        )
        self._session_id = attached["sessionId"]
        await self._send("Page.enable")
        await self._send("Network.enable")
        await self._send("Network.setCacheDisabled", {"cacheDisabled": True})
        await self._send("Fetch.enable", {"patterns": [{
            "urlPattern": self.file_origin + "/*",
            "resourceType": "Image",
            "requestStage": "Response",
        }]})
        await self._send("Page.navigate", {"url": self.root_url})

        for _ in range(120):
            state = await self._send("Runtime.evaluate", {
                "expression": "document.readyState", "returnByValue": True,
            })
            if state.get("result", {}).get("value") in {"interactive", "complete"}:
                return
            await asyncio.sleep(0.25)
        raise RuntimeError(f"El navegador no pudo abrir {self.root_url}")

    async def _receive(self) -> None:
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                msg_id = message.get("id")
                if msg_id in self._pending:
                    future = self._pending.pop(msg_id)
                    if "error" in message:
                        future.set_exception(RuntimeError(message["error"]["message"]))
                    else:
                        future.set_result(message.get("result", {}))
                    continue
                if (message.get("sessionId") == self._session_id
                        and message.get("method") == "Fetch.requestPaused"):
                    params = message["params"]
                    key = _canonical(params["request"]["url"])
                    queue = self._paused.get(key)
                    if queue is not None:
                        queue.put_nowait(params)
                    else:
                        asyncio.create_task(self._send(
                            "Fetch.continueRequest", {"requestId": params["requestId"]}
                        ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(exc)
            self._pending.clear()

    async def _send_browser(self, method: str, params: dict | None = None) -> dict:
        return await self._send_raw(method, params or {}, None)

    async def _send(self, method: str, params: dict | None = None) -> dict:
        if self._session_id is None:
            raise RuntimeError("La sesión CDP no está lista")
        return await self._send_raw(method, params or {}, self._session_id)

    async def _send_raw(self, method: str, params: dict, session_id: str | None) -> dict:
        if self._ws is None:
            raise RuntimeError("Chromium no está conectado")
        self._next_id += 1
        msg_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        payload = {"id": msg_id, "method": method, "params": params}
        if session_id is not None:
            payload["sessionId"] = session_id
        await self._ws.send(json.dumps(payload))
        return await future

    async def download(
        self,
        url: str,
        dest_dir: Path,
        filename: str,
        *,
        take_bytes: Callable[[int], Awaitable[None]] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        stall_timeout: float = 45.0,
    ) -> BrowserDownload:
        if self._ws is None:
            raise RuntimeError("BrowserImageTransport.start() no fue llamado")

        key = _canonical(url)
        queue = self._paused[key]
        element_id = "cherry_" + hashlib.md5(url.encode()).hexdigest()
        expression = """(() => {
          const old = document.getElementById(%s); if (old) old.remove();
          const img = document.createElement('img'); img.id = %s;
          img.style.display = 'none'; img.src = %s; document.body.appendChild(img);
          return true;
        })()""" % tuple(json.dumps(v) for v in (element_id, element_id, url))
        await self._send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
        })

        try:
            paused = await asyncio.wait_for(queue.get(), timeout=60.0)
        except asyncio.TimeoutError:
            self._paused.pop(key, None)
            await self._remove_element(element_id)
            return BrowserDownload(error="El navegador no recibió cabeceras en 60 s")
        except asyncio.CancelledError:
            self._paused.pop(key, None)
            await self._remove_element(element_id)
            raise

        request_id = paused["requestId"]
        status = paused.get("responseStatusCode")
        headers = {
            h["name"].lower(): h.get("value", "")
            for h in paused.get("responseHeaders", [])
        }
        if status in _REDIRECTS:
            await self._send("Fetch.continueRequest", {"requestId": request_id})
            self._paused.pop(key, None)
            await self._remove_element(element_id)
            return BrowserDownload(status=status, error=f"Redirección HTTP {status}")

        if status != 200:
            detail = await self._read_error_body(request_id, headers)
            self._paused.pop(key, None)
            await self._remove_element(element_id)
            suffix = f" — {detail}" if detail else ""
            return BrowserDownload(status=status, error=f"HTTP {status}{suffix}")

        stream = (await self._send(
            "Fetch.takeResponseBodyAsStream", {"requestId": request_id}
        ))["stream"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        tmp = dest_dir / (filename + ".tmp")
        hasher = hashlib.sha256()
        written = 0
        total = int(headers.get("content-length", "0") or 0)
        parts: list[bytes] = []
        buffered = 0

        try:
            with open(tmp, "wb") as fh:
                if on_status:
                    on_status("↓ descargando mediante navegador…")
                while True:
                    packet = await asyncio.wait_for(
                        self._send("IO.read", {"handle": stream, "size": _READ_SIZE}),
                        timeout=stall_timeout,
                    )
                    data = packet.get("data", "")
                    chunk = (base64.b64decode(data) if packet.get("base64Encoded")
                             else data.encode("utf-8"))
                    if chunk:
                        if take_bytes is not None:
                            await take_bytes(len(chunk))
                        parts.append(chunk)
                        buffered += len(chunk)
                        if buffered >= _FLUSH_EVERY:
                            for part in parts:
                                hasher.update(part)
                                fh.write(part)
                            written += buffered
                            parts = []
                            buffered = 0
                        if on_progress:
                            on_progress(written + buffered, total)
                    if packet.get("eof"):
                        break
                for part in parts:
                    hasher.update(part)
                    fh.write(part)
                written += buffered
            tmp.replace(dest)
            return BrowserDownload(
                dest=dest, file_hash=hasher.hexdigest(), file_size=written,
                status=status,
            )
        except asyncio.TimeoutError:
            tmp.unlink(missing_ok=True)
            return BrowserDownload(status=status, error="La descarga dejó de recibir datos")
        except asyncio.CancelledError:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            return BrowserDownload(status=status, error=f"Error de navegador: {type(exc).__name__}: {exc}")
        finally:
            try:
                await self._send("IO.close", {"handle": stream})
            except Exception:
                pass
            try:
                await self._send("Fetch.failRequest", {
                    "requestId": request_id, "errorReason": "Aborted",
                })
            except Exception:
                pass
            try:
                await self._remove_element(element_id)
            except Exception:
                pass
            self._paused.pop(key, None)

    async def _remove_element(self, element_id: str) -> None:
        try:
            await asyncio.shield(self._send("Runtime.evaluate", {
                "expression": f"document.getElementById({json.dumps(element_id)})?.remove()",
            }))
        except Exception:
            pass

    async def _read_error_body(self, request_id: str, headers: dict[str, str]) -> str:
        if "text" not in headers.get("content-type", "").lower():
            try:
                await self._send("Fetch.failRequest", {
                    "requestId": request_id, "errorReason": "Aborted",
                })
            except Exception:
                pass
            return ""
        stream = None
        try:
            stream = (await self._send(
                "Fetch.takeResponseBodyAsStream", {"requestId": request_id}
            ))["stream"]
            packet = await self._send("IO.read", {"handle": stream, "size": 600})
            data = packet.get("data", "")
            raw = base64.b64decode(data) if packet.get("base64Encoded") else data.encode()
            return raw.decode("utf-8", errors="replace").strip()
        except Exception:
            return ""
        finally:
            if stream is not None:
                try:
                    await self._send("IO.close", {"handle": stream})
                except Exception:
                    pass
            try:
                await self._send("Fetch.failRequest", {
                    "requestId": request_id, "errorReason": "Aborted",
                })
            except Exception:
                pass

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._send_browser("Browser.close")
            except Exception:
                pass
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._recv_task is not None:
            self._recv_task.cancel()
            await asyncio.gather(self._recv_task, return_exceptions=True)
        if self._process is not None:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._profile is not None:
            await asyncio.to_thread(shutil.rmtree, self._profile, True)
        self._ws = None
        self._process = None
        self._profile = None
