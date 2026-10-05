# cherry_dl — copia interna (vendored)

- **Origen histórico:** `E:\githubs\cherry-dl` (repo autónomo). Esa carpeta no existe
  en la ubicación indicada; no se ha verificado una ubicación archivada del standalone.
  El motor canónico está aquí — ver la regla 1.
- **Commit de origen:** `befa56d` (2026-06-28) — copiado el 2026-07-02.
- **Licencia:** BSL 1.1 (mismo licenciante que Panopticon). Ver `LICENSE` en esta carpeta.

## Reglas

1. **Esta copia diverge del origen a propósito, y los arreglos del motor se hacen AQUÍ**
   (David, 2026-09-05). La integración se oficializó el 2026-09-16: Panopticon es la
   fuente única y su interfaz aloja el runtime Cherry. La antigua protección mediante
   un standalone congelado no acredita un respaldo disponible en disco.
   - Cada divergencia se anota abajo con su motivo, conservando la procedencia del motor.
   - Sigue vigente el espíritu de CF2: **nada de cambios cosméticos ni refactors de gusto.** Se
     toca lo que está roto y se explica por qué.
2. Corre sobre el **pool de dependencias de Panopticon** (`requirements.txt` raíz). Deps
   añadidas por esta copia: httpx[http2], aiosqlite, typer, rich, pydantic, tenacity,
   browser-cookie3, nodriver.
3. Se importa añadiendo `third_party/` a `sys.path`/`PYTHONPATH` (el paquete usa imports
   relativos; el único absoluto es `__main__.py`).
4. El estado vive en `~/.cherry-dl/` (config, index.db, sesión). Esta carpeta de datos
   no es un respaldo del código. No ejecutar otro cliente contra los mismos perfiles
   mientras Panopticon los esté usando.
5. No reconstruir un standalone ni declarar una copia archivada como respaldo sin
   localizarla y verificar su procedencia. La ruta histórica por sí sola no basta.

## Divergencias respecto a `befa56d`

### HTTP sin tamaño y continuación por cuota (David, 2026-10-05)

El guard inicial de af9d659 descartaba cuerpos chunked o sin Content-Length,
dejando originales válidos aplazados en cada Sync. David pidió iniciarlos y
conservar su parcial durante una pausa por cuota, reanudando después.

`raw_read_guard.py` limita la lectura HTTP/1 antes del socket según la reserva
del intento. ContextVar separa workers; el backend se instala antes de scans y
conserva el límite al pasar a TLS. La integración privada pool/network_backend
de HTTPX está aislada y se verifica con TCP local y delegación TLS. No se cambia
ninguna dependencia instalada, catálogo Cherry, biblioteca ni sesión autenticada.

Al agotarse la reserva, el engine cierra HTTP, vuelca el parcial y espera volumen
sin watchdog ni timeout absoluto de dos horas. Reanuda con Range/If-Range,
comprobando posición e identidad; solicita encoding identity y verifica SHA-256
del productor cuando existe. Con origen sin identidad segura o Range ignorado,
reinicia; no mezcla representaciones. El EOF desconocido al agotar el cupo se
resuelve por SHA o solapamiento validado de un byte. Sólo el archivo completado
cuenta como éxito; todo cuerpo recibido, incluidos solapamientos/reinicios, cuenta
como volumen. Cancelar/cerrar conserva el comportamiento previo de borrar el tmp;
esto no implementa continuación durable entre sesiones.

`awaiting_archive` conserva su circuito de revisión semanal/manual independiente:
la prueba integrada recibe 404 chunked, conserva la espera sin sumar un éxito,
repite Sync sin reactivarlo y descarga al publicar/revisar explícitamente.
Banco: `scripts/cherry_stream_resume_selftest.py`, sólo servidor/datos privados.

### Pawchive: volumen crudo y cuota adaptativa (David, 2026-10-05)

David confirmó las pruebas vivas de los avisos anteriores y autorizó distinguir
volumen crudo recibido de archivos terminados correctamente. Al llegar al umbral
de archivos con más del 50% del volumen disponible, el motor continúa a 2 MiB/s;
vuelve al caudal normal de 4 MiB/s al bajar al 80% del umbral (400/500), o a un
margen de archivos más estricto configurado. Los éxitos económicos siguen contando.
Al agotarse el volumen o no caber el siguiente archivo, espera a recuperar al
menos el 50% del volumen y capacidad suficiente para ese intento; reanuda en
económico si aún no recuperó los archivos. Ventana y volumen siguen configurables.

