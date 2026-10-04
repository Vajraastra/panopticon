# panopticon — instrucciones del proyecto

Lo que sorprendería a alguien que llega al repo. Las reglas globales (comunicación, entorno,
documentación, manejo de errores) están en el `AGENTS.md` global y no se repiten aquí.

**Qué es:** hub modular de creación de datasets de imágenes, GPU-first. PySide6 + SQLite.
El estado de la biblioteca vive en `panopticon.db` (~294K archivos indexados).

## Copia de trabajo e historial

Decisión de David (2026-10-03): mantener **una sola copia de trabajo**, este
repositorio, y usar **Git** para el historial y la recuperación del código. Esta
regla sustituye la indicación anterior de clonar antes de editar. No crear clones,
ZIP ni bundles como respaldos rutinarios de versiones ya integradas. Retirar los
residuos que la versión actual no usa; no confundirlos con biblioteca, curación,
sesiones ni recuperación de operaciones pendientes. Las notas ignoradas por Git
se mantienen actuales; cuando necesiten respaldo previo, usar una única copia
rotativa, no una cadena de respaldos históricos.

## Arrancar y detener

- **`run.bat`** es el launcher (Windows nativo, sin Git Bash). Crea/repara el venv con `uv`
  (Python 3.13 gestionado), instala `requirements.txt` y lanza `main.py`. `run.sh` es
  best-effort para Linux y no se testea.
- Para ejecutar cualquier cosa a mano, usar **`venv/Scripts/python.exe`**, nunca un Python del
  sistema. Smoke de un módulo:
  `QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -c "import modules.X.module"`.
- La app es de escritorio: no deja procesos de fondo, se cierra cerrando la ventana.

## Trampas del entorno (ya costaron una sesión cada una)

Todas están explicadas en `BITACORA.md`; el resumen operativo:

- **Nunca `uv pip sync`** en este proyecto — rompe el venv. Usar `uv pip install -r`, o recrear.
- **Fin de línea mixto**: hay `.py` en CRLF y otros en LF. Verificar antes de commitear que la
  edición no volteó el archivo entero (el `grep -c` de retornos de carro da falsos positivos en
  este Git Bash; usar `tr -dc '\r' | wc -c`).
- `run.bat` debe quedar en **CRLF**; `run.sh` en **LF**.
- Si un `.pyd` falla con "una directiva de Control de aplicaciones bloqueó este archivo", es
  Smart App Control (desactivado por David en 2026-07-02, no debería reaparecer).

## Estructura

```
core/            base_module · mod_loader · event_bus · paths · theme(_manager) ·
                 locale_manager · logging_config · catalog_reader · database_manager ·
                 library_service · library_repository · library_connections
  components/    widgets compartidos: standard_layout · thumbnail · tag_widgets
  metadata/      extractor · stamper · verifier · batch_verifier · bundle
  ai/            helpers de descarga de modelos
modules/         un módulo por carpeta (12 activos); se descubren solos
third_party/     cherry_dl vendorizado, motor canónico (ver VENDORED.md)
scripts/         utilidades autónomas de una sola vez (p.ej. merge_patreon.py)
locales/         en.json · es.json
```

## Qué se versiona y qué no

`README.md`, `AGENTS.md` y `third_party/cherry_dl/VENDORED.md` están trackeados, además de
las licencias. La documentación de trabajo (`TASKS`, `BITACORA`, `HANDOFF`, `*_DESIGN.md`)
está **gitignored**: git no la respalda, así que **hay que copiarla antes de reescribirla**.
Tampoco se versionan `venv/`,
`models/`, `data/`, `cache/`, `logs/`, `config.json` ni las `*.db`.

## El patrón fijo para añadir un módulo

### Estructura y contrato

```
modules/mi_herramienta/
  module.py        # clase que hereda de BaseModule — punto de entrada
  logic/           # Python puro, sin UI
  ui/              # widgets PySide6 propios (opcional)
  presets/         # datos de fábrica (opcional)
```

`module.py` define `_name`, `_description`, `_icon` (emoji) y `accent_color`, y construye la
vista **de forma perezosa dentro de `get_view()`** — nunca en `__init__`. La vista usa
`StandardToolLayout` (sidebar de 320 px a la izquierda, canvas al centro, barra de acciones
abajo). `load_image_set(paths)` es la interfaz estándar para recibir imágenes de otro módulo;
`run_headless(params, input_data)` permite usar la lógica sin GUI. El `mod_loader` descubre la
carpeta sola: no hay que registrar nada en el core.

### Reglas que no se negocian

1. **Los módulos nunca se importan entre sí.** Se comunican por `EventBus` o por
   `load_image_set`. Si un widget se necesita en dos módulos, se mueve a `core/components/` — no
   se importa del vecino. Format Converter está retirado: `core/mod_loader.py` lo excluye de descubrimiento/carga;
   su código legacy y salidas se conservan. El servicio central de datos vive en `core/`: los módulos
   usan su fachada o `context["data_service"]`; nunca reciben conexiones SQLite.
2. **Nunca modificar el archivo original.** La salida va a `CachePaths.get_tool_cache("mi_tool")`
   o a una carpeta nueva. Al terminar un proceso, abrir la carpeta con `CachePaths.open_folder()`.
   Tres excepciones, deliberadas y ninguna imitable sin pensarlo: **Metadata Hub**, que para
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
   Metadata Hub conserva el respaldo previo hasta una purga definitiva manual por
   operación (David, 2026-10-01): vista previa actual, archivo conservado verificado,
   historial/recibo y confirmación explícita. Nunca purga automática; preservar
   identidad/curación/historial/recibos/diario productor y evidencia de la purga.
