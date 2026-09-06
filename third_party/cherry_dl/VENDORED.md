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

### `download_service.py` + templates — fuentes oficiales antes que archivos (2026-09-06)

Cuando un perfil tiene varias URLs, el orden de alta deja de decidir el orden
de descarga. Los templates oficiales conservan `source_priority = 0` y
Pawchive declara 100 como respaldo. El sort es estable dentro de cada categoría.

Así Patreon, Pixiv y un futuro Fanbox directo llenan primero el catálogo. Los
archivos se procesan después para completar huecos, con Pawchive al final:
como expone SHA-256 antes de descargar, todo contenido que la fuente oficial ya
trajo se descarta contra `catalog.db` sin pedir otra vez el archivo. No se omite
el scan del respaldo por una respuesta oficial aparentemente correcta, porque
otro nivel de membresía o el histórico pueden seguir teniendo huecos.

Kemono queda fuera del circuito: `download_enabled = False`. El template se
conserva para reconocer URLs y catálogos antiguos, pero `download_service` no
le hace scan ni peticiones. Sus filas no se borran ni se modifican.

### `engine.py` + `download_service.py` — pausa entre archivos de Pawchive (2026-09-06)

Una prueba real con 2 workers y caudal reducido volvió a recibir 403 al pasar
de archivos grandes a muchas imágenes pequeñas. Eso separa el problema del
ancho de banda sostenido: la rotación rápida de archivos seguía abriendo una
conexión nueva cada segundo. Pawchive declara ahora `post_download_delay = 5.0`.

La compuerta es global entre workers y se coordina con el pacer: desde la
última descarga completada deben pasar cinco segundos antes de iniciar otro
archivo, y después sigue aplicándose el turno de una petición por segundo. Si
otro worker termina mientras uno esperaba turno, el turno se descarta y la
pausa vuelve a contarse desde esa finalización. Se mantienen además 2 workers,
2 MB/s agregados y 25 GB/día.

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

**El ritmo lo puso su administrador.** El host de archivos empezó respondiendo 403 con un mensaje
escrito a mano: *«stop before I block every single one of your IPs, either download reasonably or
contact me with your reasoning, but TBs a day is just abusive»*. David le escribió, y contestó con
cifras (ver más abajo, 2026-09-05): **1 petición por segundo y 5 descargas simultáneas**. También
aclaró el origen del 403 — alguien martillando sus servidores desde múltiples IPs; el bloqueo era
preventivo y no lo provocó este cliente, que en toda la investigación gastó 40 KB.

Sin verificar todavía: una descarga real de punta a punta.

### `engine.py` + `download_service.py` — ritmo y pool por sitio (2026-09-05)

El acuerdo con Pawchive (1 petición/s, 5 simultáneas) no cabía en el motor tal como estaba, y las
dos mitades fallaban por razones distintas.

**El ritmo.** `_delay()` duerme DENTRO de cada worker, así que N workers a 1 s producen N
peticiones por segundo, no una. Prometer «1 req/s» con 5 workers y un delay de 1 s habría dado 5
req/s — exactamente lo que el administrador pidió no hacer. Se añade `_Pacer`: reparte turnos
sobre un reloj único, bajo lock, y duerme fuera del lock (los workers esperan en paralelo y
despiertan escalonados, en vez de hacer cola detrás del que duerme). El template lo declara con
`min_request_interval` y `download_service` lo activa por fuente con `engine.set_pace()`. Con el
pacer activo pasa por el turno TODA petición del sitio —scan, descarga, reintentos y
continuaciones de rango—, porque un reintento cuesta una petición igual. Medido contra el motor
real: 5 descargas simultáneas tardan 4,01 s, o sea 1/s clavado (`cherry_download_toytest.py`,
bloque 11).

**El pool.** El cap era `min(workers_del_usuario, max_workers)`, y con eso un sitio no podía subir
de lo que dijera el ajuste general: Pawchive habría quedado en 3 aunque su operador autorizara 5.
Ahora `site_workers(cls, requested)` toma el mayor entre lo pedido y lo que recomienda el template,
y lo recorta con `max_workers`. Se saca a nivel de módulo a propósito: el selftest reproducía ese
cálculo copiado y se quedó desfasado sin que ningún test fallara.

El pool y el ritmo se fijan **por fuente**, no una vez por engine (`set_workers`/`set_pace` al
entrar en cada una, y de nuevo por archivo en la cola diferida). Hace falta porque un perfil puede
tener a la vez una fuente de Pawchive (5 simultáneas) y una de Kemono (2), y aplicar el mínimo de
las dos a todo el perfil castigaría a la que sí puede correr. Es seguro exactamente ahí: el bucle
de fuentes es secuencial, así que al reemplazar el semáforo no hay ninguna descarga esperándolo.

Efectos colaterales, los dos deliberados: `templates/kemono.py` gana `max_workers = 2`, que es lo
que su comentario ya decía de facto («>2 activa rate limiting agresivo») y que sin declarar habría
dejado que Kemono subiera a 3; y `pawchive.scan_page_delay` baja a 0, porque el pacer ya espacia
las páginas del scan y sumarle un sleep propio pagaba el peaje dos veces.

### `download_service.py` — un 404 sale de la cola de pendientes (2026-09-05)

Un `not_found` contaba como error y la fila se quedaba en `pending_queue` para siempre: cada sync
volvía a intentarlo, y cada intento gastaba una petición para llegar al mismo 404. Como el scan
refresca la URL antes de descargar (ver `add_pending`, más arriba), un 404 ahí ya no es una firma
vencida: el archivo no está en el origen y no va a volver. Ahora se retira con `remove_pending` y
se cuenta aparte, en `DownloadSummary.dropped`.