`raw_traffic.py` conserva cuerpos HTTP crudos por segundo en un ledger propio,
con reservas durables: fallos, reintentos, Range ignorado y cancelación no devuelven
bytes ya recibidos. No se cambia ningún catálogo Cherry. Ante una salida no limpia,
las reservas no reconocidas se contabilizan conservadoramente al reiniciar; ante
un error de escritura no se admite más tráfico ni se borra esa evidencia.
El historial previo de éxitos se importa una sola vez; no inventa tráfico de
intentos antiguos. La restricción inicial de Content-Length se sustituyó por
lecturas acotadas y continuación por cuota; ver la divergencia anterior.

El engine observa el stream antes de decodificarlo o esperar por caudal/progreso.
Cambiar la velocidad conserva deuda y tokens del bucket compartido. El medidor
separa expiración de archivos, bytes y objetivos de recuperación, sin sumar dos
veces el progreso activo. `adaptive_quota=false` conserva la pausa rígida por
archivos para diagnóstico; el volumen permanece crudo. Pruebas privadas:
`scripts/cherry_adaptive_quota_selftest.py`; la política nueva requiere uso vivo.

### Patreon: contrato de paginación y retiro de API pública v1 (2026-10-03)

Cherry usa el sitio autenticado: `/api/campaigns?filter[vanity]` y
`/api/posts?filter[campaign_id]`, cookie `session_id` obtenida por login guiado,
y URLs de medios/adjuntos entregadas por el servidor. No usa OAuth público,
`/api/oauth2/api`, webhooks ni lectura de comentarios/repositorios externos.
La retirada del 7 de octubre se refiere a la API pública v1, no a esas rutas.
La API pública v2 requiere OAuth y `campaigns.posts`; su contrato publicado
de Post no ofrece las relaciones images/audio/video/attachments que usa este
consumidor. No hay paridad verificada que justifique sustituir la sesión vigente.
Referencia revisada: https://docs.patreon.com/#migrating-from-api-v1-to-api-v2
y https://docs.patreon.com/#post-v2 . Esto no garantiza estabilidad futura del sitio.

Una página sin `data`, con errores o continuación inválida podía parecer fin
normal, adelantando la frontera; un cursor repetido o 429 persistente podía
retener el recorrido indefinidamente. Se rechazan páginas/campañas incoherentes,
continuaciones ajenas y ciclos, y se acotan los 429 consecutivos a tres intentos.
401/403 conservan el login guiado; no se consulta otro dominio ni se cambia
la frontera ante esos fallos. Contratos sintéticos, sin credenciales ni red:
`scripts/cherry_patreon_contract_selftest.py`.

### Esperas de cuota y actividad de transferencias (2026-10-04)

Las reservas transitorias disparaban avisos de cuota con cada archivo y un
enfriamiento global que seguía visible al reanudarse la transferencia. El motor
emite ahora estado por trabajador (preparando, transfiriendo, esperando, inactivo),
con motivo y vencimiento absoluto; incluye esperas sin plazo por reservas/sondas.
La consola anuncia pausa sólo cuando todos los trabajadores restantes esperan,
y reanudación al admitir una transferencia. No repite avisos al liberar reservas.
La interfaz distingue pausa de cuota, pausa posterior al escaneo y transferencia
sin bytes recientes; los contadores usan hora de vencimiento, no ticks de Qt.
El modo sin espera también difiere reservas sin plazo en vez de quedarse dormido.
Sin cambios en topes, ventana, historial ni biblioteca. Banco aislado con HTTP
local, reinicio con cuota llena, cancelación y reloj adelantado:
`scripts/cherry_wait_status_selftest.py`.

### Mantenimiento: progreso por fases y escrituras por diferencias (2026-10-03)

