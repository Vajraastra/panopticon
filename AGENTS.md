# panopticon — instrucciones del proyecto

Lo que sorprendería a alguien que llega al repo. Las reglas globales (comunicación, entorno,
documentación, manejo de errores) están en el `AGENTS.md` global y no se repiten aquí.

**Qué es:** hub modular de creación de datasets de imágenes, GPU-first. PySide6 + SQLite.
El estado de la biblioteca, sus identidades y curación viven en `panopticon.db`
(datos locales, no versionados).

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

`README.md`, `AGENTS.md`, `DECISIONES.md` (decisiones de David con su detalle) y
`third_party/cherry_dl/VENDORED.md` están trackeados, además de las licencias. La documentación de trabajo (`TASKS`, `BITACORA`, `HANDOFF`, `*_DESIGN.md`)
está **gitignored**: git no la respalda, así que **hay que copiarla antes de reescribirla**:
`pwsh scripts/backup_docs.ps1` deja una única copia rotativa en `.work/session-notes-backup/`
(no la sobrescribe si el original quedó a menos de la mitad de tamaño; `-Force` lo anula).
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
vista **de forma perezosa dentro de `get_view()`** — nunca en `__init__`. Por defecto la
vista usa `StandardToolLayout`; la forma sigue a la función, así que tablas y exploradores
(Cherry, Gallery) pueden tener otra, siempre que respeten la regla 9. `StandardToolLayout` da
sidebar de 320 px a la izquierda, canvas al centro y barra de acciones abajo, con
`tool_header=(icono, nombre, descripción)` para el encabezado. `load_image_set(paths)` es la interfaz
estándar para recibir imágenes de otro módulo: implementarla basta para aparecer en el menú
«Enviar a…» de Gallery y Cherry. `run_headless(params, input_data)` permite usar la lógica sin
GUI. El `mod_loader` descubre la carpeta sola; lo único que se registra a mano es la etapa del
dashboard (`MainWindow.DASHBOARD_STAGES` en `main.py`: Obtener, Biblioteca, Curar, Taller,
Dataset). Sin ella la tarjeta cae en «Otros».

### Reglas que no se negocian

1. **Los módulos nunca se importan entre sí.** Se comunican por `EventBus` o por
   `load_image_set`. Si un widget se necesita en dos módulos, se mueve a `core/components/` — no
   se importa del vecino. Format Converter y Character Separator están retirados:
   `core/mod_loader.py` rechaza su descubrimiento y carga. Se conservan el código
   legacy y las salidas de Format Converter; el Separador no se debe reconstruir.
   Character Recognizer usa ArcFace + YuNet. El servicio central de datos vive en `core/`: los módulos
   usan su fachada o `context["data_service"]`; nunca reciben conexiones SQLite.
2. **Nunca modificar el archivo original.** La salida va a `CachePaths.get_tool_cache("mi_tool")`
   o a una carpeta nueva. Al terminar un proceso, abrir la carpeta con `CachePaths.open_folder()`.
   Excepciones deliberadas, ninguna imitable sin pensarlo: **Metadata Hub**, la fusión de
   proyectos de Cherry-DL, **Duplicate Finder**, **Pickler** y el mantenimiento manual de Cherry.
   Todas exigen vista previa actual, confirmación manual, diario/recibos y conservar IDs,
   curación e historial; ninguna es automática ni se decide por posición o pHash; deshacer
   jamás sobrescribe una ruta ocupada ni elimina una copia modificada; el borrado definitivo es
   siempre manual y sólo del archivo verificado. Detalle y fechas: `DECISIONES.md`.
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

   **Un `setStyleSheet` sin selector en un contenedor se hereda a todos sus hijos** y le gana
   al QSS global: así quedaron los botones primarios con aspecto de deshabilitados. Acotarlo
   con `objectName` (`QFrame#bottom_bar {...}`, `#x .QWidget` para los hijos genéricos).

   El `accent_color` que declara el módulo es su **tono** de identidad; el `mod_loader` le pone
   el croma y la claridad del tema (`themed_accent`), así que basta con elegir un tono que no
   choque con los de los demás módulos. Las paletas se generan y se auditan con
   `scripts/generate_palettes.py` — no se retocan a ojo.
7. **Metadata:** las herramientas que transforman un archivo copian la metadata del original con
   `MetadataStamper.transfer()`; Watermarker es la excepción deliberada (limpia metadata para
   distribución pública). En lotes grandes, verificar con `BatchVerifier`. Ante un fallo de
   verificación, jamás borrar el original.
