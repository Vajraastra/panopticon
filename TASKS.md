# TASKS — Panopticon

## Estado general
**Re-audit 2026-03 COMPLETO.**
**Quality Scorer refactorizado 2026-04** — Slop Filter + Quality Rank + 3 tipos de contenido.
**Sesión de fixes 2026-06-05 COMPLETA** — code review, bugs críticos resueltos, limpieza.
**EN CURSO (2026-06-13):** Módulo nuevo **Dataset Tagger** — ver plan abajo.
**Pendiente:** calibración de umbrales del Quality Scorer con imágenes reales + sistema de defaults seguros.

---

## 🏷️ MÓDULO NUEVO — Dataset Tagger (plan 2026-06-13)

**Qué es:** toma una carpeta (o set/imágenes individuales de otros módulos) y genera captions automáticos con un VLM (modelo de visión) local u online, para construir datasets de entrenamiento. Salida en sidecars `.txt` estilo kohya. Soporta tags (booru) y lenguaje natural, con plantillas específicas por modelo de generación (SDXL, Pony, Illustrious, Flux, Ideogram, Z-Image). Decisiones completas en memoria: `project_dataset_tagger.md`.

**Reglas que aplican:** salida SIEMPRE en carpeta nueva (nunca junto a los originales, respeta Regla #9 — solo Metadata Hub edita el archivo original); nombre `<carpeta>_<modelo>_tags/` y `<carpeta>_<modelo>_natural/`; acento de color vía `theme.get_color('accent_main')` (patrón de Image Optimizer); sin preprocesamiento de imagen; robustez razonable (datasets ~40 img); NO poblar la DB del Librarian.

### Estructura de archivos propuesta
```
modules/dataset_tagger/
  module.py                      # DatasetTaggerModule(BaseModule), vista lazy, accent desde theme
  logic/
    providers/
      base_provider.py           # iface: caption(img, prompt)->str, list_models(), is_vision(model)
      openai_compat.py           # cliente OpenAI-compatible (LM Studio/Ollama/vLLM)
      discovery.py               # escaneo de puertos locales (1234/11434/8000) + autoconfig
    credentials.py               # almacén Fernet para APIs online (texto plano NUNCA)
    templates.py                 # carga presets, arma meta-prompt + trigger/prefix/suffix, dedup
    caption_worker.py            # QThread: itera, llama provider, escribe sidecars + copia carpeta(s)
    sidecar.py                   # naming de carpeta, política existentes (borrar/saltar/anexar), copia, modo dual
    tag_tools.py                 # buscar/reemplazar/remover tag en todos los .txt; tags baneadas
  ui/
    tagger_view.py               # vista principal (StandardToolLayout) + DropFrame
    review_view.py               # revisión pre (confirmar set) + edición post (.txt + tag tools)
  presets/
    model_templates.json         # resultado de Fase 1 (estructura de caption por modelo)
```

### Fases (un grupo lógico = un commit; smoke antes/después)
- [x] **Fase 0 — Esqueleto + registro** (2026-06-13): `module.py` + `ui/tagger_view.py` (BaseModule, `get_view()` lazy, accent vía `theme.get_color('accent_main')`), locales `tool.dataset_tagger.*` + `tagger.*` en/es. Verificado: loader lo descubre, vista se construye con `StandardToolLayout`, `load_image_set` OK. Sin commitear aún.
- [x] **Fase 1 — INVESTIGACIÓN de estructuras por modelo** (2026-06-13): `presets/model_templates.json` con los 6 modelos. tags booru (SDXL/Pony/Illustrious) vs lenguaje natural (Flux/Ideogram/Z-Image). Cada uno con `format`, `quality_prefix`, `trigger_position`, `separator`, `structure` y `meta_prompt`. Fuentes en `_meta.sources` + BITACORA.
- [x] **Fase 2 — Capa de providers** (2026-06-13): `logic/providers/base_provider.py` (iface + `encode_image` base64 + heurística `looks_like_vision`), `openai_compat.py` (list_models, caption con `image_url` data-uri, test_connection), `discovery.py` (escaneo 1234/11434/8000/8080). Verificado contra LM Studio real corriendo en :1234.
- [ ] **Fase 3 — Worker + salida:** `caption_worker` (QThread, reintentos básicos, progreso, cancelación), `sidecar` (copia a carpeta nueva, naming con modelo, política existentes, modo dual = dos carpetas). Strings pre-traducidos antes del worker.
- [ ] **Fase 4 — UI:** `tagger_view` (selección modelo/formato/dual, trigger/prefix/suffix, prompt custom, política existentes, botón Descubrir) + `review_view` (revisión previa del set + editor de `.txt` post-proceso) + `tag_tools` (buscar/reemplazar/remover, baneadas).
- [ ] **Fase 5 — Credenciales online (Fernet):** `credentials.py` + provider online opcional. Respaldar `requirements.txt` antes de añadir `cryptography` (Regla #4).
- [ ] **Fase 6 — Integración + cierre:** `load_image_set()`, drag&drop `files_dropped`, EventBus si aplica; smoke + test funcional; entrada en BITACORA.

---

## ✅ Completado

### Sesión 2026-06-05 — Fixes de estabilidad + limpieza

- [x] **core/ai/model_downloader.py** — helper central de descarga con temp→move, sin parciales, errores diferenciados (404 / red caída / timeout)
- [x] **_download_buffalo_l()** — implementado (estaba vacío). Character Recognizer ya puede descargarse en instalación nueva
- [x] **CalibrationWorker** — calibración movida a QThread, elimina GUI freeze
- [x] **_eval_hand orientación** — vector muñeca→nudillo para detectar si dedos apuntan arriba o abajo
- [x] **AVIF eliminado de filtros de entrada** — quality_scorer y character_recognizer solo aceptan PNG/JPG/WebP (cv2.imread no soporta AVIF)
- [x] **YuNet INT8** — URL actualizada en slop_filter y recognition_engine (100KB vs 230KB FP32)
- [x] **Docstrings corregidos** — 3d_render, analyze_calibration, classify() warning
- [x] **Limpieza** — dummy_creator eliminado, test obsoleto eliminado, .gitignore actualizado
- [x] **Smoke tests 21/21 + batería lógica 19/19** — todos los módulos verificados

### Quality Scorer — Refactor completo (sesión 2026-04)
- [x] **Fase 1 — Slop Filter**: `SlopFilterWorker` QThread, detección anatómica (YuNet/lbpcascade + YOLOv8-pose + MediaPipe Hands + CLIP aesthetic)
- [x] **Fase 2 — Quality Rank**: `QualityRankWorker` QThread, métricas técnicas
- [x] **3 tipos de contenido**: fotorrealista / 3D render / ilustración con pesos y umbrales distintos
- [x] **Modo calibración individual**: botón 🔬 → scores raw + pesos + umbrales del preset activo
- [x] **UI completa**: 3 columnas paginadas, mover slop, pasar keepers a Fase 2

### Re-Auditoría módulo por módulo (sesión 2026-03)
- [x] Image Optimizer, Duplicate Finder, Watermarker, Smart Cropper, Layer Composer (eliminado)
- [x] Quality Scorer, Gallery, Character Recognizer, Librarian, Metadata Hub
- [x] Sistema de 10 temas, live preview, fix emojis Linux

---

## 🎯 PLAN DE ATAQUE — Fixes de auditoría (orden acordado 2026-06-11)

> Reglas: un módulo = un commit inmediato. Smoke test antes y después de cada uno:
> `python -c "import modules.X.module; print('OK')"`. Consultar bitácora ante errores repetidos.

### Fase 1 — Integridad de datos ✅ COMPLETA (2026-06-12)
1. [x] **Commit 1 `9f80999`: stamper temp→replace** — `_atomic_save()` aplicado a los 9 puntos de escritura in-place. Verificado con stamp/strip reales.
2. [x] **Commit 2 `51b838c`: db_manager LIKE con separador** — `_folder_like()` con `/%` + ESCAPE. Verificado con DB temporal y carpetas hermanas. Bonus: rama `path:` acepta archivo exacto.
3. [x] **Commit 3 `c12969e`: aesthetic MLP repo** — `camenduru/improved-aesthetic-predictor` via model_downloader. DESCUBRIMIENTO: el .pth es el MLP v2 completo (768→1024→…→1), no nn.Linear; nueva `_build_aesthetic_mlp()`. Verificada descarga + carga + forward. Pendiente: corrida end-to-end con CLIP (sesión de calibración).

### Fase 2 — Estabilidad ✅ COMPLETA (2026-06-12)
4. [x] **`3aac3b8` recognition_engine propaga errores de init** — initialize() re-lanza; workers emiten señal `error(str)`; UI la muestra (key `cr.error.engine`).
5. [x] **`44122d0` format_scanner: guard anti doble-inicio** — `isRunning()`.
6. [x] **Señales `finished` → `finished_signal`** — 6 commits (`de4256f`…`b68d967`): image_optimizer, format_converter, duplicate_finder, format_scanner, quality_scorer (×3 workers), character_recognizer (×2). Verificado: cero `finished = Signal` y cero `.finished.connect` restantes.
7. [x] **`c17ad3e` deduplicator: getsize con try/except OSError** — verificado con symlink roto.

### Fase 3 — Rendimiento
8. [x] **`f1a1e2f` Cache de modelos del SlopAnalyzer** — `get_analyzer()` con cache de un slot keyed por `(models_dir, content_type, flags)`; calibración 🔬 ya no recarga CLIP. MediaPipe `close()` al invalidar (resuelve también el item de Fase 4). Fallo de init no envenena el cache.
9. [x] **`f45aa59` Vectorizar `calculate_compression_artifacts`** — `np.ix_` con equivalencia numérica exacta + muestreo estriado para `np.unique`. 50x más rápido en 2048×2048.
10. [x] **`49efc15` WAL + busy_timeout en init_db** — verificado lector+escritor concurrentes. **Parte diferida:** inyectar instancia compartida via context — la auditoría vio 3 sitios pero hay 10 (`cropper:329`, `viewer_window:143`, `recognition_view` ×5…); con WAL las conexiones múltiples ya son seguras, el refactor de inyección es estructural y va mejor como sesión propia.
11. [x] **`fd3e17a` Deduplicator: hashing paralelo** (MD5 + pHash via ThreadPoolExecutor) + agrupamiento visual hamming vectorizado con numpy (equivalencia verificada).

### Fase 4 — Mejoras (sin orden estricto)
12. Independencia del CWD (LocaleManager, `panopticon.db` → `ProjectPaths.root()`).
13. Widgets compartidos a `core/components/` (`ClickableThumbnail`, `FlowLayout`, `TagChip`).
14. `print()` → `logging` + FileHandler a `logs/`.
15. Menores: ~~bare except viewer_window~~ `62ba433`, ~~`tr()` con `is not None`~~ `96557f1`, ~~`mktemp`→`mkstemp`~~ `2f70a7d`. Pendientes: `count_signal` muerto, `color: white` del dashboard, EventBus `unsubscribe`, JPEG stamp sin re-encode (decidir piexif).

---

## 🔲 Auditoría 2026-06-11 — Hallazgos (pendientes de fix)

### Bugs — prioridad alta
- [ ] **stamper.py escribe in-place sin temp→replace** — `Image.open(path)` → `img.save(path)` directo sobre el original en `stamp_file`, `MetadataStamper.stamp`, `strip_metadata`. Fallo a media escritura = original corrupto. Usar patrón temp→`os.replace` (ya existe en model_downloader). Además JPEG/WebP/AVIF re-encodean píxeles (quality=95) en cada stamp → pérdida generacional; para JPEG usar inserción de segmento EXIF sin re-encode (piexif).
- [ ] **db_manager: `LIKE folder + '%'` sin separador** — `/foo/bar` matchea `/foo/barbecue/...`. En deep_clean elimina registros de carpetas hermanas de la DB. Afecta `get_known_files_in_folder`, `remove_watched_folder`, `get_folder_count`, `get_files_recursive`, `search_files_paginated`. Fix: `norm + '/%'` + cláusula `ESCAPE` para `%`/`_` en nombres.
- [ ] **recognition_engine traga errores de init/descarga** — si falla la descarga del modelo, `initialized=False` y `analyze_image` devuelve None silencioso; el usuario ve "sin rostros" en todo. Propagar error a la UI (señal error del worker).
- [ ] **Aesthetic MLP: repo HF inexistente** — `_download_aesthetic_mlp` usa `repo_id="shunk031/aesthetics-predictor"` que **no existe** (verificado 2026-06-11, API devuelve 401). En instalación nueva el scorer aesthetic falla silenciosamente (`use_aesthetic=False`) — y en "illustration" pesa 0.40, el dominante. Reemplazo drop-in verificado: `repo_id="camenduru/improved-aesthetic-predictor"`, contiene exactamente `sac+logos+ava1-l14-linearMSE.pth` (nn.Linear 768→1). El repo `shunk031/aesthetics-predictor-v2-sac-logos-ava1-l14-linearMSE` NO sirve (es wrapper transformers con CLIP completo). URLs verificadas OK: YuNet INT8, lbpcascade, buffalo_l.zip.

### Bugs — prioridad media
- [ ] **format_scanner sin guard anti doble-inicio** — `start_scan` reasigna `self.worker` con el hilo anterior vivo (riesgo "QThread destroyed while running"). Añadir `isRunning()` guard o deshabilitar botón (patrón librarian:706).
- [ ] **8 workers sombrean `QThread.finished`** — `finished = Signal(...)` oculta la señal nativa (image_optimizer, quality_scorer×3, format_scanner, format_converter, duplicate_finder, character_recognizer×2). Los de `Signal()` sin args (RecognitionWorker/AutoScanWorker) son los más riesgosos (misma firma → posible doble disparo). Renombrar como `finished_signal` (patrón correcto ya usado en indexer.py).
- [ ] **deduplicator: `os.path.getsize` sin try** (línea 49) — crash del worker si un archivo desaparece o es symlink roto a media corrida.

### Bugs — prioridad baja
- [ ] `gallery/ui/viewer_window.py:442` — bare `except:` → `except OSError`.
- [ ] `locale_manager.tr()` — `if val:` descarta traducción vacía legítima → `if val is not None`.
- [ ] `model_downloader` — `tempfile.mktemp` (deprecado, race) → `tempfile.mkstemp`.
- [ ] `indexer.py` — `count_signal` definido pero nunca emitido; `st_ctime` no es fecha de creación en Linux.

### Optimizaciones
- [ ] **SlopAnalyzer recarga CLIP ViT-L-14 (~1.7 GB) en cada corrida** — incluso la calibración 🔬 de UNA imagen. Cachear modelos a nivel módulo (key: content_type+flags) o mantener analyzer vivo entre corridas. La mayor ganancia de rendimiento del proyecto.
- [ ] **`calculate_compression_artifacts`: doble loop Python por bloques 8×8** — vectorizar con numpy slicing (`gray[b:h-b:b, b:w-b:b]`); también `np.unique` sobre todos los píxeles → muestrear. Cuello de botella de Fase 2.
- [ ] **DB: habilitar `PRAGMA journal_mode=WAL` + `busy_timeout`** — hay 2 conexiones (DatabaseManager del Librarian + QueryEngine de Gallery) al mismo panopticon.db; riesgo de "database is locked". `search_by_terms` con `LIKE %..%` en 6 columnas no escala a 250K filas → considerar FTS5. `get_folders_paginated` hace N+1 queries.
- [ ] **Deduplicator visual O(n²)** + hashing secuencial (`ThreadPoolExecutor` importado, nunca usado) — paralelizar hashing; BK-tree o bucketing por prefijo para agrupar.
- [ ] **Startup: `add_module_card()` llama `get_view()` de todos los módulos al arrancar** — el lazy-init no es lazy. Construir vista en el primer `switch_to_module`.

### Mejoras
- [ ] Unificar `print()` → `logging` en core (paths, event_bus, mod_loader, metadata/*) y módulos (gallery loader, quality_scorer.py:205, deduplicator); configurar FileHandler hacia `logs/`.
- [ ] **Eliminar dependencia del CWD** — LocaleManager (`locales/`, `config.json`) y DatabaseManager (`panopticon.db`) usan rutas relativas; usar `ProjectPaths.root()`. Quitaría el requisito "ejecutar desde la raíz".
- [ ] Imports cruzados — análisis 2026-06-11: la cadena de producción real usa señales (`request_open_*` → wiring en main.py → `load_image_set`), NO imports. Clasificación:
  - **Legítimos (distribución de datos del hub Librarian):** `gallery/logic/query_engine.py` y `image_optimizer/module.py:389` importan `DatabaseManager` para leer la DB central (tags/ratings). Mantener el acceso, pero inyectar la instancia compartida via `context` en vez de crear conexiones nuevas (hoy hay 3 conexiones a panopticon.db) + habilitar WAL.
  - **No legítimos (reúso de widgets, nada que ver con la cadena):** `gallery/ui/components.py` importa `ClickableThumbnail` desde `librarian/module.py` completo; `gallery/ui/sidebar.py` importa `FlowLayout`/`TagChip` de `librarian/logic/tagging_ui`. Mover a `core/components/`.
- [ ] MediaPipe Hands sin `close()` al terminar corrida del SlopFilter.
- [ ] `_download_aesthetic_mlp` no usa model_downloader y deja el archivo descargado duplicado en disco.
- [ ] main.py dashboard: `color: white` hardcodeado en títulos/tarjetas — ilegible en temas light/sepia.
- [ ] EventBus sin `unsubscribe()`.
- [ ] mod_loader: instancia la primera subclase de BaseModule que devuelve `inspect.getmembers` (orden alfabético) — frágil si un module.py importa otra clase BaseModule.

---

## 🔲 Próxima sesión — Calibración del Quality Scorer

> **Prerequisito:** El usuario trae 30-40 imágenes etiquetadas manualmente
> (keeper / review / slop) de sus colecciones reales.

### 1. Sistema de defaults seguros (~30 min) — implementar primero
- Separar `FACTORY_DEFAULTS` de `CONTENT_TYPES` en `slop_filter.py`
- Función `_load_content_types()` — lee `data/calibration.json` si existe, sino usa factory defaults
- Botón "Restaurar defaults de fábrica" en UI → borra `calibration.json`
- Arquitectura: `FACTORY_DEFAULTS` (hardcoded) → `calibration.json` (usuario) → UI en vivo

### 2. Calibrador desde keepers (~1h)
- Botón "📚 Calibrar desde mis keepers…" en sidebar Fase 1
- `UserCalibrator` en `logic/user_calibration.py`:
  - Corre los 4 scorers sobre cada imagen keeper del usuario
  - Calcula distribución por scorer (media, std, percentil 10)
  - Peso sugerido ∝ 1/CV (scorer más consistente = más peso)
  - Umbral keeper = p10(combined) - margen de seguridad
- Diálogo con tabla de stats + botones [Aplicar] [Cancelar]
- Guardar en `data/calibration.json`

### 3. Sesión de calibración con imágenes reales (~1-2h)
- **Ilustración/anime**: 5-8 keeper + 3-5 review + 5-8 slop
- **3D Render**: 5-8 keeper + 3-5 review + 5-8 slop
- Correr modo 🔬 sobre cada una, registrar scores raw
- Ajustar `FACTORY_DEFAULTS` con los datos reales
- Las imágenes keeper también sirven para demostrar el calibrador automático

---

## 🔲 Pendiente — Decisión de diseño diferida

### "Bonus fantasma" en scorers que fallan al cargar
**Contexto:** Si un modelo no carga (`use_face = False`), el combined recibe `0.5 × peso_face` fijo.
Los pesos no se redistribuyen a los scorers activos.
**Estado actual:** Mantener comportamiento neutro (Opción A). Los umbrales de CONTENT_TYPES fueron calibrados asumiendo 4 scorers activos.
**Reevaluar:** Después de la sesión de calibración real, si se confirma que los modelos fallan con frecuencia en producción → implementar redistribución de pesos y recalibrar umbrales.

---

## 🔲 Futuro (sin fecha)

- [ ] **GitHub Actions / CI** — smoke tests automáticos en push (21 módulos)
- [ ] **Face Embedding Exporter** — exportar perfiles ArcFace de `character_profiles.db` como `.npy` para IP-Adapter FaceID / InstantID (ComfyUI/A1111). Ver nota en bitácora 2026-03-14.
- [ ] **Quality Scorer Fase 3** — clasificación de encuadre (full body / half body / closeup) y orientación (frontal / 3/4 / perfil) usando datos de YOLOv8-pose ya calculados en Fase 1.
- [ ] **Librarian mejoras cherry-dl** — mostrar `url_source` y artista en sidebar; deduplicación por `cherry_hash`.

---

## Integración cherry-dl (implementada 2026-04)

- `core/catalog_reader.py` — read-only: `is_cherry_catalog()`, `get_image_files()`, `get_artist_info()`
- `modules/librarian/logic/indexer.py` — detecta `catalog.db` → modo cherry-dl
- Panopticon **nunca escribe** en archivos de cherry-dl
- `cherry-dl/PANOPTICON_INTEGRATION.md` — nota para agentes de cherry-dl