El mantenimiento conserva proyecto/fase/avance en la GUI con eventos limitados
a cinco por segundo dentro de cada fase, más sus límites. El inventario excluye
directorios internos y cede al runtime para atender cancelación. La API de progreso
es opcional y no depende de Qt. La consola conserva transiciones de fase.
El catálogo compara nombre, tamaño y contador dentro de la transacción y escribe
sólo filas y metadatos modificados; los recibos idénticos tampoco se reescriben.
Se mantienen planes, respaldos, historial y verificación final para reanudación.
Motivo: las corridas sin cambios emitían por archivo y repetían dos escrituras
por copia conservada, ocultando el proyecto activo y amplificando el trabajo.

### Pawchive: caudal compatible con la ventana y reserva estricta (2026-10-02)

David autorizó 4 MiB/s agregados y el default de 25 GiB / 2 horas; 2 MiB/s
sólo permitían 14,06 GiB en ese tiempo. Se conservan dos workers, 1 petición/s,
500 archivos y el margen de reanudación por archivos. El User-Agent y Ajustes
declaran la política nueva, sin cambiar la configuración existente del usuario.

Un archivo anunciado no inicia si consumo + reservas + tamaño supera la cuota:
24,5 GiB + 700 MiB espera a que expire consumo real de la ventana móvil.
Se retira la excepción que dejaba pasar archivos mayores que toda la cuota:
éstos quedan diferidos y requieren aumentar explícitamente el presupuesto.
Sin tamaño anunciado se reserva el espacio libre; el transporte HTTP verifica
cabeceras y cuerpo contra la reserva antes de publicar. Las cabeceras corrigen
la reserva si el tamaño real cabe, liberando espacio para el otro worker. Tamaños inconsistentes
o cuerpos que no caben quedan diferidos para otra Sync, sin parcial publicado.
El contrato conserva el conteo de transferencias completadas; no redefine el
consumo de red de intentos fallidos ni garantiza cuota si faltan archivos elegibles.

Las esperas con fecha de vencimiento también despiertan cuando cambia una reserva:
redimensionar por cabeceras, terminar, fallar o abandonar un archivo obliga a
revalidar el cupo. La prueba visible expuso que un segundo worker podía dormir
hasta vencer consumo histórico aunque el primero ya hubiera liberado espacio.
El aviso distingue cupo ocupado por consumo o reservas de una cuota consumida;
el techo estricto se conserva y la cancelación recoge el temporizador auxiliar.


### Frontera incremental confirmada por descubrimiento (2026-10-01)

Guardar `artist_id` o `file_count` adelantaba `last_synced` antes de terminar
el recorrido remoto y podía saltar publicaciones después de una interrupción.
La API vendorizada sólo mueve esa frontera cuando recibe explícitamente la
hora UTC de inicio de un recorrido completo. Primero confirma y cierra todos
los lotes del catálogo; errores de paginación, autenticación o flush y la
cancelación durante el recorrido conservan la frontera anterior. El cierre
del catálogo se recoge incluso ante cancelaciones repetidas, sin tareas sueltas.
Una transacción del índice compara fechas UTC y evita retroceder la frontera
cuando termina después un recorrido iniciado antes. No cambia el esquema.

El descubrimiento y las transferencias tienen fronteras distintas: una descarga
cancelada después de confirmar el recorrido conserva su cola durable y la
frontera de descubrimiento. Un recorrido vacío que llega al final también es
válido. Sin prueba `full_scan_done`, una fecha heredada no acredita cobertura
histórica y se recorre todo el historial. El espejo portable sigue usando
`profile_meta`; si falla, su fecha anterior obliga a repetir, sin perder la cola.
Catálogo e índice son DBs separadas: este orden evita saltos, no constituye
una transacción SQLite entre ambas.

Pruebas privadas sin red: `cherry_scan_frontier_selftest.py`. Los perfiles que ya
traían una frontera incorrecta y `full_scan_done=1` requieren revisión/Rescan:
la fecha almacenada no permite deducir qué publicaciones faltan. No se alteraron
catálogos reales. Las fechas retroactivas del servidor y los cambios remotos
durante la paginación quedan fuera de la garantía del contrato incremental.

### `site_throttle.py` — ventana móvil por archivos o GB (2026-09-10)

