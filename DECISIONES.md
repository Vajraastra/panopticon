# Decisiones de David que fijan contratos

Movido desde `AGENTS.md` el 2026-11-24, **sin cambios de fondo**, para que AGENTS.md quede corto
y legible al arrancar cada sesión. Estas decisiones siguen siendo vinculantes: `AGENTS.md` conserva
la regla dura y enlaza aquí el detalle, las fechas y los porqués.

## Excepciones a «nunca modificar el archivo original»

Excepciones deliberadas, ninguna imitable sin pensarlo: **Metadata Hub**, que para
eso existe, y **la fusión de proyectos de Cherry-DL**, que mueve archivos entre carpetas y
puede borrar los repetidos — por eso pide confirmación enseñando los números antes de tocar
nada, y por eso se prueba con carpetas de juguete (`scripts/cherry_merge_toytest.py`);
**Duplicate Finder**, autorizado por David para retirar copias exactas redundantes:
conserva la curada/documentada, revalida el grupo completo y mueve a recuperación
persistente con diario, sin eliminar recibos Cherry. Su purga posterior requiere
vista previa actual y confirmación manual por copia (David, 2026-09-30): borrado
definitivo sólo del archivo verificado en recuperación, con la copia conservada
protegida; conserva IDs offline/curación/diarios/recibos. Nunca automático, por
posición o pHash.
Pickler también mueve originales por decisión explícita del curador: Windows,
imagen/caption verificados, destino sin sobrescritura, diario y servicio Native.
Mover conserva ID; copiar conserva curación inicial en ID independiente (David,
2026-10-01). Deshacer jamás sobrescribe una ruta ocupada ni elimina una copia
modificada; su ID/curación se conservan offline. Sin SQL propio en Cherry.
El mantenimiento manual de Cherry también conserva ID/curación por movimiento
Native verificado, con reserva y recibos; no elimina paquetes ni duplicados.
Metadata Hub conserva el respaldo previo hasta una purga definitiva manual por
operación (David, 2026-10-01): vista previa actual, archivo conservado verificado,
historial/recibo y confirmación explícita. Nunca purga automática; preservar
identidad/curación/historial/recibos/diario productor y evidencia de la purga.

## Puertas por las que Panopticon escribe en cherry-dl

**Excepción acordada (David, 2026-09-02):** dar de alta proyectos y añadirles fuentes sí escribe
`index.db`, porque es la operación central del programa y sin ella el wrapper obligaba a abrir la
GUI clásica para lo más básico. Vive en `modules/cherry/logic/profile_writes.py` y **solo llama a
la API de alto nivel del motor** (`profiles.create_profile`, `add_url_to_profile`,
`index.set_profile_url_enabled`) — la misma puerta por la que el motor ya escribe su índice en
cada descarga. Lo que sigue prohibido es exactamente lo que la regla protege: SQL nuestro contra
sus tablas y cualquier cambio de schema.

**El segundo punto que escribe (2026-09-03):** `modules/cherry/logic/merge.py`, la
fusión de proyectos repetidos. Escribe más que ningún otro —mueve archivos entre carpetas, borra
los repetidos si se le pide, y deja un perfil menos en `index.db`— pero por la misma puerta:
`catalog.migrate_unique_files`, `index.merge_profiles` y `dedup.handle_orphans`, todas del motor.
Cero SQL propio. Lo que NO copia del motor es su orden: el índice se fusiona después de mover (si
mover falla, fusionar dejaría los archivos sin dueño) y los huérfanos se resuelven después del
índice. El porqué, medido, está en la cabecera de ese archivo y en `BITACORA.md`.

**El tercero (2026-09-05):** `modules/cherry/logic/pending_purge.py`, que saca de
`pending_queue` las filas que apuntan a un host muerto —las 98.295 de Kemono, que no se limpian
solas porque un timeout es diferible y se reintenta para siempre—. Borra fila a fila con
`catalog.remove_pending`, que es API del motor: más lento que un `DELETE ... WHERE` y aceptado a
sabiendas, porque la alternativa es justo lo que la regla prohíbe. Nunca toca un archivo del
disco ni el catálogo de lo ya descargado.

`modules/cherry/logic/repoint.py` (migración Kemono → Pawchive) **no cuenta como cuarto**: no
escribe por su cuenta, delega en `profile_writes.add_url`.

El mantenimiento manual usa `modules/cherry/logic/native_maintenance.py` y la API
del motor para confirmar rutas y miembros después del recibo Native. Antes de Sync,
`counter_highwater.py` sólo eleva contadores mediante `catalog.ensure_counter_at_least`.
Estas puertas también son deliberadas: ningún wrapper escribe SQL Cherry.

## Mantenimiento con una biblioteca Native indexada

El motor mantiene archivos y catálogos Cherry; no reconcilia por sí solo los IDs
centrales. La interfaz individual/batch delega en `process_native_project`: guard
compartido con Sync/fusión, handles fijados, diario Native por fase y recibos por
movimiento antes de confirmar el catálogo. `archives.process_project(native_hooks=...)`
cubre organización, extracción y compactación; conectar sólo los callbacks finales
no cubre las fases anteriores. El CLI batch genérico permite vista previa; aplicar
sin servicio Native sigue bloqueado. Nunca sustituir este contrato por reingesta.
Las operaciones interrumpidas conservan su reserva; requieren revisión física/Native
actual y confirmación antes de reanudarlas, sin reproducir planes históricos.
Sync conserva los originales manuales y no organiza archivos existentes al arrancar;
una descarga coincidente se cataloga como copia nueva. Reindexar después no repara
renombrados ni evita atribuir un contenido nuevo a una identidad anterior.
Conservar IDs, curación e historial; ante contenido distinto, mantener la versión
anterior offline y admitir una identidad independiente.
La ingesta conserva evidencia de contradicciones y confirma las filas sanas del lote.
Una versión original o modificada conocida es compatible; fechas de catálogo y de
creación física no son intercambiables. Un nombre offline sigue reservando su número
antes de nuevas descargas. Una carpeta ausente se reconstruye al iniciar una descarga (no un escaneo) si su carpeta
superior existe, para recuperar un borrado erróneo; el catálogo parte vacío y se vuelve a
descargar. Quien ya no quiere el perfil lo elimina de la lista.

La compactación abarca raíz y carpetas administradas inmediatas. Los nombres del
artista en carpetas curadas/manuales reservan sus números sin entrar en la
deduplicación ni moverse. No aplicar planes históricos ni purgar sus diarios.
El mantenimiento conserva paquetes y no retira duplicados automáticamente. Un
manifiesto Native completo puede acreditar un miembro posteriormente movido o
retirado; no extraerlo de nuevo sobre la decisión del curador.

Una purga manual autorizada de comprimidos ya extraídos exige el manifiesto
completo ligado al SHA del paquete y todos los SHA/tamaños de sus miembros
catalogados. Las retiradas posteriores cuentan como historial y no se restauran
por inferencia (David, 2026-11-19). Registrar miembros nuevos antes de borrar;
conservar catálogo, recibos y evidencia de la eliminación por handle verificado.
El registro del ZIP por sí solo no acredita sus miembros. Los paquetes cifrados
sin manifiesto comprobable se conservan.
