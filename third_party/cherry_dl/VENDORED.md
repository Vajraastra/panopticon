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

### Plan de optimización multi-fuente (2026-09-09)

La auditoría posterior a Pawchive fijó seis hitos, que se ejecutarán en sesiones y commits
separados:

1. **Catálogo por lotes y URLs firmadas — cerrado.** Una instantánea reemplaza las consultas por
   archivo y los UPSERTs se confirman en bloques de 250. Las firmas activas se renuevan sin alterar
   `awaiting_archive`, FIFO, cancelación ni reanudación.
2. **Jerarquía y políticas por sitio — cerrado.** Orden explícito y estable: oficiales de pago
   (Patreon/Fanbox), oficiales gratuitos (Pixiv), repositorios (Pawchive/Coomer) y legado
   desactivado (Kemono). API/scan y transferencias usan políticas independientes.
3. **Batch por pasadas globales.** Procesar la categoría activa para todos los proyectos antes
   de avanzar: pago → gratuito → repositorios. Un fallo de una fuente no cancela el proyecto.
4. **Identidad cruzada.** Pawchive ya expone SHA-256 antes de descargar y seguirá siendo la
   prueba principal. Se añadirá una tabla de alias para conservar servicio, publicación, nombre
   original, rol/posición y tamaño; un basename aislado nunca basta para omitir contenido.
5. **Medición viva de Patreon/Pixiv.** O5 registra rendimiento y rechazos por fuente en JSONL.
   Patreon conserva 2 workers y Pixiv sube a 3; API y archivos reutilizan el jitter nativo de
   cada sitio. Pixiv sólo encola ilustraciones estáticas por decisión del usuario.
6. **Sitios nuevos.** Fanbox y Coomer entran solo después de estabilizar la arquitectura común.

O1–O5 quedaron cerrados. O6 se aplaza mientras se reinicia la indexación y se recoge evidencia
operativa con las métricas vivas. Antes de O6 se hará una limpieza auditada de respaldos y archivos
ignorados; no se eliminarán copias por patrón ni sin clasificar primero su función de recuperación.

Los límites de Pawchive se restablecen al cambiar de fuente y no causan directamente la lentitud
de Patreon/Pixiv. La configuración `[templates.<sitio>].workers` ya participa en la política
efectiva y siempre queda por debajo del tope rígido declarado por el sitio.

### `catalog.py` + `download_service.py` — scan por lotes y firmas activas (2026-09-09)

O1 reemplaza las consultas SQLite por elemento del scan con `ScanCatalogBatch`: una sola conexión
carga URLs y hashes descargados, pendientes activos, esperas de Pawchive y el máximo FIFO. Los
pendientes nuevos y las firmas renovadas se escriben en bloques de 250; un conflicto actualiza sólo
`download_url` y `extra_headers`, nunca `queue_position`. Las filas de `awaiting_archive` se omiten.

El bloque incompleto se confirma al salir incluso por cancelación, de modo que un Sync posterior
reanuda desde la cola persistida. `scripts/cherry_catalog_batch_selftest.py` mide una conexión y tres
commits para 503 UPSERTs, además de refresh, FIFO, espera, cancelación y reanudación (521 checks).

### `site_policy.py` + engine/templates — jerarquía y políticas por fase (2026-09-09)

O2 convierte el orden numérico implícito en `SourceCategory`: pago, gratuito, repositorio y legado.
El sort sigue siendo estable dentro de cada categoría. `ScanPolicy` contiene delay, reintentos,
ritmo y enfriamiento de API; `TransferPolicy` contiene workers/tope, delay, reintentos, stall,
ritmo, caudal, pausa post-descarga, presupuesto, ventana/circuito y transporte.

`download_service` activa primero la política de scan y sólo al encontrar cola activa cambia a la
de transferencia. Los diferidos conservan y restauran el contrato completo de su sitio. Patreon y
Pixiv mantienen jitter API propio; desde O5 sus archivos reutilizan ese mismo rango. El override TOML de workers,
antes inerte, ya manda por sitio sin superar `max_workers`. Pawchive conserva exactamente 2 workers,
1 petición/s en ambas fases, 2 MiB/s, 5 s post-descarga, 300/3 h, circuito y navegador CDP. Banco:
`scripts/cherry_site_policy_selftest.py` 29/29; regresiones amplias sin fallos.