Pawchive se detiene cuando ocurre primero uno de dos topes configurables dentro
de la misma ventana: cantidad de transferencias completadas o GB transferidos.
El tamaño remoto anunciado se reserva antes de iniciar cada archivo, evitando
que los dos workers crucen juntos el presupuesto con ZIP/RAR grandes; los
archivos sin tamaño conocido registran sus bytes reales al terminar.

El estado persistente conserva compatibilidad con ejecuciones anteriores. En
una descarga individual y en la pasada final de repositorios la cuota espera de
forma cancelable; al vencer reanuda el mismo proyecto y continúa automáticamente
con los demás. Las categorías anteriores del batch todavía pueden diferirse para
no congelar el trabajo restante. Los archivos fallidos mantienen su
comportamiento FIFO: pasan al fondo mediante `queue_position`, mientras una
cuota no altera sus posiciones.

Valor actual: 500 archivos o 25 GiB cada 2 horas, lo que ocurra primero. Ambos
límites y las horas se editan desde Ajustes.

### Nombre de artista desde una fuente Pawchive (2026-09-09)

El alta de un proyecto ya no depende primero del índice global de creadores de Pawchive. Consulta
la página individual (~75 KB), lee su `meta artist_name` y conserva el índice de ~13 MB sólo como
respaldo compatible. El flujo público aplica la política de scan del sitio, por lo que la consulta
usa el ritmo Pawchive de 1 petición/s en vez del delay genérico de 10–30 s. Verificado en vivo con
`patreon/user/600910`: `Kobi-Tfs` en 0,96 s. Regresión Pawchive: 71/71.
David confirmó después desde la interfaz que la obtención del nombre funciona en uso real.

### Disponibilidad persistente de fuentes (2026-09-09)

`profile_urls` distingue una casilla desactivada por el usuario de una fuente vetada
automáticamente. Los batch y Rescan desmarcan una fuente que no responde antes de entregar
contenido o que sigue sin acceso después de renovar sesión. Sync simple la omite; el usuario puede
reactivarla y un Rescan vuelve a probar las vetadas automáticamente. Un scan correcto las activa de
nuevo y las desactivadas manualmente nunca se sondean. El estado y su motivo viajan también en
`profile_meta`. Regresión: `scripts/cherry_batch_planner_selftest.py` (34/34).

### Ciclo de vida robusto entre proyectos y standby (2026-09-09)

La cola productor/workers ya no interpreta 120 segundos de backpressure como fallo: espera espacio
y, si una tarea muere inesperadamente, cancela y recoge el grupo para no dejar consumidores sin
centinela. El transporte CDP verifica receptor y proceso al reanudar, reconstruye la sesión muerta
tras standby y despierta las órdenes pendientes cuando cierra el WebSocket. Un 206 incompleto
descarta esa sesión; el transporte HTTP cuenta parciales repetidos sin progreso hacia el límite de
reintentos. Regresión dedicada: `scripts/cherry_lifecycle_selftest.py` (9/9).

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

La primera observación viva completa con RuiDX confirmó que Patreon entrega y Cherry cataloga las
publicaciones recientes. La aparente detención en marzo era una confusión entre fecha de descarga
y fecha de publicación durante un rescan histórico. Quedan dos ajustes generales fuera de O:
confirmar `last_synced` sólo al terminar el scan y conservar ambas cronologías del archivo
(modificación = publicación; creación = descarga), con migración histórica sin retransferencias.

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
de transferencia. Los pendientes de sesiones anteriores usan el contrato completo de su sitio. Patreon y
Pixiv mantienen jitter API propio; desde O5 sus archivos reutilizan ese mismo rango. El override TOML de workers,
antes inerte, ya manda por sitio sin superar `max_workers`. Pawchive conserva exactamente 2 workers,
1 petición/s en ambas fases, 2 MiB/s, 5 s post-descarga, 500/3 h, circuito y navegador CDP. Banco:
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

### `catalog.py` + `download_service.py` — diferidos entre sesiones (2026-09-07; 2026-09-24)

`pending_queue` guarda una `queue_position` mutable. Todo fallo de archivo la mueve atómicamente
después del máximo actual, sin importar extensión ni clase de error; un 404 confirmado conserva su
tratamiento especial y sale de la cola. La migración asigna posiciones por `ROWID` para no alterar
el FIFO existente.