8. **Escrituras en disco atómicas:** temp → `os.replace`, nunca `save()` sobre el destino final.
9. **Convención de interfaz** (2026-11-26; detalle y razones en `UI_DESIGN.md`). Toda
   herramienta se ve y se usa igual; antes de dibujar algo a mano, buscar la pieza en
   `core/components/`:
   - **Barra lateral en orden de flujo:** origen (abrir carpeta/imágenes) → opciones →
     avanzado plegado. Títulos de sección con `section()`, notas con `hint()`.
   - **Barra inferior:** navegación a la izquierda; la acción principal **a la derecha, una
     sola por pantalla**, marcada con `primary()`; «Cancelar» a su izquierda; lo destructivo
     con `danger()`. Los roles (`core/components/ui_roles.py`) se pintan por el QSS: nada de
     colores sólidos ad hoc, MAYÚSCULAS ni emoji en botones (el emoji va en el encabezado).
   - **Modos excluyentes:** botones `setCheckable(True)` en grupo, no combos escondidos.
   - **Recorrer imágenes:** `ImageNavigator` («◀ Anterior · N / total · Siguiente ▶», atajos
     ←/→ que no les roban las flechas a los campos de texto). **Ficha de la imagen:**
     `ImageDetails` (nombre, carpeta, resolución, tamaño; `add_row` para filas propias).
     **Tipo de archivo:** `FileBadge` («🖼 PNG», «📦 ZIP»; color por categoría desde el tema) y
     `core/file_kinds.py` (icono/etiqueta/categoría por extensión). **Comprimidos:**
     `core/archive_preview.py` da la primera imagen de un ZIP/RAR en memoria.
   - **Enviar a otra herramienta:** `populate_send_menu` (`core/components/send_to.py`),
     nunca botones fijos por destino.
   - **Recuperación:** el módulo no dibuja su propia pantalla de recuperación; declara
     `recovery_entries()` → `[(clave, título, descripción, abrir)]` y aparece en el Centro de
     recuperación del dashboard.
   - **Taller:** Cropper, Optimizer y Watermarker viven como pestañas de
     `core/workshop_window.py` (lista `WORKSHOP_TOOLS`). Un módulo que entre ahí emite
     `image_set_loaded(list)` al aceptar imágenes y `current_image_changed(str)` al cambiar la
     actual; si solo trabaja con una imagen, declara `accepts_multiple_images = False`.
   - **Nombres:** «etiquetas de biblioteca» son los tags de la BD (Gallery, Librarian,
     Recognizer); «captions» son los `.txt` del Dataset Tagger. No mezclarlos en textos.

### Antes de dar por bueno un módulo

Smoke test (import + `on_load` + `get_view()`) antes y después del cambio, un grupo lógico por
commit, y `git status` limpio de `__pycache__`.

Los selftests se corren con `scripts/run_selftests.ps1` (`-Filter pickler`, `-List`,
`-IncludeToy`): fija `TEMP` largo y el venv, un log por prueba en `logs/selftests/`, exit 1
si algo falla.
Lanzarlo con `pwsh` (PowerShell 7); el `powershell.exe` 5.1 trae la ejecución de scripts
deshabilitada y no lo arranca.

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

Panopticon escribe en cherry-dl **solo por la API de alto nivel del motor, sin SQL propio**,
en estas puertas deliberadas (cada una con su fecha y porqué en `DECISIONES.md`):
`modules/cherry/logic/profile_writes.py` (alta de proyectos y fuentes), `merge.py` (fusión),
`pending_purge.py` (colas de un host muerto), `native_maintenance.py` y `counter_highwater.py`;
`repoint.py` delega en `profile_writes` y no cuenta como puerta propia.

### Mantenimiento con una biblioteca Native indexada

Reglas duras (el contrato completo está en `DECISIONES.md`): el mantenimiento delega en
`process_native_project` (guard compartido, diario Native por fase, recibos por movimiento); nunca
se sustituye por reingesta. Conservar IDs, curación e historial; ante contenido distinto, mantener
la versión anterior offline y admitir una identidad independiente. Las operaciones interrumpidas
requieren revisión física/Native actual y confirmación antes de reanudarlas, sin reproducir planes
históricos. El mantenimiento conserva paquetes y no retira duplicados automáticamente; una purga de
comprimidos exige el manifiesto completo ligado al SHA del paquete y sus miembros.

La fusión está oficializada; el trabajo de producto pendiente está en `TASKS.md` y las
decisiones cerradas en `CHERRY_FUSION_DESIGN.md`.
