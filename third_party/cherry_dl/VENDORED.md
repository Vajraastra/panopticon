# cherry_dl — copia interna (vendored)

- **Origen:** `E:\githubs\cherry-dl` (repo autónomo). **Ya no es la fuente canónica del motor**
  — ver la regla 1.
- **Commit de origen:** `befa56d` (2026-06-28) — copiado el 2026-07-02.
- **Licencia:** BSL 1.1 (mismo licenciante que Panopticon). Ver `LICENSE` en esta carpeta.

## Reglas

1. **Esta copia diverge del origen a propósito, y los arreglos del motor se hacen AQUÍ**
   (David, 2026-09-05). La dirección de CF2 se invirtió: el standalone se queda **congelado y sin
   tocar** porque es la red de seguridad mientras el módulo no esté al 100%. Arreglar upstream y
   re-copiar habría dejado las dos versiones rotas a la vez.
   - Cada divergencia se anota abajo con su motivo; esa lista es el parche a portar si algún día
     se retoma el standalone.
   - Sigue vigente el espíritu de CF2: **nada de cambios cosméticos ni refactors de gusto.** Se
     toca lo que está roto y se explica por qué.
2. Corre sobre el **pool de dependencias de Panopticon** (`requirements.txt` raíz). Deps
   añadidas por esta copia: httpx[http2], aiosqlite, typer, rich, pydantic, tenacity, qasync,
   browser-cookie3, nodriver. (`textual` NO se instala: la TUI legacy no funciona aquí.)
3. Se importa añadiendo `third_party/` a `sys.path`/`PYTHONPATH` (el paquete usa imports
   relativos; el único absoluto es `__main__.py`).
4. El estado vive en `~/.cherry-dl/` (config, index.db, sesión) — **compartido con el
   standalone**. No ejecutar ambas GUIs a la vez contra los mismos perfiles.
5. El standalone **no se retira ni se modifica** hasta que el módulo funcione al 100%
   (David, 2026-09-05): es la versión a la que volver si algo de aquí se rompe.

## Divergencias respecto a `befa56d`

### `engine.py` — un 403 del CDN ya no se toma por bloqueo de Cloudflare (2026-09-05)

`_is_cloudflare()` daba True con solo ver `cf-ray` o `server: cloudflare`, cabeceras que
`patreonusercontent.com` manda en **todas** sus respuestas. Un 403 del origen —firma vencida, sin
acceso— entraba así en la rama de espera de CF: 60+90+…+240 s ≈ **17 min por archivo** para
fallar igual. Ahora `_is_cloudflare_block()` exige señal de mitigación real (`cf-mitigated`, o al
menos una página HTML); el 403 del origen retorna al instante como `AUTH`.

### `engine.py` — firma de CDN vencida detectada antes de pedir (2026-09-05)

`_signed_url_expiry()` lee el `token-time` que Patreon pone en la URL firmada; si ya pasó, se
falla sin gastar el request ni el delay previo, con el nuevo `ErrorKind.URL_EXPIRED`.

**No es `DEFERRABLE` a propósito:** la cola diferida reusa la misma `file_info.url`, así que
reintentarla en la misma corrida es otro fallo seguro. Al no ser éxito tampoco se llama
`remove_pending`, de modo que el archivo se queda en `pending` y la **próxima sync** lo resuelve
con una firma nueva — que es lo único que lo arregla.

### `catalog.py` — un re-scan refresca la URL guardada en `pending_queue` (2026-09-05)

`add_pending()` insertaba con `INSERT OR IGNORE`: si la `url_source` ya estaba en la cola, la
fila no se tocaba. Como las descargas salen de `pending_queue` y **no** de la respuesta recién
resuelta, una URL firmada de Patreon guardada en un scan viejo llegaba muerta al momento de
descargar, y ningún re-scan la reponía — el archivo quedaba atascado en la cola de forma
permanente. Medido el 2026-09-05: 12 pendientes con la firma vencida, y en dos proyectos
(Hoovesart, BadSpider) eran el 100% de su cola, así que no podían terminar nunca.

Ahora es un UPSERT que refresca `download_url` y `extra_headers`, con un `WHERE` que evita
reescribir la fila cuando nada cambió (el caso normal en sitios que direccionan por hash, donde
la URL es estable). `discovered_at` se conserva a propósito: sigue marcando el primer avistaje.