La lista temporal que reintentaba todos los fallos al final de la misma ejecución se retiró: cada
pendiente tiene como máximo una vuelta por Sync. `deferred_attempts` persiste que una fila ya falló
en una sesión anterior. Se vuelve a intentar y, si falla, rota otra vez, pero no alimenta la ventana
de cinco rechazos que interpreta una caída del sitio. Una petición explícita del operador para
detenerse conserva prioridad y sí corta la ejecución. Si el scan entrega una URL o hash nuevos, el
marcador se limpia para evaluar el recurso renovado normalmente. Validación: cola 14/14, catálogo
521/521 y `cherry_block_toytest.py` 39/39.

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
detalle en `panopticon.log` y lo muestra en cada intento, incluida la siguiente Sync, en lugar
de enseñar solo `[network]`. Los cuerpos textuales se compactan y limitan; la query —que contiene
el nombre original— no se escribe. Un 206 sigue rechazándose: aceptar una respuesta parcial como
archivo completo corrompería el vídeo. Banco Chromium: 26/26.

### `site_throttle.py` — ventana configurable y sonda automática (2026-09-06/07)

La instrumentación mostró la causa que antes se escondía: después de una tanda grande,
`file.pawchive.pw` dejó de entregar cabeceras (`reason=Failed` y timeout) y se recuperó solo horas
después. La cuota nueva limita las transferencias exitosas por cantidad de archivos y bytes en
una ventana móvil configurable, global para Pawchive y persistente en
`~/.cherry-dl/site_throttle.json`. Cada worker reserva cupo antes de pedir; dos workers no pueden
cruzar juntos el límite y un intento fallido devuelve su reserva.

Los valores recomendados son **500 archivos o 25 GiB / 2 h** y viven en `[pawchive]`
(`downloads_per_window`, `gigabytes_per_window`, `window_hours`) dentro de `config.toml`. El cambio
sólo afecta a Pawchive. Su techo agregado de **4 MiB/s** permanece fijo en el template; los demás
servicios continúan bajo sus propias políticas.

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
4 MiB/s agregados (política actualizada el 2026-10-02); Pawchive no tiene un tope diario propio.

### `engine.py` — un 403 del CDN ya no se toma por bloqueo de Cloudflare (2026-09-05)

`_is_cloudflare()` daba True con solo ver `cf-ray` o `server: cloudflare`, cabeceras que
`patreonusercontent.com` manda en **todas** sus respuestas. Un 403 del origen —firma vencida, sin
acceso— entraba así en la rama de espera de CF: 60+90+…+240 s ≈ **17 min por archivo** para
fallar igual. Ahora `_is_cloudflare_block()` exige señal de mitigación real (`cf-mitigated`, o al
menos una página HTML); el 403 del origen retorna al instante como `AUTH`.

### `engine.py` — firma de CDN vencida detectada antes de pedir (2026-09-05)

`_signed_url_expiry()` lee el `token-time` que Patreon pone en la URL firmada; si ya pasó, se
falla sin gastar el request ni el delay previo, con el nuevo `ErrorKind.URL_EXPIRED`.

Al no ser éxito no se llama `remove_pending`: el archivo permanece en `pending` y la **próxima
Sync** lo resuelve con una firma nueva, que es lo único que puede arreglarlo. Ningún error temporal
tiene ya un segundo intento al final de la misma corrida.

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
3 GB no cabe en 300 s, así que al primer tropiezo se abortaba por TIMEOUT y cada nueva ejecución
volvía a pagar el ancho de banda sin posibilidad real de terminar.
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
- **Preview-only es sólo estado (revertido 2026-09-10):** el listado vivo usa
  `preview_state: "pending"` / `has_full: false`; también se reconoce el `preview_only` legado.
  Se guarda un marcador en `preview_only`, nunca una miniatura: no entra en la cola, no consume
  ancho de banda y no reserva contador. Al aparecer el original, su identidad canónica retira el
  marcador y entra a `pending_queue`. `scripts/cherry_preview_cleanup.py` revierte los archivos
  provisionales ya descargados con respaldo de DB y cuarentena recuperable.

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
entrar en cada una). Hace falta porque un perfil puede
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

Cifra de Pawchive que **no** es un acuerdo sino nuestra estimación de lo razonable: 4 MiB/s
(política actualizada el 2026-10-02; validación viva pendiente). Puede
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

