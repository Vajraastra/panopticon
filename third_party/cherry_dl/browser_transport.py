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
import logging
import os
import re
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
_CONTENT_RANGE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)$", re.IGNORECASE)
_IMAGE_EXTS = frozenset({
    ".avif", ".bmp", ".gif", ".ico", ".jfif", ".jpe", ".jpeg", ".jpg",
    ".png", ".svg", ".tif", ".tiff", ".webp",
    # Lienzos editables: Pawchive los sirve correctamente con contexto image.
    ".clip", ".lip", ".psb", ".psd",
})
_VIDEO_EXTS = frozenset({
    ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".ogv", ".webm",
})
_AUDIO_EXTS = frozenset({
    ".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav",
    ".weba",
})
_CLOSE_COMMAND_TIMEOUT = 1.0
_SOCKET_CLOSE_TIMEOUT = 1.0
_PROCESS_CLOSE_TIMEOUT = 2.0
_TASK_CLOSE_TIMEOUT = 1.0
_COMMAND_TIMEOUT = 15.0

log = logging.getLogger(__name__)


@dataclass(slots=True)
class BrowserDownload:
    dest: Path | None = None
    file_hash: str | None = None
    file_size: int = 0
    status: int | None = None
    error: str | None = None
    failure_code: str | None = None


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
    """Clave estable del objeto, independiente del nombre decorativo ``?f=``.

    Chromium codifica Unicode y ciertos signos de la query antes de emitir los
    eventos CDP. El path de Pawchive contiene el hash real y no cambia; usarlo
    evita perder respuestas cuyos nombres llevan apóstrofos, espacios o emoji.
    """
    parts = urlsplit(url)
    return parts._replace(
        path=parts.path.replace(" ", "%20"), query="", fragment="",
    ).geturl()


def _subresource_element(url: str, filename: str = "") -> str:
    """Elige un elemento compatible; lo desconocido se trata como documento."""
    suffix = Path(urlsplit(url).path).suffix.lower() or Path(filename).suffix.lower()
    if suffix in _IMAGE_EXTS:
        return "img"
    if suffix in _VIDEO_EXTS:
        return "video"
    if suffix in _AUDIO_EXTS:
        return "audio"
    return "iframe"


def _complete_response_size(status: int | None, headers: dict[str, str]) -> int | None:
    """Tamaño esperado si la respuesta contiene el objeto entero; si no, None."""
    try:
        length = int(headers.get("content-length", "0") or 0)
    except ValueError:
        length = 0
    if status == 200:
        return max(0, length)
    if status != 206:
        return None
    match = _CONTENT_RANGE.fullmatch(headers.get("content-range", "").strip())
    if match is None:
        return None
    start, end, total = (int(value) for value in match.groups())
    if start != 0 or end + 1 != total or (length and length != total):
        return None
    return total


def _safe_target(url: str) -> str:
    """Host y path sin query: identifica el objeto sin filtrar su nombre."""
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}" if parts.netloc else parts.path


def _one_line(value: str, limit: int = 240) -> str:
    compact = " ".join(str(value).split())
    return compact if len(compact) <= limit else compact[:limit - 1] + "…"


def _response_diagnostic(
    paused: dict, headers: dict[str, str], response_status: int | None = None,
) -> str:
    """Descripción persistible de una respuesta CDP que no se puede bajar."""
    status = (response_status if response_status is not None
              else paused.get("responseStatusCode"))
    status_text = _one_line(paused.get("responseStatusText") or "", 80)
    first = f"HTTP {status}" if status is not None else "sin estado HTTP"
    if status_text:
        first += f" {status_text}"
    parts = [first]
    fields = (
        ("mime", "content-type"),
        ("length", "content-length"),
        ("range", "content-range"),
        ("server", "server"),
    )
    for label, key in fields:
        if headers.get(key):
            parts.append(f"{label}={_one_line(headers[key], 120)}")
    if paused.get("responseErrorReason"):
        parts.append(f"reason={paused['responseErrorReason']}")
    if headers.get("location"):
        parts.append(f"location={_safe_target(headers['location'])}")
    if paused.get("redirectedRequestId"):
        parts.append(f"redirected_from={paused['redirectedRequestId']}")
    if paused.get("networkId"):
        parts.append(f"network_id={paused['networkId']}")
    return " · ".join(parts)


