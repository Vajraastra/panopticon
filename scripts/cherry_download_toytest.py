"""
Banco de pruebas del bucle de descarga del motor (`engine._do_download`).

Monta su propio servidor HTTP en 127.0.0.1 y ejercita contra él los caminos que
el motor tiene que sobrevivir: descarga limpia, conexión truncada a mitad
—que obliga a reanudar por Range—, servidor que ignora el Range, y los 403 que
distinguen un bloqueo real de Cloudflare de una denegación del origen.

También mide el techo de memoria y el presupuesto de tiempo de los archivos
grandes, que es donde el motor fallaba en silencio.

Por qué existe: `_do_download` reanuda descargas parciales con una máquina de
estados delicada (`written`, `full_size`, descarte de lo acumulado) y aquí no
hay tests del motor — `tests/` no se copió en el vendorizado. Cualquier cambio
en ese bucle se valida con esto antes de tocar nada real.

No toca la red, ni `~/.cherry-dl`, ni `G:`: todo vive en un temporal que se
borra al terminar.

Uso:  venv/Scripts/python.exe scripts/cherry_download_toytest.py
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
import tempfile
import threading
import time
import tracemalloc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "third_party"))

from cherry_dl.config import NetworkConfig, UserConfig      # noqa: E402
from cherry_dl.engine import DownloadEngine, ErrorKind      # noqa: E402

# La consola de Windows es cp1252: sin esto, el primer ✓ mata el script.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# ── Contadores ────────────────────────────────────────────────────────────────

_ok = 0
_fail = 0


def check(cond: bool, msg: str) -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  ✓ {msg}")
    else:
        _fail += 1
        print(f"  ✗ {msg}")


# ── Cuerpo determinista, para verificar byte a byte ───────────────────────────

_SEED = b"cherry-dl toytest payload "


def body_chunks(size: int, chunk: int = 256 * 1024):
    """
    Genera el cuerpo por trozos, sin materializarlo entero.

    Importa para la prueba de memoria: el servidor vive en un thread de ESTE
    proceso, así que si construyera el archivo completo su pico contaminaría la
    medición del motor — que es justo lo que se quiere medir.
    """
    L = len(_SEED)
    pat = _SEED * (chunk // L + 2)
    sent = 0
    while sent < size:
        n = min(chunk, size - sent)
        off = sent % L
        yield pat[off:off + n]
        sent += n


def body(size: int) -> bytes:
    """El cuerpo completo. Solo para comprobaciones sobre archivos pequeños."""
    return b"".join(body_chunks(size))


def body_hash(size: int) -> str:
    """SHA-256 del cuerpo, por streaming y sin materializarlo."""
    h = hashlib.sha256()
    for part in body_chunks(size):
        h.update(part)
    return h.hexdigest()


# ── Servidor de pruebas ───────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    """
    Rutas, todas con el tamaño en la propia URL:

      /ok/<size>       descarga limpia
      /trunc/<size>    corta la conexión a la mitad; con Range sirve el resto
      /norange/<size>  ignora el Range y siempre devuelve 200 completo
      /slow/<size>     sirve a ~1 MiB/s
      /slowtrunc/<size> sirve lento Y corta a la mitad: obliga a reanudar tarde
      /cf403           403 del origen servido por Cloudflare (texto plano)
      /cfblock         403 con cf-mitigated: bloqueo real de Cloudflare
      /expired/<size>  204 vacío — solo se pide con token-time vencido
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # silencio
        pass

    def _size(self) -> int:
        # El último segmento, sin la query: las URLs firmadas traen ?token-time=
        try:
            return int(self.path.split("?", 1)[0].rsplit("/", 1)[1])
        except (IndexError, ValueError):
            return 1024

    def do_GET(self) -> None:  # noqa: N802  (nombre impuesto por la stdlib)
        path = self.path
        if path.startswith("/cf403"):
            payload = b"Forbidden  "
            self.send_response(403)
            self.send_header("Server", "cloudflare")
            self.send_header("CF-RAY", "a3667c491d37667b-QRO")
            self.send_header("Content-Type", "text/plain;charset=UTF-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if path.startswith("/cfblock"):
            payload = b"<html>challenge</html>"
            self.send_response(403)
            self.send_header("Server", "cloudflare")
            self.send_header("CF-RAY", "a3667c491d37667b-QRO")
            self.send_header("cf-mitigated", "challenge")
            self.send_header("Content-Type", "text/html; charset=UTF-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        size = self._size()
        rng = self.headers.get("Range")

        def send(desde: int, hasta: int, lento: bool = False) -> None:
            """Envía body[desde:hasta] por trozos, sin materializarlo."""
            enviado = 0
            objetivo = hasta - desde
            for part in body_chunks(size, chunk=64 * 1024 if lento else 256 * 1024):
                if lento:
                    time.sleep(0.06)   # ~1 MiB/s
                fin = enviado + len(part)
                if fin > desde:
                    trozo = part[max(0, desde - enviado):]
                    trozo = trozo[:objetivo - max(0, enviado - desde)]
                    if trozo:
                        self.wfile.write(trozo)
                enviado = fin
                if enviado >= hasta:
                    break

        # El servidor que ignora Range: siempre 200 con el archivo entero.
        if path.startswith("/norange"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            send(0, size)
            return

        # Reanudación: el cliente pide desde un offset y se le sirve el resto.
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=", 1)[1].split("-", 1)[0])
            self.send_response(206)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size - start))
            self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
            self.end_headers()
            send(start, size, lento=path.startswith("/slow"))
            return

        # Servidor lento: entrega real pero por debajo de lo cómodo.
        if path.startswith("/slow"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if path.startswith("/slowtrunc"):
                # Lento Y cortado: cuando el motor vuelva a intentar, ya habrá
                # gastado más segundos que el total_timeout fijo.
                send(0, size // 2, lento=True)
                self.close_connection = True
                return
            send(0, size, lento=True)
            return

        # Primer request.
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        if path.startswith("/trunc"):
            # Declara el total pero manda la mitad y cierra: el motor tiene que
            # conservar lo recibido y pedir el resto con Range.
            send(0, size // 2)
            self.close_connection = True
            return
        send(0, size)


def start_server() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ── Configuración del motor: sin esperas, para que el test sea rápido ─────────

def toy_config(tmp: Path) -> UserConfig:
    return UserConfig(
        download_dir=str(tmp),
        workers=2,
        timeout=10,
        network=NetworkConfig(
            delay_min=0.0, delay_max=0.0,
            retries_api=2, retries_file=3,
            stall_timeout=10,
        ),
    )


# ── Pruebas ───────────────────────────────────────────────────────────────────

async def run() -> None:
    srv, base = start_server()
    tmp = Path(tempfile.mkdtemp(prefix="cherry_dltest_"))
    print(f"servidor en {base}\ntemporal   {tmp}\n")

    try:
        cfg = toy_config(tmp)
        async with DownloadEngine(cfg, workers=2) as eng:

            # ── 1. Descarga limpia ────────────────────────────────────────
            print("[1] Descarga limpia")
            size = 512 * 1024
            r = await eng.download(f"{base}/ok/{size}", tmp, "limpia.bin")
            check(r.ok, "termina sin error")
            check(r.file_size == size, f"tamaño correcto ({r.file_size} == {size})")
            dest = tmp / "limpia.bin"
            check(dest.exists(), "el archivo existe en destino")
            check(dest.read_bytes() == body(size), "el contenido es byte a byte el servido")
            check(
                r.file_hash == body_hash(size),
                "el hash SHA-256 coincide con el del contenido",
            )
            check(not (tmp / "limpia.bin.tmp").exists(), "no queda .tmp huérfano")

            # ── 2. Conexión truncada → reanudar por Range ────────────────
            print("\n[2] Conexión truncada a la mitad (reanuda por Range)")
            size = 256 * 1024
            r = await eng.download(f"{base}/trunc/{size}", tmp, "trunc.bin")
            check(r.ok, "termina sin error pese al corte")
            check(r.file_size == size, f"recupera el archivo entero ({r.file_size} == {size})")
            check((tmp / "trunc.bin").read_bytes() == body(size),
                  "el contenido reensamblado es correcto")
            check(r.file_hash == body_hash(size),
                  "el hash del reensamblado coincide")
            check(not (tmp / "trunc.bin.tmp").exists(), "no queda .tmp huérfano")

            # ── 3. Servidor que ignora Range ─────────────────────────────
            print("\n[3] Servidor que ignora el Range (200 en vez de 206)")
            size = 128 * 1024
            r = await eng.download(f"{base}/norange/{size}", tmp, "norange.bin")
            check(r.ok, "termina sin error")
            check((tmp / "norange.bin").read_bytes() == body(size),
                  "no se duplican bytes al descartar el parcial")

            # ── 4. 403 del origen: sin reintentos ────────────────────────
            print("\n[4] 403 del origen (Cloudflare solo como CDN)")
            t0 = asyncio.get_running_loop().time()
            r = await eng.download(f"{base}/cf403", tmp, "denegado.bin")
            dt = asyncio.get_running_loop().time() - t0
            check(not r.ok, "falla, como debe")
            check(r.error_kind == ErrorKind.AUTH,
                  f"clasificado como AUTH, no CLOUDFLARE (fue {r.error_kind})")
            check(dt < 5.0, f"retorna al instante, sin ciclo de esperas ({dt:.2f}s)")
            check(not (tmp / "denegado.bin").exists(), "no deja archivo a medias")

            # ── 5. URL con firma vencida: ni se pide ─────────────────────
            print("\n[5] URL con firma ya vencida")
            t0 = asyncio.get_running_loop().time()
            r = await eng.download(
                f"{base}/ok/1024?token-time=1788566400", tmp, "vencida.bin"
            )
            dt = asyncio.get_running_loop().time() - t0
            check(not r.ok, "falla sin intentarlo")
            check(r.error_kind == ErrorKind.URL_EXPIRED,
                  f"clasificado como URL_EXPIRED (fue {r.error_kind})")
            check(dt < 1.0, f"ni siquiera abre la conexión ({dt:.3f}s)")
            check(ErrorKind.URL_EXPIRED not in ErrorKind.DEFERRABLE,
                  "no es diferible: la cola diferida reusa la misma URL")

            # ── 6. URL firmada vigente: se descarga normal ───────────────
            print("\n[6] URL con firma todavía vigente")
            import time as _t
            futuro = int(_t.time()) + 3600
            r = await eng.download(
                f"{base}/ok/2048?token-time={futuro}", tmp, "vigente.bin"
            )
            check(r.ok, "la firma vigente no estorba")
            check(r.file_size == 2048, "descarga completa")

            # ── 7. Techo de memoria ──────────────────────────────────────
            # El motor no debe acumular el archivo entero en RAM: con archivos
            # grandes (packs .zip, .psd) eso escala con el tamaño y, al unir los
            # chunks, con el DOBLE. Se mide el pico real de asignación.
            print("\n[7] Techo de memoria con un archivo grande")
            size = 64 * 1024 * 1024   # 64 MiB
            esperado = body_hash(size)     # fuera de la ventana medida
            tracemalloc.start()
            r = await eng.download(f"{base}/ok/{size}", tmp, "grande.bin")
            _, pico = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            mb = pico / 2**20
            check(r.ok, "descarga un archivo de 64 MiB")
            check((tmp / "grande.bin").stat().st_size == size, "el tamaño en disco es correcto")
            check(r.file_hash == esperado, "hash correcto")
            print(f"      pico de memoria: {mb:.1f} MiB para un archivo de {size / 2**20:.0f} MiB")
            check(mb < 32, f"el pico NO escala con el tamaño del archivo ({mb:.1f} MiB < 32)")

            # ── 8. Un archivo grande no muere por el timeout total ───────
            # El worker pasa total_timeout=300; con un tope fijo, cualquier
            # archivo que tarde más queda condenado a fallar y re-diferirse.
            print("\n[8] Archivo lento que excede el total_timeout fijo")
            # Sirve lento y corta a la mitad, así que el motor vuelve al bucle
            # cuando ya lleva más segundos que el total_timeout: con un tope fijo
            # aborta ahí; con el presupuesto proporcional continúa, porque para
            # entonces ya tiene bytes confirmados que se lo ganaron.
            size = 3 * 1024 * 1024          # ~3 s a 1 MiB/s
            t0 = asyncio.get_running_loop().time()
            r = await eng.download(f"{base}/slowtrunc/{size}", tmp, "lento.bin",
                                   total_timeout=1.0)
            dt = asyncio.get_running_loop().time() - t0
            check(r.ok, f"termina pese a tardar {dt:.1f}s con total_timeout=1s")
            check(r.file_size == size, "el archivo llega entero")
            check(r.file_hash == body_hash(size), "hash correcto")

            # ── 9. Lo que NO progresa sí se corta ────────────────────────
            # El presupuesto crece con lo bajado; si no baja nada, no crece.
            print("\n[9] El presupuesto no tapa una descarga que no avanza")
            r = await eng.download(f"{base}/cf403", tmp, "nada.bin",
                                   total_timeout=0.001)
            check(not r.ok, "sigue fallando cuando no hay progreso que premiar")

    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{_ok} ok, {_fail} fallo(s)")
    sys.exit(1 if _fail else 0)


if __name__ == "__main__":
    asyncio.run(run())