### Cierre CDP durante una transferencia grande (2026-09-10)

Un ZIP de 216 MB alcanzó cabeceras válidas (`HTTP 200`) y después perdió el WebSocket CDP.
El primer worker se difería correctamente, pero otro que esperaba cabeceras en la misma sesión
consumía los 60 s completos porque esa espera sólo observaba su cola de respuestas. Ahora espera
a la vez la respuesta y la tarea receptora: si CDP termina, todos los workers afectados despiertan
de inmediato y conservan sus archivos en la cola persistente.

La invalidación también queda ligada a la instancia concreta que falló. Un worker atrasado ya no
puede cerrar el navegador de reemplazo que otro worker haya creado mientras procesaba el fallo.
Regresiones: `scripts/cherry_lifecycle_selftest.py` (13/13) y navegador real
`scripts/cherry_browser_toytest.py` (51/51).

### Confirmación de sonda y espera selectiva del batch (2026-09-10; 2026-09-24)

Un `no_http_failed` durante la sonda del cortacircuitos se repite una sola vez después de que el
engine descarte la sesión Chromium afectada. Sólo el resultado de esa sesión limpia decide si el
cooldown aumenta. Las descargas ordinarias no se duplican. En ejecución batch, las categorías
oficiales adquieren la cuota/circuito con `wait=False`; si no hay permiso, conservan la fila y
continúan el trabajo prioritario. La pasada de repositorios usa `wait=True`: como es la última
pasada global, Pawchive conserva el proyecto activo, muestra el contador, espera de forma
cancelable y reanuda la cola al vencer la ventana en vez de declarar terminado el lote.

### Pawchive vuelve a HTTP identificable (2026-09-16)

El operador cambió el host de archivos y pidió que las herramientas se identifiquen mediante
`User-Agent` y no finjan ser navegadores. El transporte Chromium/CDP empezó a devolver
`reason=Failed` sin estado HTTP después de una tanda grande, aunque página y API seguían en 200.
La misma URL fallida respondió `206 Partial Content` a una petición HTTP directa de un byte con
un `User-Agent` `Cherry-DL/0.1` enlazado al repositorio.

Pawchive usa ahora el transporte HTTP normal con ese identificador. El mismo `User-Agent` declara
la política efectiva de la ejecución en inglés legible (caudal sostenido, concurrencia,
archivos/GB y horas
de ventana), de modo que el operador pueda verla en sus registros incluso cuando David cambie
Ajustes. Se conserva
`Referer`, ritmo, pool, caudal, pausa y ventana. Se retira la firma `no_http_failed`: pertenecía
a CDP y no debe convertir un fallo local de red en un bloqueo remoto cuando HTTP ya entrega
estados reales. Al construir la nueva política se limpia cualquier circuito CDP persistido, sin
borrar las transferencias que alimentan la ventana de archivos/GB.

Las filas que ya estaban en `pending_queue` conservaban el JSON de encabezados capturado al
descubrirlas y, por tanto, sólo enviaban `Referer`. El servicio aplica ahora el hook
`transfer_headers()` al reconstruir cada `FileInfo`: Pawchive inyecta el `User-Agent` vigente justo
antes de descargar, también para colas antiguas y reintentos de sesiones posteriores. No se reescriben
manualmente las bases ni se depende de que el scan vuelva a insertar la fila.

### `site_throttle.py` + UI nativa — medidor visual de cuota Pawchive (2026-09-16)

`QuotaSnapshot` expone una lectura no destructiva del mismo estado móvil que bloquea las
descargas: bytes y archivos terminados, reservas activas y tiempo hasta la próxima liberación.
La consola lo muestra siempre y lo actualiza durante las transferencias mediante `QuotaStatus`;
los bytes en vuelo se señalan como provisionales y sólo pasan al total persistido al terminar el
archivo. La lectura y el evento existen únicamente para la cuota configurada de Pawchive: ningún
límite ni contador se comparte con otros sitios.

### `site_throttle.py` — margen configurable antes de reanudar Pawchive (2026-09-24)

