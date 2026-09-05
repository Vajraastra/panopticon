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

### `engine.py` — el archivo va al disco según llega, no a la RAM (2026-09-05)

`_do_download` acumulaba el archivo entero en una lista de chunks y al terminar
`_finalize_download` hacía `b"".join(chunks)`, que además duplica el pico porque la lista sigue
viva mientras se construye la copia. Medido con `scripts/cherry_download_toytest.py`: **128 MiB
de pico para un archivo de 64 MiB** — el doble del tamaño, y por descarga concurrente.

No era teórico: 41 de los 46 perfiles tienen el filtro de tipos vacío, así que bajan también
`.zip` y `.psd`. Un pack de 3 GB con los workers ocupados se lleva el proceso por delante.

Ahora el contenido se vuelca al `.tmp` cada `_FLUSH_EVERY` (8 MiB) y el SHA-256 se calcula
incremental, así que el pico es constante: **8.9 MiB para ese mismo archivo de 64 MiB**. El
estado del Range resume pasa a ser `written` —bytes confirmados en el `.tmp`—, que es la posición
exacta desde la que reanudar; `_flush()` se llama antes de evaluar completitud y en los dos
handlers de excepción para que ese invariante se cumpla siempre. `_restart()` reemplaza al
`chunks.clear()` y trunca el `.tmp`. Un `finally` cierra el handle y borra el `.tmp` aunque el
worker se cancele: en Windows un handle abierto impide borrarlo después.

### `engine.py` — el presupuesto de tiempo crece con lo ya bajado (2026-09-05)

El worker pasa `total_timeout=300.0`. Como tope fijo condena a los archivos grandes: un `.zip` de
3 GB no cabe en 300 s, así que al primer tropiezo se aborta por TIMEOUT, pasa a la cola diferida
y allí se le dan otros 300 s — nunca termina, y cada intento cuesta el ancho de banda gastado.
Ahora el presupuesto es `total_timeout + written / _MIN_RATE` (256 KiB/s): segundos de gracia más
lo que costaría bajar a una tasa mínima decente lo ya confirmado. Lo que no progresa se sigue
cortando en seco, porque sin bytes confirmados el presupuesto no crece.

### `engine.py` — el delay entre peticiones ya no se paga dos veces (2026-09-05)

En un reintento se pagaba el backoff exponencial y, encima, el `_delay()` de 10-30 s, porque la
condición miraba solo si había progreso. Ese delay es el espaciado que evita el 429 entre
peticiones distintas; en un reintento ese papel ya lo cumple el backoff. Sumar los dos añadía
10-30 s por error, hasta ~2 min extra en un archivo que agota los reintentos. Ahora el delay
completo se paga solo en el primer intento del archivo.

### `templates/pawchive.py` — sitio nuevo, con Kemono técnicamente muerto (2026-09-05)

Kemono sigue en pie pero ya no deja descargar nada (David, 2026-09-05), y `pawchive.pw` se
declara «a public archiver based on Kemono». La API lo confirma: es la misma, campo por campo
—`/api/v1/creators`, `/api/v1/{service}/user/{id}/posts?o=N`, 50 por página, hash SHA-256 en el
path—. Solo dos servicios: patreon (68.947 creadores) y fanbox (24.625).

Dos cosas que no se ven desde Kemono y hubo que averiguar en vivo:

- **Los archivos van por otro host**, `https://file.pawchive.pw/data{path}?f={nombre}`, con Range.
  El dominio principal devuelve 404 para esas rutas e `img.pawchive.pw` solo sirve miniaturas.
- **`preview_only`** marca contenido indexado pero no importado. Se salta: bajarlo metería en la
  biblioteca una versión degradada que, al deduplicarse por hash del contenido, contaría como un
  archivo distinto del original y no se reemplazaría sola.

**Un archivo a la vez, y no es una preferencia.** El host de archivos responde 403 con un mensaje
escrito por su administrador: *«stop before I block every single one of your IPs, either download
reasonably or contact me with your reasoning, but TBs a day is just abusive»*. De ahí
`max_workers = 1` como tope rígido y `scan_page_delay = 2.0` (Kemono usa 1).

Sin verificar todavía: una descarga real de punta a punta. El host devolvía ese 403 durante toda
la investigación —la API, en otra infraestructura, seguía respondiendo 200— así que el bloqueo
parece ser de IP y previo, no algo que provocara el sondeo: se bajaron 40 KB en total.