### Integración cerrada — autenticación de Pixiv (2026-09-07)

El motor busca `PHPSESSID`, conserva la cookie y lanza `NeedsPixivAuth` cuando falta. Panopticon
ya conecta ese evento con `guided_login_pixiv()`: abre el navegador real mediante el perfil CDP
dedicado, captura y persiste la sesión, resuelve el token del puente y reanuda la fuente. El
recorrido sintético está cubierto en `scripts/cherry_bridge_selftest.py` (99/99); queda validar
una sesión real con la cuenta del usuario.

La auditoría posterior eliminó las restricciones residuales: Pixiv y Patreon descartan el cliente
HTTP que retiene una cookie rechazada, ambos 401/403 entran al resolver común, el paginado puede
renovar sesión y retomarse una vez, y las vistas clásica individual/batch aceptan Pixiv. La red de
seguridad específica vive en `scripts/cherry_auth_selftest.py` (27/27 sin red).

La primera prueba viva encontró que una cookie ya presente en el perfil CDP cerraba el navegador
antes del login. `capture_cookies()` registra ahora el valor inicial y Patreon/Pixiv exigen uno
nuevo antes de aceptar y cerrar. Banco específico actualizado: 27/27. El batch actual serializa
perfiles; una paralelización futura deberá aplicar single-flight a la renovación por sitio.

La segunda prueba viva llegó autenticada a Pixiv (HTTP 200) y descubrió que Pixiv y Patreon
forzaban `Accept-Encoding: br` aunque Brotli no estuviera disponible. Ambos dejaron de imponer
ese header; HTTPX negocia automáticamente una compresión compatible. La consulta real al perfil
104099575 ya obtiene el artista y el banco específico queda en 27/27.

La comprobación de visibilidad mostró después que esa cookie era anónima: se enviaba, pero
`/ajax/user/extra` respondía 401 mientras los endpoints públicos seguían dando 200. Pixiv valida
ahora un `userId` antes de escanear; el capturador CDP no cierra el navegador hasta que esa misma
prueba confirme el login. Si la autenticación se renueva antes del scan, se fuerza un recorrido
histórico completo para recuperar contenido que una ejecución anónima pudiera haber omitido.
Banco específico: 37/37.

La ruta heredada `www.pixiv.net/login.php` mostró “Page unavailable” durante la prueba real.
Aunque todavía redirige, el flujo abre ahora directamente el portal vigente y verificado con 200:
`accounts.pixiv.net/login`, con `return_to` a Pixiv. Banco específico: 38/38.

La captura posterior mostró que el formulario terminaba en `accounts.pixiv.net/error` dentro de
Brave mientras Shields bloqueaba recursos. El servicio seguía respondiendo 200. Para evitar que
los bloqueadores integrados rompan callbacks entre subdominios, Windows prueba ahora Edge y
Chrome antes que Brave; en el equipo validado selecciona Edge. Banco específico: 39/39.

El mismo callback falló después en Edge: no era una caída ni un bloqueo propio de Brave, sino que
la autenticación web por cookies no era una base fiable. El template usa ahora la App API con
OAuth2 PKCE. `guided_login_pixiv()` captura el `code`, intercambia y persiste los tokens; las
ejecuciones siguientes renuevan el access token con el refresh token sin interacción. La prueba
viva autorizó correctamente y consultó el perfil 104099575: primera página de 30 obras y
`next_url` presente. Pruebas: `cherry_pixiv_oauth_selftest.py` 18/18,
`cherry_auth_selftest.py` 21/21 y puente 99/99.

### `catalog.py` + `download_service.py` — los fallos rotan al fondo (2026-09-07)

La cola diferida anterior sólo vivía durante una ejecución: al reiniciar, el orden volvía a
`discovered_at` y un archivo anómalo podía reaparecer al frente. `pending_queue` guarda ahora una
`queue_position` mutable. Todo fallo de archivo la mueve atómicamente después del máximo actual,
sin importar extensión ni clase de error; un 404 confirmado conserva su tratamiento especial y
sale de la cola. La migración asigna posiciones por `ROWID` para no alterar el FIFO existente.
Validación sin red: `cherry_queue_selftest.py` 9/9 y `cherry_block_toytest.py` 33/33.

