# TASKS — Panopticon

## Estado general
Revertido a versión estable `6fd849a`. App operativa. Audit pendiente de re-ejecutar módulo por módulo con commits individuales.

---

## ✅ Completado

### Auditoría de módulos (sesión 2025-03)
- [x] **Gallery** — revisado en sesión anterior
- [x] **Librarian** — revisado en sesión anterior
- [x] **Format Converter** — format_map corregido, imports top-level, padres de diálogos, scan_folder_for_conversion lógica de extensiones
- [x] **Image Optimizer** — currentIndex() en lugar de string matching, lazy guard en get_view(), imports top-level
- [x] **Character Recognizer** — cosine_similarity al top-level, bare except → logging, except duplicado eliminado
- [x] **Duplicate Finder** — módulo nuevo creado (hash MD5 + pHash perceptual, QThread, delete con confirmación)
- [x] **Metadata Hub** — action_save_current() siempre confirma antes de tocar el original (2 niveles de warning), action_export_copy() no inyecta metadata vacía
- [x] **Format Scanner** — strings hardcodeados → self.tr("fscanner.*"), guard división por cero, padre de QFileDialog
- [x] **Dummy Creator** — imports al top-level, padres de diálogos None → self.view
- [x] **main.py** — traceback al top-level, QSize/QIcon removidos
- [x] **standard_layout.py** — dead `pass` eliminado
- [x] **Locale EN + ES** — fc.*, df.*, fscanner.*, meta.warn.*, meta.title/desc/tech_meta/open_images añadidos; meta.export.success {path}→{dest} corregido
- [x] **Git history limpiado** — git-filter-repo eliminó modelos, DBs, scripts de debug de los 53 commits
- [x] **Commit** — `16a1a23` refactor(audit): Full module audit

---

## 🔲 Pendiente

### Inmediato (próxima sesión)
- [ ] **Test de humo por módulo** — antes de auditar, verificar estado real: `python -c "import modules.X.module"`
- [ ] **Re-audit módulo por módulo** — un módulo = un commit inmediato; orden: locale/themes → módulos simples → metadata al final
- [ ] **Push a GitHub** — remote ya configurado (`origin`), hacer push tras primer módulo estable
- [ ] **Locale + themes cross-cutting** — verificar claves faltantes y coherencia de themes en todos los módulos

### Mediano plazo
- [ ] **Character Recognizer locale** — ~35 strings hardcodeados en recognition_view.py; necesita helper tr() con LocaleManager singleton y claves `cr.*` en locale files
- [ ] **Quality Scorer** — algoritmo de scoring pendiente de decisión; módulo existe pero lógica incompleta
- [ ] **PSD Composer** — módulo existe, revisar en auditoría

### Futuro
- [ ] Configurar GitHub Actions / CI básico
- [ ] Tests automatizados de importación de módulos