Solo `NOT_FOUND`. Un `AUTH` (403) puede ser una sesión caducada y un `URL_EXPIRED` se re-resuelve
en el siguiente scan; retirar cualquiera de esos sí perdería el archivo de verdad. Y lo que se
borra es una fila de cola, nunca un archivo en disco: si el origen lo repone, el próximo scan lo
vuelve a descubrir.

### `download_service.py` + `engine.py` — el motor deja de insistir cuando le dicen que no (2026-09-05)

La primera descarga real de Pawchive encadenó **774 respuestas 403 en trece minutos**, a una
petición por segundo, hasta agotar la cola. El motor trataba cada 403 como problema de ESE archivo
—lo cual es correcto para una sesión caducada— y pasaba al siguiente, así que el rechazo del sitio
no tenía forma de detener nada. Ningún archivo se perdió, pero el servidor recibió 774 peticiones
inútiles después de habernos dicho que no: justo la conducta que provoca el bloqueo siguiente.

Ahora `_BLOCK_STREAK = 5` rechazos **seguidos** (`AUTH`, `CLOUDFLARE` o `RATE_LIMIT`; un 404 no
cuenta, ni un timeout, ni una firma vencida) se leen como «el sitio cerró la puerta»: se emite el
evento nuevo `SiteBlocked`, se abandona el sitio en esa run y las fuentes que queden de él se
saltan sin gastar una petición. La racha se reinicia con **cualquier** descarga exitosa, que es lo
que distingue un sitio caído de cinco archivos con problemas.

Detalle de implementación que no es cosmético: cuando el freno salta, los workers **siguen
vaciando la cola sin tocar la red** en vez de salir del bucle. Salir dejaría al productor colgado
en un `put()` sobre una cola llena sin consumidor, y el cierre —que ya es delicado por los
centinelas— dejaría de funcionar. Los archivos no se tocan: siguen en `pending_queue`.

En `engine.py`, el 403 sin cabeceras de Cloudflare ahora **lee los primeros 300 bytes del cuerpo**
(`read_short_body`) y los adjunta al mensaje de error. El de Pawchive eran 143 bytes de
`text/plain` escritos por una persona, y el motor los descartaba sin mirarlos: era la única
explicación disponible de por qué nos rechazaban.

Banco de pruebas: `scripts/cherry_block_toytest.py`, 11 asserts contra un servidor propio en
localhost que responde 403 a todo. Mide lo que importa —cuántas peticiones llegan de verdad al
servidor— y no una reimplementación de la lógica: 6 en vez de 40 con una fuente, 6 en vez de 120
con tres.

### `engine.py` + `download_service.py` — un techo de caudal y un presupuesto diario (2026-09-05)

El acuerdo con el administrador de Pawchive está escrito en peticiones por segundo, y el motor lo
cumplía al pie de la letra mientras traía **10,7 MB/s**: una petición por segundo con archivos de
130 MB da eso, y sostenido son casi un tera al día. La unidad en la que se quejó él era otra —«TBs
a day is just abusive»— y el motor no tenía forma de expresarla.

**`_ByteBucket`** (engine): cubo de fichas compartido por todos los workers, que se paga con cada
chunk recibido dentro del stream. Se declara por template (`max_bytes_per_sec`) y se fija por
fuente, como el pacer. Un archivo grande vacía el cubo, así que la descarga siguiente arranca
esperando a que se rellene: **el espaciado proporcional al tamaño sale solo**, y por eso NO hay
además un delay por peso del archivo anterior — sería el mismo peaje cobrado dos veces, el error
que ya costó una sesión con `scan_page_delay`.

Dos detalles que no son cosméticos. El cubo se paga **antes** de reprogramar el watchdog de stall:
lo que se duerme adrede no es falta de datos, y contarlo como stall abortaría la descarga por ir
despacio a propósito. Y el presupuesto de tiempo total (`total_timeout + written / _MIN_RATE`) baja
su suelo al caudal cuando hay techo: si no, el motor mataría descargas que van lentas porque se lo
pedimos nosotros.

**`site_budget.py`** (nuevo): tope de bytes por sitio y día natural, en un sidecar propio
`~/.cherry-dl/site_budget.json`. No toca `index.db` ni ningún `catalog.db`, y perderlo no corrompe
nada. Al agotarse, el sitio se abandona en esa run por la misma vía que el freno por rechazos
—`blocked_sites`, `SiteBlocked` con racha 0— y una segunda sync el mismo día no gasta ni una
petición. Un archivo empezado se termina: cortarlo a la mitad gastaría el ancho de banda igual y
encima no dejaría nada.

**Carrera arreglada de paso.** El bloqueo vivía en el contador de racha, y un worker que terminaba
bien un archivo ya en vuelo lo reiniciaba —correcto para lo que la racha mide— **levantando el
bloqueo que otro acababa de poner**. Se veía de forma intermitente como una run que se bloqueaba
y aun así bajaba los 20 archivos. Ahora la verdad es `blocked_sites` y la racha solo detecta.

`set_pace` y `set_rate` conservan el objeto cuando el valor no cambia entre fuentes: rehacerlos
regalaba un turno y un burst enteros por fuente, y un perfil con 43 de Pawchive son 172 MB fuera
de todo límite.

Cifras de Pawchive, que **no** son un acuerdo sino nuestra estimación de lo razonable: 2 MB/s y
25 GB/día. A diferencia de las suyas, estas se pueden mover — hacia abajo sin preguntar.