### `browser_transport.py` — contexto según tipo y cierre acotado (2026-09-07)

Pawchive no sirve todos sus adjuntos bajo un contexto de imagen. Ocho fallos de una ejecución real
eran `.mp4`/`.webm` válidos; una sonda A/B confirmó que un WebM fallaba como `<img>` y respondía
HTTP 206 completo como `<video>`. El transporte elige ahora por extensión entre `<img>`, `<video>`,
`<audio>` y un `<iframe>` genérico, e intercepta cualquier tipo de subrecurso del host de archivos.

Un 206 sólo se considera completo si su `Content-Range` abarca exactamente `0..total-1` y la
longitud coincide. Al terminar el stream también se comparan los bytes escritos; un parcial se
rechaza y se borra. Para evitar que el cierre de Panopticon quede congelado, el comando de cierre,
WebSocket, receptor, tareas auxiliares, proceso y borrado del perfil tienen esperas acotadas.
Validación Chromium: 38/38, incluida una conexión que nunca contesta al cierre.

Por petición expresa, los lienzos `.psd`, `.psb`, `.clip` y `.lip` pertenecen al contexto de
imagen aunque el navegador no pueda renderizarlos: CDP intercepta la respuesta antes de la
decodificación. Las cuatro extensiones también se reconocen en el catálogo local y en el grupo
“Archivos de proyecto”. Banco Chromium actualizado: 41/41.

La auditoría real de Hoovesart descubrió que la query `?f=` tampoco sirve como clave CDP: Chromium
codifica nombres Unicode antes de emitir el evento y una URL con `Nutt’in…` dejaba al worker
esperando 60 s aunque las cabeceras hubieran llegado. Como el path de Pawchive contiene el hash
estable y `?f=` sólo es el nombre sugerido, la correlación ignora esa query. Un WebM real terminó
con 31.994.635 bytes y cero errores sobre una copia del catálogo. Banco Chromium: 43/43.

La ejecución real siguiente confirmó que los tres archivos `Nutt’in…` sí se descargaron completos;
la racha posterior afectó a `Absolute Pin…` y `Mom , what the ...` sin apóstrofes. Un elemento
multimedia se limpia ahora con `pause()`, retirada de `src` y `load()` antes de salir del DOM. Si
Chromium devuelve un fallo sin HTTP, timeout o error de stream, el engine cierra esa sesión y crea
otra para el siguiente intento, evitando heredar recursos dañados. Banco Chromium: 48/48.

### `browser_transport.py` — recupera el 404 oculto de un `<img>` cross-origin (2026-09-07)

Un archivo ausente de Pawchive devuelve una página HTML 404. Cuando se pide como `<img>`, Chromium
entrega a `Fetch.requestPaused` sólo `responseErrorReason=Failed`, sin estado; el motor lo confundía
con la firma del cortacircuitos. Sin embargo, el evento compañero
`Network.responseReceivedExtraInfo` sí conserva `statusCode=404` y las cabeceras. El transporte
ahora correlaciona ambos por `networkId`, espera hasta 500 ms si llegaron invertidos y limpia el
estado auxiliar al terminar. El engine recibe 404, lo clasifica `NOT_FOUND` y el servicio retira
sólo esa fila de `pending_queue`; no alimenta ni escala el guardrail global.

Confirmado con dos hashes reales mediante una petición por archivo y reproducido sin red externa
con un 404 HTML cross-origin en `scripts/cherry_browser_toytest.py`. La sonda viva de una
sola petición queda en `scripts/cherry_cdp_404_probe.py`.

### `browser_transport.py` — una pausa CDP tardía no deja una tarea huérfana (2026-09-06)

En la primera prueba viva del transporte integrado, Chromium emitió alguna
`Fetch.requestPaused` después de que el `<img>` correspondiente ya se hubiera quitado. El receptor
intentaba liberarla con `asyncio.create_task(Fetch.continueRequest(...))`; si Chromium invalidaba
el identificador en esa ventana, nadie recogía la excepción y asyncio imprimía `Task exception
was never retrieved: Invalid InterceptionId`.

