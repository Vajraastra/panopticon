"""Banco local del transporte Chromium: streaming, concurrencia y rechazo."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "third_party")]

from cherry_dl.config import NetworkConfig, UserConfig
from cherry_dl.browser_transport import (
    BrowserDownload, BrowserImageTransport, _canonical, _subresource_element,
)
from cherry_dl.engine import DownloadEngine, ErrorKind

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


BODY_A = bytes(range(256)) * 4096
BODY_B = b"cherry-browser" * 65536
BODY_VIDEO = b"webm-test" * 32768
BODY_AUDIO = b"audio-test" * 8192
BODY_TEXT = b"generic attachment\n"
state = {"active": 0, "max_active": 0, "headers": [], "requests": []}
lock = threading.Lock()
slow_started = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def _record(self) -> dict[str, str]:
        headers = {k.lower(): v for k, v in self.headers.items()}
        with lock:
            state["requests"].append((self.path, headers))
        return headers

    def do_GET(self) -> None:
        if self.path == "/":
            body = b"<!doctype html><body>root</body>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if self.path == "/deny":
            body = b"stop test: respectful client must stop"
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/partial":
            body = BODY_B[:1024]
            self.send_response(206, "Partial Content")
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Range", f"bytes 0-1023/{len(BODY_B)}")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/missing-html":
            body = b"<html><body>404 Not Found</body></html>"
            self.send_response(404, "Not Found")
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/video-full":
            self._record()
            self.send_response(206, "Partial Content")
            self.send_header("Content-Type", "video/webm")
            self.send_header("Content-Length", str(len(BODY_VIDEO)))
            self.send_header(
                "Content-Range", f"bytes 0-{len(BODY_VIDEO) - 1}/{len(BODY_VIDEO)}",
            )
            self.end_headers()
            self.wfile.write(BODY_VIDEO)
            return
        if self.path == "/audio":
            self._record()
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(BODY_AUDIO)))
            self.end_headers()
            self.wfile.write(BODY_AUDIO)
            return
        if self.path == "/document.txt":
            self._record()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(BODY_TEXT)))
            self.end_headers()
            self.wfile.write(BODY_TEXT)
            return

        is_slow = self.path == "/slow"
        body = (b"slow-browser" * 1024 * 1024 if is_slow
                else BODY_A if self.path == "/a" else BODY_B)
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            headers = {k.lower(): v for k, v in self.headers.items()}
            state["headers"].append(headers)
            state["requests"].append((self.path, headers))
        try:
            if is_slow:
                slow_started.set()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for offset in range(0, len(body), 65536):
                self.wfile.write(body[offset:offset + 65536])
                self.wfile.flush()
                time.sleep(0.03 if is_slow else 0.01)
        except OSError:
            pass
        finally:
            with lock:
                state["active"] -= 1


ok = fail = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global ok, fail
    if condition:
        ok += 1
        print(f"  ✓ {label}")
    else:
        fail += 1
        print(f"  ✗ {label} {detail}")


async def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    origin_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    origin_thread = threading.Thread(target=origin_server.serve_forever, daemon=True)
    thread.start()
    origin_thread.start()
    root = f"http://127.0.0.1:{server.server_port}/"
    cross_origin_root = f"http://127.0.0.1:{origin_server.server_port}/"
    target = Path(tempfile.mkdtemp(prefix="cherry_browser_toy_"))
    config = UserConfig(
        workers=2,
        timeout=5,
        network=NetworkConfig(delay_min=0, delay_max=0, stall_timeout=10),
    )
    try:
        print("[1] Dos streams simultáneos a disco")
        async with DownloadEngine(config, workers=2) as engine:
            results = await asyncio.gather(
                engine.download(root + "a", target, "a.png",
                                transport="browser_image", browser_origin=root),
                engine.download(root + "b", target, "b.jpg",
                                transport="browser_image", browser_origin=root),
            )
            initial_max_active = state["max_active"]
            denied = await engine.download(
                root + "deny", target, "deny.bin",
                transport="browser_image", browser_origin=root,
            )
            partial = await engine.download(
                root + "partial", target, "partial.mp4",
                transport="browser_image", browser_origin=root,
            )
            missing = await engine.download(
                root + "missing-html", target, "missing.gif",
                transport="browser_image", browser_origin=cross_origin_root,
            )
            video = await engine.download(
                root + "video-full", target, "clip.webm",
                transport="browser_image", browser_origin=root,
            )
            audio = await engine.download(
                root + "audio", target, "sound.mp3",
                transport="browser_image", browser_origin=root,
            )
            generic = await engine.download(
                root + "document.txt", target, "notes.txt",
                transport="browser_image", browser_origin=root,
            )
            project_results = []
            for suffix in (".psd", ".psb", ".clip", ".lip"):
                project_results.append(await engine.download(
                    root + suffix.removeprefix("."), target, "canvas" + suffix,
                    transport="browser_image", browser_origin=root,
                ))
            unicode_name = await engine.download(
                root + "unicode?f=Nutt’in & clubbin.mp4",
                target, "unicode-name.mp4",
                transport="browser_image", browser_origin=root,
            )
            cancelled_task = asyncio.create_task(engine.download(
                root + "slow", target, "slow.bin",
                transport="browser_image", browser_origin=root,
            ))
            await asyncio.to_thread(slow_started.wait, 5)
            await asyncio.sleep(0.1)
            cancelled_task.cancel()
            cancelled = False
            try:
                await cancelled_task
            except asyncio.CancelledError:
                cancelled = True

        check(all(r.ok for r in results), "los dos archivos terminan")
        check((target / "a.png").read_bytes() == BODY_A, "A coincide byte a byte")
        check((target / "b.jpg").read_bytes() == BODY_B, "B coincide byte a byte")
        check(results[0].file_hash == hashlib.sha256(BODY_A).hexdigest(), "hash A correcto")
        check(results[1].file_hash == hashlib.sha256(BODY_B).hexdigest(), "hash B correcto")
        check(1 <= initial_max_active <= 2,
              "el servidor nunca vio más de dos descargas activas",
              f"(máximo {initial_max_active})")
        image_headers = [
            headers for path, headers in state["requests"] if path in {"/a", "/b"}
        ]
        check(all(h.get("sec-fetch-dest") == "image" for h in image_headers),
              "las imágenes usan su contexto nativo")
        check(all(h.get("referer") == root for h in image_headers),
              "ambos nacieron desde la página de origen")

        print("\n[2] El contexto sigue el tipo del archivo")
        check(video.ok and (target / "clip.webm").read_bytes() == BODY_VIDEO,
              "un vídeo 206 completo se guarda entero")
        check(audio.ok and (target / "sound.mp3").read_bytes() == BODY_AUDIO,
              "un audio usa su elemento nativo")
        check(generic.ok and (target / "notes.txt").read_bytes() == BODY_TEXT,
              "un adjunto genérico usa un documento oculto")
        destinations = {
            path: headers.get("sec-fetch-dest") for path, headers in state["requests"]
        }
        check(destinations.get("/video-full") == "video", "el vídeo sale como video")
        check(destinations.get("/audio") == "audio", "el audio sale como audio")
        check(destinations.get("/document.txt") == "iframe", "el texto sale como iframe")
        check(all(result.ok for result in project_results),
              "los cuatro lienzos editables se descargan")
        check(all(
            destinations.get("/" + suffix.removeprefix(".")) == "image"
            for suffix in (".psd", ".psb", ".clip", ".lip")
        ), "PSD/PSB/CLIP/LIP salen con contexto de imagen")
        check(all(
            _subresource_element("https://file.invalid/art" + suffix) == "img"
            for suffix in (".psd", ".psb", ".clip", ".lip")
        ), "la clasificación no depende del servidor")
        check(
            _canonical("https://file.invalid/data/hash.mp4?f=Nutt’in & clubbin.mp4")
            == _canonical(
                "https://file.invalid/data/hash.mp4?f=Nutt%E2%80%99in%20&%20clubbin.mp4"
            ),
            "el nombre Unicode no rompe la correlación CDP",
        )
        check(unicode_name.ok, "la respuesta con nombre Unicode llega a su descarga")

        print("\n[3] Un rechazo no deja basura ni se reintenta")
        check(not denied.ok, "el 403 falla")
        check(denied.error_kind == ErrorKind.AUTH, "el 403 se clasifica como AUTH")
        check(denied.failure_code == "http_rejected",
              "el 403 no se confunde con el fallo silencioso")
        check("stop test" in (denied.error or ""), "conserva el mensaje del operador")
        check(not (target / "deny.bin").exists(), "no crea archivo de destino")
        check(not list(target.glob("*.tmp")), "no deja .tmp")

        print("\n[4] Un 206 parcial conserva los datos necesarios para diagnosticarlo")
        check(not partial.ok, "el parcial no se acepta como archivo completo")
        check(partial.error_kind == ErrorKind.NETWORK, "el 206 se clasifica como network")
        check(partial.failure_code == "http_rejected",
              "el 206 no activa el cortacircuitos silencioso")
        check("HTTP 206 Partial Content" in (partial.error or ""), "muestra código y estado")
        check("mime=video/mp4" in (partial.error or ""), "muestra el MIME")
        check("range=bytes 0-1023/" in (partial.error or ""), "muestra Content-Range")
        check(not (target / "partial.mp4").exists(), "no guarda un video parcial")

        print("\n[5] Un 404 HTML cross-origin conserva su estado real")
        check(not missing.ok, "el HTML de nginx no se acepta como imagen")
        check(missing.error_kind == ErrorKind.NOT_FOUND,
              "Network recupera el 404 que Fetch presenta como Failed")
        check(missing.failure_code == "http_rejected",
              "el archivo ausente no activa el cortacircuitos")
        check("HTTP 404" in (missing.error or ""), "el diagnóstico muestra el 404")
        check(not (target / "missing.gif").exists(), "no crea archivo de destino")

        print("\n[6] Cancelar corta el subrecurso y limpia el parcial")
        check(cancelled, "la cancelación llega al llamador")
        check(not (target / "slow.bin").exists(), "no deja destino incompleto")
        check(not (target / "slow.bin.tmp").exists(), "borra el parcial cancelado")

        print("\n[7] Una pausa tardía de CDP se recoge sin ruido de asyncio")
        transport = BrowserImageTransport(root, root + "late")
        calls = []

        async def invalid_interception(method, params):
            calls.append((method, params))
            raise RuntimeError("Invalid InterceptionId.")

        transport._send = invalid_interception
        transport._resume_unclaimed("already-gone")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        check(not transport._side_tasks, "la tarea auxiliar termina y queda recogida")
        check(calls == [("Fetch.continueRequest", {"requestId": "already-gone"})],
              "intenta liberar la pausa exactamente una vez")

        print("\n[8] Una orden CDP muda expira sin esperar el timeout de archivo")
        silent = BrowserImageTransport(
            root, root + "silent", command_timeout=0.02,
        )

        class SilentSocket:
            async def send(self, _payload):
                return None

        silent._ws = SilentSocket()
        try:
            await silent._send_browser("Runtime.silent")
            check(False, "la orden muda debía expirar")
        except TimeoutError as exc:
            check("Runtime.silent" in str(exc), "el error identifica la orden CDP")
            check(not silent._pending, "la orden expirada no deja futuros pendientes")

        print("\n[9] El cierre CDP tiene un límite aunque el navegador no responda")
        hanging = BrowserImageTransport(root, root + "hang")

        class HangingSocket:
            async def close(self):
                await asyncio.Event().wait()

        async def hanging_close(_method, _params=None):
            await asyncio.Event().wait()

        hanging._ws = HangingSocket()
        hanging._send_browser = hanging_close
        started = time.monotonic()
        await hanging.close()
        check(time.monotonic() - started < 3.0,
              "un WebSocket mudo no congela el apagado")

        print("\n[10] La limpieza libera recursos multimedia")
        cleanup = BrowserImageTransport(root, root + "cleanup")
        cleanup_calls = []

        async def capture_cleanup(method, params):
            cleanup_calls.append((method, params))
            return {}

        cleanup._send = capture_cleanup
        await cleanup._remove_element("media-test")
        cleanup_expression = cleanup_calls[0][1]["expression"]
        check("removeAttribute('src')" in cleanup_expression,
              "se corta el origen antes de retirar video/audio")
        check("media.load()" in cleanup_expression,
              "Chromium libera el recurso multimedia")

        print("\n[11] Un Failed invalida la sesión antes del siguiente archivo")

        class BrokenTransport:
            def __init__(self):
                self.closed = False

            async def start(self, _rate):
                pass

            async def download(self, *_args, **_kwargs):
                return BrowserDownload(
                    error="sin estado HTTP · reason=Failed",
                    failure_code="no_http_failed",
                )

            async def close(self):
                self.closed = True

        async with DownloadEngine(config, workers=1) as reset_engine:
            broken = BrokenTransport()
            broken_url = root + "broken.webm"
            reset_engine._browser_transport = broken
            reset_engine._browser_key = (root, urlsplit(broken_url).netloc)
            broken_result = await reset_engine.download(
                broken_url, target, "broken.webm",
                transport="browser_image", browser_origin=root,
            )
            check(not broken_result.ok, "el fallo conserva su resultado")
            check(broken.closed, "la sesión dañada se cierra")
            check(reset_engine._browser_transport is None,
                  "el siguiente archivo creará un navegador limpio")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        origin_server.shutdown()
        origin_server.server_close()
        origin_thread.join(timeout=2)
        shutil.rmtree(target, ignore_errors=True)

    print(f"\n{ok} ok, {fail} fallo(s)")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