3. **Lo pesado va en un `QThread`.** Inferencia, hashing, recorrido de disco: nunca en el hilo
   de la GUI. La señal de fin se llama `finished_signal`, **no** `finished` (sombrearía la señal
   nativa de `QThread`). Poner siempre un guard `isRunning()` contra doble inicio.
4. **Ningún texto de UI hardcodeado.** `self.tr("clave", "default")` + la clave en `locales/en.json`
   y `locales/es.json`, con formato `tool.nombre_modulo.id_elemento`.
5. **Todo control accionable lleva tooltip.** Botones, casillas, combos, campos y sliders,
   con `setToolTip(self.tr("<prefijo>.<id>_tip", "…"))` — la misma regla 4 se aplica al texto
   de ayuda. Un tooltip que repite la etiqueta es ruido: el que sirve dice lo NO obvio (qué
   pasa después, dónde va la salida, qué lo distingue del botón de al lado, si es destructivo
   o si tarda). Comprobar con `scripts/tooltip_audit.py`, que mide sobre los widgets reales.
6. **Ningún color hardcodeado.** Vía `theme_manager.get_color()`. Hay 10 temas y los claros
   delatan cualquier `color: white` suelto. El vocabulario, con su significado —usar el que
   toca, no el que se parece—:
   - Superficies, de hundida a elevada: `bg_input` · `bg_sidebar` · `bg_main` · `bg_panel`.
     Lo grande va sobre `bg_main`; `bg_panel` es para lo pequeño y elevado (tarjetas, popovers).
     Una tabla a pantalla completa sobre `bg_panel` deja el área dominante en el nivel más claro.
   - Bordes: `border` separa superficies; **`border_strong` dibuja lo accionable** (casillas,
     campos, botones). Un control con `border` se pierde contra el fondo.
   - Texto: `text_primary` · `text_secondary` · `text_dim`.
   - Acentos: `accent_main` (identidad y títulos) · `accent_hover` · `accent_info` ·
     `accent_success` · `accent_warning` · `accent_danger`.

   **Un control sin regla en el QSS global no está tematizado**, por mucho que lo esté el resto
   de la ventana: Qt lo pinta con su paleta nativa. Si se estrena un tipo de widget, hay que
   darle su regla en `ThemeManager.get_stylesheet()`.

   El `accent_color` que declara el módulo es su **tono** de identidad; el `mod_loader` le pone
   el croma y la claridad del tema (`themed_accent`), así que basta con elegir un tono que no
   choque con los de los demás módulos. Las paletas se generan y se auditan con
   `scripts/generate_palettes.py` — no se retocan a ojo.
7. **Metadata:** las herramientas que transforman un archivo copian la metadata del original con
   `MetadataStamper.transfer()`; Watermarker es la excepción deliberada (limpia metadata para
   distribución pública). En lotes grandes, verificar con `BatchVerifier`. Ante un fallo de
   verificación, jamás borrar el original.
8. **Escrituras en disco atómicas:** temp → `os.replace`, nunca `save()` sobre el destino final.

### Antes de dar por bueno un módulo

Smoke test (import + `on_load` + `get_view()`) antes y después del cambio, un grupo lógico por
commit, y `git status` limpio de `__pycache__`.

## Cherry-dl

`third_party/cherry_dl/` contiene el motor canónico de Cherry dentro de Panopticon.
Su origen histórico es `E:\githubs\cherry-dl` (commit `befa56d`), pero esa carpeta ya no
existe en la ubicación indicada; no se ha verificado una ubicación archivada del standalone.
No usar esa ruta como respaldo ni reconstruir un segundo repositorio por inferencia.
**Desde el 2026-09-05 los arreglos del motor se hacen aquí** (decisión de David);
la integración quedó oficializada el 2026-09-16, con Panopticon como fuente única.
Cada divergencia se anota en `third_party/cherry_dl/VENDORED.md` con su motivo.
Nada de cambios cosméticos ni refactors de gusto en esa carpeta; se toca lo que está roto
y se explica por qué.
Regla: **Panopticon no escribe los archivos de cherry-dl con SQL propio ni cambia su schema**;
un sidecar nuevo y propio junto a ellos sí es válido.

**Excepción acordada (David, 2026-09-02):** dar de alta proyectos y añadirles fuentes sí escribe
`index.db`, porque es la operación central del programa y sin ella el wrapper obligaba a abrir la
GUI clásica para lo más básico. Vive en `modules/cherry/logic/profile_writes.py` y **solo llama a
la API de alto nivel del motor** (`profiles.create_profile`, `add_url_to_profile`,
`index.set_profile_url_enabled`) — la misma puerta por la que el motor ya escribe su índice en
cada descarga. Lo que sigue prohibido es exactamente lo que la regla protege: SQL nuestro contra
sus tablas y cualquier cambio de schema.

**El segundo y último punto que escribe (2026-09-03):** `modules/cherry/logic/merge.py`, la
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

Esos tres archivos son los únicos de Panopticon que escriben ahí.

La fusión está oficializada; el trabajo de producto pendiente está en `TASKS.md` y las
decisiones cerradas en `CHERRY_FUSION_DESIGN.md`.
