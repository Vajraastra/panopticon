# TASKS — Panopticon

## Estado general
**Re-audit 2026-03 COMPLETO.** Soporte AVIF + integración cherry-dl implementados (2026-04).
**Quality Scorer refactorizado (2026-04)** — Fase 1 Slop Filter + Fase 2 Quality Rank.
**Próxima sesión:** calibración de umbrales con imágenes reales + sistema de defaults seguros.

---

## ✅ Completado

### Quality Scorer — Refactor completo (sesión 2026-04)
- [x] **Fase 1 — Slop Filter**: `SlopFilterWorker` QThread, detección anatómica (YuNet/lbpcascade + YOLOv8-pose + MediaPipe Hands + CLIP aesthetic)
- [x] **Fase 2 — Quality Rank**: `QualityRankWorker` QThread, métricas técnicas (nitidez, artefactos, resolución, color, composición)
- [x] **3 tipos de contenido**: fotorrealista (YuNet), 3D render (lbpcascade), ilustración/anime (lbpcascade + CLIP dominante)
- [x] **Modo calibración individual**: botón 🔬 → scores raw por modelo + umbrales actuales
- [x] **UI completa**: 3 columnas paginadas (keeper/review/slop), mover SLOP, pasar keepers a Fase 2, carpeta F2 configurable
- [x] **Deps nuevas**: `mediapipe>=0.10.0`, `open-clip-torch>=2.24.0`
- [x] **lbpcascade_animeface**: confirmado por usuario para renders 3D y ilustraciones

### Re-Auditoría módulo por módulo (sesión 2026-03)
- [x] **Image Optimizer** — botones consolidados al sidebar, `DropFrame` con drag & drop (imágenes + carpetas recursivo), eliminado bottom bar
- [x] **Duplicate Finder** — full rewrite: locale `dup.*` completo, `theme_manager.get_color()`, `QMessageBox.critical→warning`, `logging.warning`, `DuplicateItem(bg_color, border_color)`
- [x] **Watermarker** — imports top-level, locale `wm.*`, `theme_manager.get_color()`, todos los diálogos con `self.view`, `critical→warning`
- [x] **Smart Cropper** — lazy guard, locale `crop.*`, proporciones agrupadas (Dataset/Monitors/Phones) con separadores `QStandardItem(disabled)`, comparación por índice AR_FREE/AR_CUSTOM (locale-safe), auto-append extensión en save_crop con regex
- [x] **Layer Composer** — ELIMINADO completamente (no había referencias externas)
- [x] **Quality Scorer** — `os.startfile()→CachePaths.open_folder()`, `critical→warning`, locale `qs.*`, `theme_manager.get_color()`, `run_headless()` añadido
- [x] **Gallery** — imports top-level, 4 strings → `self.tr()`, `run_headless()` añadido
- [x] **Character Recognizer** — audit completo + nuevas features: modo Illustration/Real Person (ArcFace+YuNet+landmark alignment), locale `cr.*` (~40 keys), logging, `run_headless()`, botones Skip/Return, borrado de tags, Auto Mode con `AutoScanWorker` (sugerencia dominante al 60%)
- [x] **Librarian** — `theme_manager.get_color()` en sidebar/content/bottom/thumbnails/scan buttons, bare excepts corregidos, bug CSS resuelto; **bugs post-audit**: columnas dinámicas (viewport-aware) + carga lazy de thumbnails por batches (no bloquea UI)
- [x] **Metadata Hub** — `theme_manager.get_color()` en sidebar/content/set_mode/_update_mode_styles, `ResponsiveImageLabel` recibe colores del tema, eliminado `from core.theme import Theme`, `LocaleManager()` directo eliminado, 2× `QMessageBox.critical→warning`
- [x] **Sistema de temas** — 10 paletas (cyberpunk/midnight/forest/slate/light/sepia/cosmic/grape/ocean/aurora), ThemeManager expandido, settings page rediseñada con grid de tarjetas, live preview via `_rebuild_settings_page()`, emoji icons en tarjetas, fix de rendering de emojis en Linux (`QFont.setFamilies`)

---

## 🔲 Pendiente

### Próxima sesión — Calibración del Quality Scorer

#### 1. Sistema de defaults seguros (implementar primero, ~30 min)
- Separar `FACTORY_DEFAULTS` de `CONTENT_TYPES` en `slop_filter.py`
- Función `_load_content_types()` — lee `data/calibration.json` si existe, sino usa factory defaults
- Botón "Restaurar defaults de fábrica" en UI → borra `calibration.json`
- Arquitectura: factory defaults (hardcoded) → calibración usuario (JSON) → calibración desde keepers (UI)

