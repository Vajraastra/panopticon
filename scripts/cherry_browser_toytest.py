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

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "third_party")]

from cherry_dl.config import NetworkConfig, UserConfig
from cherry_dl.engine import DownloadEngine, ErrorKind

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


BODY_A = bytes(range(256)) * 4096
BODY_B = b"cherry-browser" * 65536
state = {"active": 0, "max_active": 0, "headers": []}
lock = threading.Lock()
slow_started = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

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

        is_slow = self.path == "/slow"
        body = (b"slow-browser" * 1024 * 1024 if is_slow
                else BODY_A if self.path == "/a" else BODY_B)
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            state["headers"].append({k.lower(): v for k, v in self.headers.items()})
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


def check(condition: bool, label: str) -> None:
    global ok, fail
    if condition:
        ok += 1
        print(f"  ✓ {label}")
    else:
        fail += 1
        print(f"  ✗ {label}")


async def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_port}/"
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
                engine.download(root + "a", target, "a.bin",
                                transport="browser_image", browser_origin=root),
                engine.download(root + "b", target, "b.bin",
                                transport="browser_image", browser_origin=root),
            )
            denied = await engine.download(
                root + "deny", target, "deny.bin",
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
        check((target / "a.bin").read_bytes() == BODY_A, "A coincide byte a byte")
        check((target / "b.bin").read_bytes() == BODY_B, "B coincide byte a byte")
        check(results[0].file_hash == hashlib.sha256(BODY_A).hexdigest(), "hash A correcto")
        check(results[1].file_hash == hashlib.sha256(BODY_B).hexdigest(), "hash B correcto")
        check(1 <= state["max_active"] <= 2,
              "el servidor nunca vio más de dos descargas activas")
        check(all(h.get("sec-fetch-dest") == "image" for h in state["headers"]),
              "Chromium las pidió como imágenes")
        check(all(h.get("referer") == root for h in state["headers"]),
              "ambas nacieron desde la página de origen")

        print("\n[2] Un rechazo no deja basura ni se reintenta")
        check(not denied.ok, "el 403 falla")
        check(denied.error_kind == ErrorKind.AUTH, "el 403 se clasifica como AUTH")
        check("stop test" in (denied.error or ""), "conserva el mensaje del operador")
        check(not (target / "deny.bin").exists(), "no crea archivo de destino")
        check(not list(target.glob("*.tmp")), "no deja .tmp")

        print("\n[3] Cancelar corta el subrecurso y limpia el parcial")
        check(cancelled, "la cancelación llega al llamador")
        check(not (target / "slow.bin").exists(), "no deja destino incompleto")
        check(not (target / "slow.bin.tmp").exists(), "borra el parcial cancelado")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        shutil.rmtree(target, ignore_errors=True)

    print(f"\n{ok} ok, {fail} fallo(s)")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