def _lower_headers(raw: object) -> dict[str, str]:
    """Normaliza las dos formas de cabeceras que usan Fetch y Network."""
    if isinstance(raw, dict):
        return {str(name).lower(): str(value) for name, value in raw.items()}
    if isinstance(raw, list):
        return {
            str(item.get("name", "")).lower(): str(item.get("value", ""))
            for item in raw if isinstance(item, dict) and item.get("name")
        }
    return {}


def _merge_response_metadata(
    paused: dict, extra: dict | None,
) -> tuple[int | None, dict[str, str]]:
    """Recupera el HTTP que Chromium puede ocultar al cargador de imágenes."""
    status = paused.get("responseStatusCode")
    headers = _lower_headers(paused.get("responseHeaders", []))
    if extra is not None:
        if status is None:
            status = extra.get("statusCode")
        # Fetch es la vista más cercana al cuerpo; sus valores ganan si ambas
        # fuentes traen la misma cabecera.
        headers = {**_lower_headers(extra.get("headers", {})), **headers}
    return status, headers


class BrowserImageTransport:
    """Una sesión Chromium/CDP de subrecursos compartida por todos los workers.

    El nombre se conserva por compatibilidad, pero el elemento se elige por
    extensión: imagen, vídeo, audio o documento genérico.
    """

    def __init__(
        self, root_url: str, file_url: str, *, command_timeout: float = _COMMAND_TIMEOUT,
    ) -> None:
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
        self._file_request_ids: set[str] = set()
        self._response_extra: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        self._side_tasks: set[asyncio.Task] = set()
        self._start_lock = asyncio.Lock()
        self._configured_rate: float | None = None
        self._command_timeout = max(0.01, float(command_timeout))

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
                    if future.done():
                        continue
                    if "error" in message:
                        future.set_exception(RuntimeError(message["error"]["message"]))
                    else:
                        future.set_result(message.get("result", {}))
                    continue
                if message.get("sessionId") != self._session_id:
                    continue
                method = message.get("method")
                params = message.get("params", {})
                if method == "Network.requestWillBeSent":
                    request_url = params.get("request", {}).get("url", "")
                    if request_url.startswith(self.file_origin + "/"):
                        self._file_request_ids.add(params["requestId"])
                elif (method == "Network.responseReceivedExtraInfo"
                      and params.get("requestId") in self._file_request_ids):
                    self._response_extra[params["requestId"]].put_nowait(params)
                elif method == "Fetch.requestPaused":
                    params = message["params"]
                    key = _canonical(params["request"]["url"])
                    queue = self._paused.get(key)
                    if queue is not None:
                        queue.put_nowait(params)
                    else:
                        self._resume_unclaimed(
                            params["requestId"], params.get("networkId"),
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(exc)
            self._pending.clear()

    def _resume_unclaimed(
        self, request_id: str, network_id: str | None = None,
    ) -> None:
        """Libera una pausa tardía sin dejar una tarea huérfana.

        Quitar el ``<img>`` puede invalidar el request entre recibir
        ``Fetch.requestPaused`` y enviar ``continueRequest``. Chromium responde
        entonces ``Invalid InterceptionId``: es un cierre benigno, pero si la
        corrutina se lanza directamente con ``create_task`` asyncio lo presenta
        como ``Task exception was never retrieved``.
        """
        task = asyncio.create_task(
            self._resume_unclaimed_async(request_id, network_id)
        )
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    async def _resume_unclaimed_async(
        self, request_id: str, network_id: str | None,
    ) -> None:
        try:
            await self._send(
                "Fetch.continueRequest", {"requestId": request_id},
            )
        except RuntimeError as exc:
            if "invalid interceptionid" in str(exc).casefold():
                log.debug("CDP descartó una pausa tardía ya cerrada: %s", request_id)
            else:
                log.warning("No se pudo liberar una pausa CDP sin dueño: %s", exc)
        except Exception as exc:
            log.warning("No se pudo liberar una pausa CDP sin dueño: %s", exc)
        finally:
            self._discard_network_metadata(network_id)

    async def _take_response_extra(self, network_id: str) -> dict | None:
        """Espera brevemente el evento Network compañero si llega rezagado."""
        queue = self._response_extra[network_id]
        try:
            try:
                return queue.get_nowait()
            except asyncio.QueueEmpty:
                return await asyncio.wait_for(queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            return None
        finally:
            self._discard_network_metadata(network_id)

    def _discard_network_metadata(self, network_id: str | None) -> None:
        if network_id is None:
            return
        self._file_request_ids.discard(network_id)
        self._response_extra.pop(network_id, None)

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
        try:
            await asyncio.wait_for(
                self._ws.send(json.dumps(payload)), timeout=self._command_timeout,
            )
            return await asyncio.wait_for(future, timeout=self._command_timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Chromium no respondió a {method} en {self._command_timeout:g} s"
            ) from exc
        finally:
            self._pending.pop(msg_id, None)

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
        element = _subresource_element(url, filename)
        expression = """(() => {
          const old = document.getElementById(%s); if (old) old.remove();
          const media = document.createElement(%s); media.id = %s;
          media.style.display = 'none'; media.src = %s;
          if (media.tagName === 'VIDEO' || media.tagName === 'AUDIO') media.preload = 'auto';
          document.body.appendChild(media);
          if (media.tagName === 'VIDEO' || media.tagName === 'AUDIO') media.load();
          return true;
        })()""" % tuple(
            json.dumps(v) for v in (element_id, element, element_id, url)
        )
        await self._send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
        })

        try:
            paused = await asyncio.wait_for(queue.get(), timeout=60.0)
        except asyncio.TimeoutError:
            self._paused.pop(key, None)
            await self._remove_element(element_id)
            error = "El navegador no recibió cabeceras en 60 s"
            log.warning(
                "CDP timeout de cabeceras | archivo=%r | target=%s",
                filename, _safe_target(url),
            )
            return BrowserDownload(error=error, failure_code="headers_timeout")
        except asyncio.CancelledError:
            self._paused.pop(key, None)
            await self._remove_element(element_id)
            raise

        request_id = paused["requestId"]
        network_id = paused.get("networkId")
        extra = None
        if paused.get("responseStatusCode") is None and network_id is not None:
            extra = await self._take_response_extra(network_id)
        else:
            self._discard_network_metadata(network_id)
        status, headers = _merge_response_metadata(paused, extra)
        diagnostic = _response_diagnostic(paused, headers, status)
        if status in _REDIRECTS:
            await self._send("Fetch.continueRequest", {"requestId": request_id})
            self._paused.pop(key, None)
            await self._remove_element(element_id)
            error = f"Redirección: {diagnostic}"
            log.warning(
                "CDP redirección no seguida | archivo=%r | target=%s | %s",
                filename, _safe_target(url), diagnostic,
            )
            return BrowserDownload(status=status, error=error,
                                   failure_code="redirect")

        expected_size = _complete_response_size(status, headers)
        if expected_size is None:
            detail = await self._read_error_body(request_id, headers)
            self._paused.pop(key, None)
            await self._remove_element(element_id)
            suffix = f" · body={_one_line(detail)}" if detail else ""
            error = diagnostic + suffix
            log.warning(
                "CDP respuesta rechazada | archivo=%r | target=%s | %s",
                filename, _safe_target(url), error,
            )
            code = ("no_http_failed"
                    if status is None
                    and paused.get("responseErrorReason") == "Failed"
                    else "http_rejected")
            return BrowserDownload(status=status, error=error, failure_code=code)

        stream = (await self._send(
            "Fetch.takeResponseBodyAsStream", {"requestId": request_id}
        ))["stream"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        tmp = dest_dir / (filename + ".tmp")
        hasher = hashlib.sha256()
        written = 0
        total = expected_size
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
            if total and written != total:
                tmp.unlink(missing_ok=True)
                error = (
                    f"La respuesta terminó con {written} de {total} bytes · "
                    f"{diagnostic}"
                )
                log.warning(
                    "CDP respuesta incompleta | archivo=%r | target=%s | %s",
                    filename, _safe_target(url), error,
                )
                return BrowserDownload(
                    status=status, error=error, failure_code="incomplete_response",
                )
            tmp.replace(dest)
            return BrowserDownload(
                dest=dest, file_hash=hasher.hexdigest(), file_size=written,
                status=status,
            )
        except asyncio.TimeoutError:
            tmp.unlink(missing_ok=True)
            error = f"La descarga dejó de recibir datos · {diagnostic}"
            log.warning(
                "CDP stream detenido | archivo=%r | target=%s | %s",
                filename, _safe_target(url), diagnostic,
            )
            return BrowserDownload(status=status, error=error,
                                   failure_code="stream_timeout")
        except asyncio.CancelledError:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            error = (
                f"Error de navegador: {type(exc).__name__}: {_one_line(exc)}"
                f" · {diagnostic}"
            )
            log.exception(
                "CDP fallo de stream | archivo=%r | target=%s | %s",
                filename, _safe_target(url), diagnostic,
            )
            return BrowserDownload(status=status, error=error,
                                   failure_code="stream_error")
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
        expression = """(() => {
          const media = document.getElementById(%s);
          if (!media) return false;
          if (media.tagName === 'VIDEO' || media.tagName === 'AUDIO') {
            media.pause();
            media.removeAttribute('src');
            media.load();
          } else if (media.tagName === 'IMG') {
            media.removeAttribute('src');
          } else if (media.tagName === 'IFRAME') {
            media.src = 'about:blank';
          }
          media.remove();
          return true;
        })()""" % json.dumps(element_id)
        try:
            await asyncio.wait_for(
                asyncio.shield(self._send("Runtime.evaluate", {
                    "expression": expression,
                })),
                timeout=1.0,
            )
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
                await asyncio.wait_for(
                    self._send_browser("Browser.close"),
                    timeout=_CLOSE_COMMAND_TIMEOUT,
                )
            except Exception as exc:
                log.debug("Cierre CDP sin respuesta: %s", exc)
            try:
                await asyncio.wait_for(
                    self._ws.close(), timeout=_SOCKET_CLOSE_TIMEOUT,
                )
            except Exception as exc:
                log.debug("WebSocket CDP no cerró a tiempo: %s", exc)
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(self._recv_task, return_exceptions=True),
                    timeout=_TASK_CLOSE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                log.warning("El receptor CDP no confirmó su cancelación en 1s")
            self._recv_task = None
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        side_tasks = tuple(self._side_tasks)
        for task in side_tasks:
            task.cancel()
        if side_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*side_tasks, return_exceptions=True),
                    timeout=_TASK_CLOSE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                log.warning("Las tareas auxiliares CDP no cerraron en 1s")
        self._side_tasks.clear()
        self._file_request_ids.clear()
        self._response_extra.clear()
        if self._process is not None:
            try:
                await asyncio.wait_for(
                    self._process.wait(), timeout=_PROCESS_CLOSE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    log.warning("Chromium no confirmó su terminación en 1s")
        if self._profile is not None:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(shutil.rmtree, self._profile, True),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                log.warning("El perfil temporal tardó en borrarse: %s", self._profile)
        self._ws = None
        self._process = None
        self._profile = None