Al alcanzar el límite de archivos, la ventana ya no reanuda con cada slot individual. Persiste
un estado de recuperación y espera hasta que hayan vencido `resume_headroom` transferencias
reales; el valor se ajusta en la sección Pawchive de la interfaz y vale 50 por defecto. Cero
conserva el comportamiento anterior. El techo de archivos y la ventana móvil de GB no cambian:
el margen sólo decide cuánta capacidad libre debe acumularse antes de volver a conceder permisos.
El medidor muestra el umbral efectivo y el tiempo hasta la transferencia que completa el margen.

## Organización manual ZIP/RAR por proyecto (2026-09-10)

archives.py reutiliza next_counter, build_filename y add_file para incorporar los miembros
extraídos. Conserva el registro original del comprimido y un manifiesto lateral, verifica
las copias físicas antes de borrar y recupera incorporaciones/movimientos interrumpidos.
archive_reader.py aísla ZIP y UnRAR incluido en el venv, con límites de extracción y cierre
del proceso al cancelar. No se engancha a download_service ni cambia el schema del catálogo.

catalog.relocate_file mantiene las rutas al organizar por tipo; migrate_unique_files
transporta los recibos completos y sus alias para conservar la omisión de descargas.
La UI nativa ofrece la acción sólo para el proyecto seleccionado, con exclusión mutua
frente a descargas, preparación y fusión. Instalación por requirements/run.bat.

Verificación Windows: comprimidos/interfaz 55/55, catálogo 521/521, alias 39/39,
fusión 38/38 e interfaz existente 20/20. Tooltips Cherry 32/32; prueba RAR sin PATH.

Organización de proyectos (2026-09-10): `organize_project()` crea las subcarpetas
solo cuando hacen falta y actualiza el catálogo sin extraer. Las imágenes detectables
por firma permanecen en la raíz aunque no tengan extensión. Prueba combinada: 60/60.

Enrutamiento automático (2026-09-11): `file_routing.py` concentra la misma clasificación
para mantenimiento y descarga. Al iniciar un proyecto, el servicio reubica los archivos
catalogados antiguos que quedaron en la raíz y crea las carpetas no visuales elegidas en
el filtro. Las descargas nuevas escriben directamente en su carpeta y `files.filename`
guarda la ruta relativa; imágenes y animaciones permanecen en la raíz. La reparación no
extrae ni borra paquetes y, si falla, informa el problema sin impedir nuevas descargas.
Verificación: archivos/interfaz 65/65, puente 99/99, planificador 36/36, motor 44/44 y
catálogo 521/521.

### Organización y nomenclatura v2 (2026-09-11)

Las imágenes y animaciones son el contenido primario: permanecen en la raíz y conservan
`Artista_00001.ext`. Los demás tipos viven en `Comprimidos`, `Proyectos`, `Videos`, `Audio`,
`Documentos` u `Otros`, con nombre `tipo-Artista-00001.ext` (`arc`, `pro`, `vid`, `aud`,
`doc`, `otr`) y contador independiente por categoría. PSD, PSB, CLIP y LIP comparten
`Proyectos`; los comprimidos conservan su identidad aunque después se extraigan a imágenes.

No se añadió una columna de tipo ni se cambió el schema: la categoría se deriva de la
extensión y `catalog.db.meta` guarda `layout_version=2` y `counter:<categoría>`. Un catálogo
antiguo no vacío queda bloqueado para descargar hasta migrarlo; uno nuevo crea el layout y
las subcarpetas seleccionadas automáticamente. CLI, importador, extracción, descarga
nativa y fusión usan la misma ruta centralizada.

`layout_migration.py` y `scripts/cherry_layout_v2.py` hacen plan, respaldo, staging,
actualización atómica del catálogo y reanudación tras interrupción. La biblioteca real se
migró con respaldo por proyecto: 65 proyectos, 116,738 filas, 2,855 rutas actualizadas y
2,189 archivos físicos renombrados, sin errores. Una segunda auditoría produjo cero cambios.
Verificación temporal: archivos/importador/interfaz 88/88, catálogo 521/521, fusión 38/38,
puente 99/99, planificador 36/36, motor 44/44 y pulida funcional 23/23.

### Panopticon pasa a ser la única interfaz (2026-09-16)

