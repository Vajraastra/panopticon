"""Sonda CDP de una petición para diagnosticar un archivo de Pawchive.

No toca índices, catálogos ni colas. Abre el origen en un perfil temporal,
solicita el objeto como imagen o vídeo y compara los eventos Fetch/Network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "third_party"))

from cherry_dl.browser_transport import (  # noqa: E402
    BrowserImageTransport,
    _canonical,
    _merge_response_metadata,
    _response_diagnostic,
)


BAD_URL = (
    "https://file.pawchive.pw/data/79/3e/"
    "793e5649764b401558cdc8c4400fe1825e2877c97a6e1033105419543ccf5aab.gif"
)


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


async def probe_headers(
    transport: BrowserImageTransport, url: str, *, element: str = "img",
) -> tuple[int | None, str]:
    """Pide el subrecurso y aborta al recibir cabeceras; no descarga el cuerpo."""
    key = _canonical(url)
    queue = transport._paused[key]
    element_id = "cherry_probe_" + hashlib.md5(url.encode()).hexdigest()
    expression = """(() => {
      const media = document.createElement(%s); media.id = %s;
      media.style.display = 'none'; media.src = %s;
      if (media.tagName === 'VIDEO') media.preload = 'metadata';
      document.body.appendChild(media);
      if (media.tagName === 'VIDEO') media.load();
    })()""" % (json.dumps(element), json.dumps(element_id), json.dumps(url))
    await transport._send("Runtime.evaluate", {"expression": expression})

    try:
        paused = await asyncio.wait_for(queue.get(), timeout=20.0)
    except asyncio.TimeoutError:
        transport._paused.pop(key, None)
        await transport._remove_element(element_id)
        return None, "sin cabeceras después de 20 s"

    request_id = paused["requestId"]
    network_id = paused.get("networkId")
    extra = None
    if paused.get("responseStatusCode") is None and network_id is not None:
        extra = await transport._take_response_extra(network_id)
    else:
        transport._discard_network_metadata(network_id)
    status, headers = _merge_response_metadata(paused, extra)
    diagnostic = _response_diagnostic(paused, headers, status)
    try:
        await transport._send(
            "Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"},
        )
    except Exception:
        pass
    transport._paused.pop(key, None)
    await transport._remove_element(element_id)
    return status, diagnostic


async def main() -> int:
    if "--iframe" in sys.argv[1:]:
        element = "iframe"
    else:
        element = "video" if "--video" in sys.argv[1:] else "img"
    parallel = "--parallel" in sys.argv[1:]
    rate = 2 * 1024 * 1024 if "--rate-2mb" in sys.argv[1:] else 0
    flags = {"--video", "--iframe", "--parallel", "--rate-2mb"}
    probe_urls = [arg for arg in sys.argv[1:] if arg not in flags] or [BAD_URL]
    transport = BrowserImageTransport("https://pawchive.pw/", probe_urls[0])
    try:
        print(
            f"{len(probe_urls)} petición(es) como <{element}>; "
            "sólo cabeceras, sin descargar cuerpos\n"
        )
        await transport.start(rate)
        if element == "video":
            await transport._send("Fetch.disable")
            await transport._send("Fetch.enable", {"patterns": [{
                "urlPattern": transport.file_origin + "/*",
                "requestStage": "Response",
            }]})
        if parallel:
            outcomes = await asyncio.gather(*(
                probe_headers(transport, url, element=element) for url in probe_urls
            ))
        else:
            outcomes = []
            for index, url in enumerate(probe_urls, 1):
                outcomes.append(await probe_headers(transport, url, element=element))
                if index < len(probe_urls):
                    await asyncio.sleep(5.0)
        for index, (url, (status, detail)) in enumerate(
            zip(probe_urls, outcomes), 1,
        ):
            print(f"[{index}/{len(probe_urls)}] {Path(url).name}")
            print(f"  status={status!r} · {detail}\n")
        return 0
    finally:
        await transport.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
