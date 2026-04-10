# TASKS — Panopticon

## Estado general
**Re-audit 2026-03 COMPLETO.** Todos los módulos auditados desde `6fd849a`. Listo para verificación final y push a GitHub.

---

## ✅ Completado

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

### Próxima sesión — Verificación y cierre

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