La UI nativa ya cubre altas, fuentes, ajustes, autenticación, descarga, mantenimiento y fusión.
Se retiraron la GUI qasync, la TUI Textual, sus comandos `gui`/`tui`, el launcher transicional y
la dependencia `qasync`. El CLI se conserva únicamente para mantenimiento y migraciones.

La única capacidad no portada era clasificar en lote todos los pares por solapamiento de hashes;
se aceptó diferirla a M4, que resolverá la misma pregunta sobre `panopticon.db`. La comparación de
una fusión concreta sigue mostrando hashes compartidos antes de tocar archivos.

Pawchive recibe `Cherry-DL/0.1` como identidad del descargador. El enlace de contacto sigue
apuntando al repositorio de Panopticon, pero el producto declarado al servidor ya no es Panopticon.

### Mantenimiento completo por proyecto (2026-09-17)

`archives.process_project()` ya no termina al extraer ZIP/RAR: invoca la misma operación
recuperable que usará el batch. La primera ejecución reconcilia todos los archivos numerados con
los SHA-256 ya verificados de `catalog.db`, calcula sólo físicos no registrados o con tamaño
distinto, conserva la copia exacta con contador menor y compacta por separado los siete contadores
del layout v2. Una auditoría forzada relee cada byte cuando se necesite comprobar modificaciones
externas; el modo normal sigue detectando incorporaciones manuales sin volver a leer cientos de GB.

`project_maintenance.py` persiste plan, respaldo SQLite, staging y cuarentena dentro del proyecto.
Sólo elimina la cuarentena después de verificar archivos, catálogo y recibos; una interrupción
entre el movimiento físico y la transacción se reanuda con el mismo plan. `catalog.py` confirma
rutas, altas recuperadas, contadores y marca de auditoría en una sola transacción.

La UI ofrece la misma operación para el proyecto seleccionado y para todos los visibles. El batch
de `modules/cherry/logic/archive_batch.py` no contiene una segunda implementación: itera perfiles,
agrega resultados y aísla sus errores. `scripts/cherry_archive_batch.py` expone vista previa,
selección por id, auditoría forzada, aplicación explícita y reporte JSON para controlar la primera
limpieza real sin mantener lógica desechable.

## Recibos de URLs duplicadas e historial de descarga (2026-09-30)

Una URL nueva cuyo contenido ya estaba catalogado podía descargarse en cada sesión si
carecía de identidad lógica suficiente: se descartaban sus bytes sin recordar esa URL.
`url_receipts.py` conserva el vínculo URL/hash dentro de cada `catalog.db`; la API canónica
confirma el recibo antes de descartar un duplicado o retirar su pendiente. El scan incorpora
URLs nuevas por lotes, con rollback de recibos/alias/cola juntos. Previews y hashes no probados
no crean recibos de originales; conflictos no sobrescriben la identidad previa.

La fusión preserva todos los recibos de contenido final y sus alias, incluidos archivos sin
presencia física, después de la migración física y antes de que el wrapper retire el perfil
origen. Importa historia en bloques y una transacción destino; filas ausentes usan el marcador
existente `_purged_<hash>`, sin crear archivos ni avanzar contadores. Fallos conservan el origen
y revierten la importación de historia; movimientos previos siguen el protocolo existente.

Los miembros extraídos conservan la URL del comprimido como procedencia; no reemplazan
su recibo por el hash de una imagen interna. Un miembro sí puede probar una descarga
individual de esos mismos bytes en otra URL.

Descarga y fusión omiten rutas ocupadas cuando el contador está rezagado, con un límite de intentos;
no es una reserva atómica frente a programas externos. Validación en Windows con catálogos
temporales, sesiones del servicio con transporte simulado y regresiones de Cherry. La curación
central y la elección global de la copia más documentada pertenecen a Panopticon; este bloque
no borra copias ni consulta esa DB. La estructura de catálogos separados y portabilidad se conserva.

### Host-owned identities during profile merging (2026-10-02)

`migrate_unique_files` accepts an optional awaited file mover. Panopticon uses
its shared verified transfer for already indexed copies, preserving Native ID,
curation and history before Cherry acknowledges the destination. The engine still
owns catalog/history writes. Completed physical paths are reported even if the
later catalog acknowledgement fails, so consumers can review partial effects.
The original mover remains available for engine-only clients and unindexed files.