Las pausas sin descarga dueña pasan ahora por una tarea registrada que consume ese cierre benigno,
avisa si recibe otro error y se cancela ordenadamente al cerrar el navegador. No cambia la
clasificación HTTP ni la cola: la prueba viva confirmó que los registros recientes tenían archivo
en disco y que no quedaron `.tmp`. `scripts/cherry_browser_toytest.py` fuerza la carrera.

### `browser_transport.py` + `download_service.py` — los fallos dejan diagnóstico útil (2026-09-06)

La prueba viva siguiente tuvo 75 respuestas consecutivas que el visor redujo a `[network]`; el
log persistente solo conservaba el `Network.loadingFailed` que el propio cliente provoca al
abortar la respuesta. Se perdían el estado HTTP y las cabeceras que permiten distinguir un 206,
una redirección, un fallo del servidor y un problema real de CDP.

El transporte registra ahora archivo y target sin query, estado/texto HTTP, MIME, longitud,
`Content-Range`, servidor, redirección, error de red e id de Network. El servicio conserva el
detalle en `panopticon.log` y lo muestra tanto en el intento inicial como en el diferido, en lugar
de enseñar solo `[network]`. Los cuerpos textuales se compactan y limitan; la query —que contiene
el nombre original— no se escribe. Un 206 sigue rechazándose: aceptar una respuesta parcial como
archivo completo corrompería el vídeo. Banco Chromium: 26/26.

### `site_throttle.py` — ventana configurable y sonda automática (2026-09-06/07)

La instrumentación mostró la causa que antes se escondía: después de algo más de 300
transferencias, `file.pawchive.pw` dejó de entregar cabeceras (`reason=Failed` y timeout) y se
recuperó solo horas después. La cuota nueva permite como máximo **300 transferencias exitosas en
cualquier ventana móvil de 3 horas**, global para Pawchive y persistente en
`~/.cherry-dl/site_throttle.json`. Cada worker reserva cupo antes de pedir; dos workers no pueden
cruzar juntos el límite y un intento fallido devuelve su reserva.

Los valores siguen siendo **300 / 3 h** por defecto y la UI los marca expresamente como
recomendados, pero ahora viven en `[pawchive] downloads_per_window` y `window_hours` dentro de
`config.toml`. El cambio sólo afecta a Pawchive. Su techo agregado de **2 MB/s** permanece fijo en
el template; los demás servicios continúan sin límite de caudal propio.

El cortacircuitos cuenta únicamente cinco firmas CDP `no_http_failed` dentro de dos minutos. El
timeout de cabeceras se conserva en el diagnóstico pero no cuenta, porque también puede venir de
una caída momentánea de la red local. Un 404, rechazo HTTP, stream detenido, otra razón de red o
archivo aislado rompe la racha. Enfría 15/30/60/180 minutos y después deja pasar una sola sonda:
éxito reanuda; repetir la firma escala la espera inmediatamente. La cola queda intacta y el sueño
sigue siendo cancelable.
Banco con reloj simulado: `scripts/cherry_throttle_toytest.py`, 15/15.

Un éxito que empezó antes de la apertura del circuito ya no puede cerrarlo después: se registra
para la cuota, pero sólo una sonda posterior al enfriamiento reactiva el sitio. Esto evita la tanda
extra observada cuando un video grande terminó 17 segundos después de que otros cinco intentos
abrieran el corte. Banco con reloj simulado actualizado: 18/18.

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
pausa vuelve a contarse desde esa finalización. Se mantienen además 2 workers y
2 MB/s agregados; Pawchive no tiene un tope diario propio.

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

Cifra de Pawchive que **no** es un acuerdo sino nuestra estimación de lo razonable: 2 MB/s. Puede
moverse hacia abajo sin preguntar. El presupuesto diario quedó desactivado después de validar el
transporte conservador; si el volumen acumulado resulta problemático se reactivará con evidencia.

### `browser_transport.py` — Pawchive descarga como subrecurso real (2026-09-06)

La observación manual y una prueba CDP aislaron la diferencia del 403: no eran cookies. El mismo
archivo daba 200 cuando nacía como imagen desde `https://pawchive.pw/`, con HTTP/2, `Referer` y
`Sec-Fetch-Dest: image`; el GET directo de `httpx` no tenía ese contexto. Una tanda deliberada de
20 imágenes, máximo 2 simultáneas, 2 MB/s y pausa entre parejas dio 20/20 sin rechazo.

