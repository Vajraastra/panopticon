# TASKS — Panopticon

## Estado general
Re-audit en progreso (iniciado desde `6fd849a`). Módulos 1-7 completados con commits individuales. Faltan 3 módulos críticos.

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
- [x] **Locale EN + ES** — 23 keys `dup.*` añadidas; verificar que `crop.*`, `wm.*`, `qs.*`, `gallery.*` están completas
- [x] **Character Recognizer** — audit completo + nuevas features: modo Illustration/Real Person (ArcFace+YuNet+landmark alignment), locale `cr.*` (~40 keys), logging, `run_headless()`, botones Skip/Return, borrado de tags, Auto Mode con `AutoScanWorker` (sugerencia dominante al 60%)

---

## 🔲 Pendiente

### Inmediato (próxima sesión) — ORDEN CRÍTICO
- [ ] **Librarian** — módulo crítico (DB central). Audit: locale, theme dinámico, CachePaths, logging
- [ ] **Metadata Hub** — el más crítico, SIEMPRE al final. 2 niveles de warning en save_current, no inyectar metadata vacía

### Post-audit
- [ ] **Verificar locale completo** — keys `crop.*`, `wm.*`, `qs.*`, `gallery.*`, `opt.*` en en.json y es.json
- [ ] **Push a GitHub** — tras completar los 3 módulos restantes
- [ ] **Tests automatizados de importación** — `python -c "import modules.X.module"` para todos

### Futuro
- [ ] Configurar GitHub Actions / CI básico
- [ ] **Face Embedding Exporter** — exportar perfiles ArcFace de `character_profiles.db` como `.npy` para uso en IP-Adapter FaceID / InstantID (ComfyUI/A1111). Ver nota técnica en BITACORA.