#### 2. Calibración desde keepers del usuario (implementar, ~1h)
- Botón "📚 Calibrar desde mis keepers…" en sidebar Fase 1
- `UserCalibrator` en `logic/user_calibration.py`:
  - Corre los 4 scorers sobre cada imagen keeper
  - Calcula distribución por scorer (media, std, percentil 10)
  - Peso sugerido ∝ 1/CV (scorer más consistente = más peso)
  - Umbral keeper = p10(combined) - margen
- Diálogo de resultado con tabla de stats + [Aplicar] [Cancelar]
- Guardar en `data/calibration.json`

#### 3. Sesión de calibración con imágenes reales (~1-2h)
El usuario trae ~30-40 imágenes etiquetadas (keeper/review/slop):
- **Ilustración/anime**: 5-8 keeper + 3-5 review + 5-8 slop
- **3D Render**: 5-8 keeper + 3-5 review + 5-8 slop
- Correr modo 🔬 sobre cada una, anotar scores raw
- Ajustar `FACTORY_DEFAULTS` con esos datos reales
- Las imágenes keeper también sirven para demostrar el calibrador automático

#### Características clave del calibrador desde keepers
- Colecciones con fisonomías exageradas (anime estilizado): el módulo aprende que face=0.35 ES correcto para ese estilo
- Pesos automáticos: si body es muy ruidoso en keepers → peso bajo; si aesthetic es consistente → peso alto
- No requiere que el usuario entienda de pesos ni umbrales

---

### Pendiente anterior — Verificación y cierre

#### 1. Tests de importación (smoke tests)
Ejecutar en orden para detectar errores de import regresivos:
```bash
python -c "import modules.gallery.module; print('gallery OK')"
python -c "import modules.librarian.module; print('librarian OK')"
python -c "import modules.quality_scorer.module; print('quality_scorer OK')"
python -c "import modules.smart_cropper.module; print('smart_cropper OK')"
python -c "import modules.watermarker.module; print('watermarker OK')"
python -c "import modules.format_converter.module; print('format_converter OK')"
python -c "import modules.image_optimizer.module; print('image_optimizer OK')"
python -c "import modules.character_recognizer.module; print('character_recognizer OK')"
python -c "import modules.metadata.module; print('metadata OK')"
python -c "import modules.duplicate_finder.module; print('duplicate_finder OK')"
python -c "import modules.format_scanner.module; print('format_scanner OK')"
```

#### 2. Verificar locale completo
Confirmar que todos los namespaces están completos en `en.json` y `es.json`:
- `crop.*` — Smart Cropper
- `wm.*` — Watermarker
- `qs.*` — Quality Scorer
- `gallery.*` — Gallery
- `opt.*` — Image Optimizer
- `fscanner.*` — Format Scanner (ojo: `fs.*` pertenece a Face Scorer, usar `fscanner.*`)

#### 3. Push a GitHub
```bash
git add -p   # revisar diff antes de stagging
git commit -m "..."
git push origin master
```

---

### Futuro
- [ ] Configurar GitHub Actions / CI básico (smoke tests automáticos en push)
- [ ] **Face Embedding Exporter** — exportar perfiles ArcFace de `character_profiles.db` como `.npy` para uso en IP-Adapter FaceID / InstantID (ComfyUI/A1111). Ver nota técnica en BITACORA.
- [ ] **Quality Scorer Fase 3** — clasificación de encuadre (full body / half body / closeup) y orientación (frontal / 3/4 / perfil) usando datos de YOLOv8-pose + MediaPipe Face Mesh ya calculados en Fase 1. Ver diseño teórico en sesión 2026-04.

---

## Integración cherry-dl (implementada 2026-04)

**Objetivo:** Panopticon lee `catalog.db` de cherry-dl en modo read-only para
indexar solo las imágenes que el usuario conservó después del scraping.

### Archivos nuevos/modificados
- `core/catalog_reader.py` — lector read-only: `is_cherry_catalog()`, `get_image_files()`, `get_artist_info()`
- `modules/librarian/logic/indexer.py` — modo cherry-dl aware: detecta `catalog.db` y delega a CatalogReader

### Comportamiento
- Si una carpeta tiene `catalog.db` → modo cherry-dl: filtra por extensión de imagen + existencia en disco
- Si no → modo estándar con `os.walk` (sin cambios)
- Panopticon **nunca escribe** en archivos de cherry-dl
- Los datos de Panopticon (tags, ratings) siguen en metadata de imagen + `panopticon.db`

### Nota en cherry-dl
- `PANOPTICON_INTEGRATION.md` creado en `/run/media/system/Kilaya/githubs/cherry-dl/`

### Pendiente (mejoras futuras)
- [ ] Mostrar `url_source` y nombre de artista (vía `index.db`) en el sidebar del Librarian
- [ ] Deduplicación por `cherry_hash` en Panopticon (cruzar SHA-256 con su propio índice)