Pawchive declara `download_transport = "browser_image"`. El engine abre de forma perezosa Brave
(Edge/Chrome como respaldo) con un perfil temporal vacío, navega una vez al origen y crea un
`<img>` por archivo. No copia cookies, no usa stealth, no cambia de IP y no reintenta dentro del
archivo. La sesión Chromium se comparte entre workers y se cierra con el engine.

Cada orden CDP tiene además un plazo de 15 s. Antes, un standby podía cerrar el WebSocket a medias
y dejar una orden previa al stream esperando el timeout exterior de dos horas; ahora el engine
descarta esa sesión y el siguiente archivo crea un navegador limpio.

El archivo no se recupera con `Network.getResponseBody`: eso lo acumularía entero en RAM. CDP lo
pausa al recibir cabeceras, `Fetch.takeResponseBodyAsStream` entrega el cuerpo e `IO.read` lo
escribe al `.tmp` en fragmentos mientras calcula SHA-256. Conserva el semáforo, pacer, cubo de
bytes, compuerta de 5 s, callbacks de progreso, clasificación de 403/404/429/5xx y limpieza por
cancelación. `websockets` queda declarado directamente en `requirements.txt`.

Banco propio: `scripts/cherry_browser_toytest.py`, servidor localhost y Chromium real. Comprueba
dos streams, bytes y hashes exactos, contexto `image`/referer, límite máximo de dos, cuerpo humano
de un 403, ausencia de retry y limpieza del parcial al cancelar.

### Límite diario global opcional (2026-09-06)

`UserConfig.daily_limit_enabled` está apagado por defecto; `daily_limit_gb` conserva una cifra
configurable (100 GB inicialmente). Ambas GUIs exponen casilla y cantidad, y la CLI comparte el
mismo TOML. El contador `__global__` de `site_budget.json` suma bytes entre todos los servidores y
perfiles. Cuando se agota, el servicio no inicia otro archivo ni escanea la siguiente fuente; una
sync posterior tampoco toca la red hasta el siguiente día. Los archivos ya en curso terminan y
pueden producir un rebase pequeño. Los topes internos por sitio siguen disponibles, pero Pawchive
declara cero: el único tope diario que el usuario puede activar es el global.

### 404 de originales que Pawchive todavía no archivó (2026-09-08)

Un 404 del host de archivos no basta para decidir que el original desapareció. El template abre
la página de la publicación y reconoce exclusivamente “haven't archived this post yet” —incluida
la variante de apóstrofo tipográfico— y “Previews only”. Solo entonces mueve la fila a
`awaiting_archive`, fuera de la cola activa y visible como `◷` en el proyecto.

El botón del inspector devuelve hasta 25 filas a revisión; una tanda vencida también vuelve
automáticamente cada semana. Un post sin esos anuncios conserva el tratamiento de 404 definitivo.
Si la página de diagnóstico falla, la fila permanece activa para no convertir una interrupción
de red en pérdida de catálogo. Banco: `scripts/cherry_archive_waiting_selftest.py`.

### `download_service.py` — ejecución por categoría (2026-09-09)

Panopticon puede limitar una ejecución a un conjunto de `SourceCategory`. El batch usa esa costura
para completar los oficiales de pago de todos los perfiles, luego los oficiales gratuitos y al
final los repositorios. `finalize_profile` reserva `last_checked` para la última pasada; una llamada
normal conserva el comportamiento anterior. Los fallos al resolver una fuente se registran y
saltan sólo esa fuente. La agregación de resúmenes y el orden global pertenecen a
`modules/cherry/logic/download_queue.py`, no al vendor.

### Identidad cruzada de contenido (2026-09-09)

`catalog.db` conserva alias ligados al SHA-256 final con servicio original, publicación, nombre
exacto, rol/posición y tamaño remoto cuando está disponible. El servicio decide en este orden:
hash remoto, identidad semántica segura y, después de transferir, SHA-256 local. Un basename
aislado nunca basta para omitir un archivo. La cola activa y `awaiting_archive` conservan esos
metadatos durante reanudaciones y migran catálogos anteriores de forma aditiva.
