# 📖 The Panopticon Bible: Module Development Guide
# 📖 La Biblia de Panopticon: Guía de Desarrollo de Módulos

> **[EN]** This guide details the philosophy and technical specifications required to build and integrate new tools into the **Panopticon** ecosystem.
>
> **[ES]** Esta guía detalla la filosofía y las especificaciones técnicas necesarias para crear e integrar nuevas herramientas en el ecosistema **Panopticon**.

---

## 🏛️ 1. Core Philosophy / Filosofía de Diseño

**[EN]** Panopticon is not just an application; it is a **visual orchestration framework**. Every tool must adhere to these three pillars:

**[ES]** Panopticon no es solo una aplicación, es un **framework de orquestación visual**. Cada herramienta debe seguir estos tres pilares fundamentales:

### A. Strict Modularization / Modularización Estricta (Independence & Scalability / Independencia y Escalabilidad)

**[EN]**
*   **Atomic Responsibility**: A module must do one thing extremely well (e.g., crop, optimize, score). If a tool becomes too complex, split it.
*   **Decoupling**: Modules must never import each other directly. Interaction is handled solely through the `EventBus` or standard interfaces (`load_image_set`).
*   **Invisible Integration**: The system discovers modules automatically via folder scanning. If you drop a folder into `/modules`, it should "just work."

**[ES]**
*   **Responsabilidad Atómica**: Un módulo debe hacer una cosa extremadamente bien (ej. recortar, optimizar). Si una herramienta se vuelve demasiado compleja, debe dividirse en submódulos.
*   **Desacoplamiento Total**: Los módulos nunca deben importarse entre sí directamente. La interacción se maneja exclusivamente a través del `EventBus` o interfaces estándar como `load_image_set`.
*   **Integración Invisible**: El sistema descubre módulos automáticamente escaneando carpetas. Si sueltas una carpeta en `/modules`, debería aparecer en el Dashboard sin tocar el core.

### B. High Performance / Optimización de Alto Nivel

**[EN]**
*   **UI/Logic Separation**: Heavy algorithms (AI, image processing) MUST run in background threads (`QThread`). Never freeze the main GUI thread.
*   **Lazy Loading**: Do not build complex UI components in the constructor. Only initialize the view inside `get_view()` when the user actually switches to your tool.
*   **Memory Stewardship**: Large image sets should be processed using generators or batching to prevent RAM spikes.

**[ES]**
*   **Separación UI/Lógica**: Los algoritmos pesados (IA, procesamiento de imágenes) DEBEN ejecutarse en hilos de fondo (`QThread`). Nunca bloquees el hilo principal de la interfaz.
*   **Carga Perezosa (Lazy Loading)**: No construyas componentes complejos de la UI en el constructor (`__init__`). Inicializa la vista solo dentro de `get_view()` cuando sea estrictamente necesario.
*   **Gestión de Memoria**: Al manejar miles de imágenes, usa generadores o procesamiento por lotes para evitar picos de consumo de RAM.

### C. Functional Depth & Persistence / Profundidad Funcional y Persistencia

**[EN]**
*   **Data Pipelines**: Tools should be designed to receive data from the `Librarian` or `Gallery` and potentially pass results forward.
*   **Mobile Asset Principle**: Tools that **transform** files (Cropper, Watermarker) must use the `StampLib` utility to embed original metadata into the new file.
*   **Static Library, Not Service**: Persistence is handled on-demand via a utility library, not background processes.
*   **Metadata Hygiene**: Read-only tools (like Scorer) must NEVER touch the physical file.
*   **Headless Capability**: Implement `run_headless()` so your tool logic can be used in bulk automation without a GUI.

**[ES]**
*   **Pipelines de Datos**: Diseña las herramientas para recibir datos del `Librarian` (Biblioteca) y poder pasar los resultados a la próxima herramienta.
*   **Principio de Activo Móvil**: Las herramientas que **transforman** archivos (Cropper, Watermarker) deben usar la librería `StampLib` para incrustar los metadatos originales en el nuevo archivo.
*   **Librería Estática, No Servicio**: La persistencia se maneja bajo demanda mediante una librería utilitaria, no procesos en segundo plano.
*   **Limpieza de Metadatos**: Herramientas de solo lectura (como Scorer) no deben tocar el archivo físico.
*   **Capacidad Headless**: Implementa `run_headless()` para que la lógica de tu herramienta pueda usarse en automatizaciones masivas sin necesidad de una interfaz gráfica.

---

## 📁 2. Module Structure / Estructura de un Módulo

**[EN]** Each module lives in its own subfolder within `/modules`.

**[ES]** Cada módulo vive en su propia subcarpeta dentro de `/modules`.

```text
[EN] modules/my_tool/          [ES] modules/mi_herramienta/
├── module.py          # Entry point / Punto de entrada
├── logic/             # Pure Python logic (No UI) / Lógica pura sin UI
│   └── processing.py
├── assets/            # Icons, models / Iconos, modelos
└── ui/                # (Optional) Custom PySide6 widgets / Widgets opcionales
```

---

## 🛠️ 3. The `module.py` Contract / El Contrato de `module.py`

**[EN]** Every module must inherit from `BaseModule`.

**[ES]** Es el contrato con el sistema. Debe contener una clase que herede de `BaseModule`.

```python
from core.base_module import BaseModule
from core.components.standard_layout import StandardToolLayout

# [EN] class MyToolModule(BaseModule):
# [ES] class MiHerramientaModule(BaseModule):
class MyToolModule(BaseModule):
    def __init__(self):
        super().__init__()
        # [EN] Mandatory Metadata for the Dashboard
        # [ES] Metadatos obligatorios para el Dashboard
        self._name = "My Tool"               # [EN] Display Name  [ES] Nombre (fallback)
        self._description = "Brief info."    # [EN] Short desc    [ES] Descripción corta
        self._icon = "🚀"                    # [EN] Emoji or path [ES] Emoji o ruta
        self.accent_color = "#00ffcc"        # [EN] Identity color [ES] Color de identidad
        self.view = None                     # [EN] Cached view   [ES] Caché de la vista

    def get_view(self) -> QWidget:
        """[EN] Visual entry point. [ES] Punto de entrada visual."""
        if self.view: return self.view

        # [EN] Lazy UI Construction / [ES] Construcción perezosa de la interfaz
        content = self._create_content()
        sidebar = self._create_sidebar()

        self.view = StandardToolLayout(
            content,
            sidebar_widget=sidebar,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        return self.view

    def load_image_set(self, paths: list):
        """[EN] Standard interface for receiving bulk data.
        [ES] Interfaz estándar para recibir datos masivos."""
        pass
```

---

## 🎨 4. Visual Standards / Estándar Visual (StandardToolLayout)

**[EN]** To maintain a premium feel, tools must use the 3-Panel Layout:
- **Sidebar (Left, 320px fixed)**: For filters, settings, and tweakable parameters.
- **Canvas (Center/Top)**: Main work area, preview, or results grid.
- **Action Bar (Bottom)**: Execution buttons (e.g., "Process All", "Export").

**[ES]** Para mantener una estética premium y coherente, todas las herramientas deben usar el Layout de 3 Paneles:
- **Sidebar (Izquierda, 320px fija)**: Para filtros, ajustes y parámetros variables.
- **Canvas (Centro/Superior)**: Área de trabajo principal, previsualización o rejilla de resultados.
- **Action Bar (Inferior)**: Botones de ejecución final (Ej: "Procesar Todo", "Exportar").

### Cyberpunk Aesthetic / Estética Cyberpunk (Mandatory / Mandatoria)

**[EN]** All modules must respect the system color palette (via `theme_manager.get_color()`):
- **Cyan (`accent_main`)**: Main accents, titles, and active borders.
- **Purple (`accent_info` / `Theme.ACCENT_INFO`)**: Secondary info, manual action buttons.
- **Neon Green (`accent_success` / `Theme.ACCENT_SUCCESS`)**: Confirmation or success states.
- **White (`text_primary`)**: High-contrast data and important values.

**[ES]** Todos los módulos deben respetar la paleta de colores del sistema (vía `theme_manager.get_color()`):
- **Cyan (`accent_main`)**: Acentos principales, títulos y bordes activos.
- **Morado (`accent_info` / `Theme.ACCENT_INFO`)**: Información secundaria, botones de acción manual.
- **Verde Neón (`accent_success` / `Theme.ACCENT_SUCCESS`)**: Confirmación o estados de éxito.
- **Blanco (`text_primary`)**: Datos de alto contraste y valores importantes.

### Image Display / Visualización de Imágenes

**[EN]**
- **1024px Rule**: Individual images should be scaled so their longest side is **exactly 1024px** (preserving aspect ratio).
- **Centering**: Always center vertically and horizontally in the viewport.

**[ES]**
- **Regla de 1024px**: Las imágenes individuales deben escalarse para que su lado más largo sea **exactamente 1024px** (conservando relación de aspecto).
- **Centrado**: Siempre centrar vertical y horizontalmente en el viewport.

---

## 🌍 5. Global Localization / Localización Global (i18n)

**[EN] Golden Rule**: NEVER hardcode user-facing strings.

**[ES] Regla de Oro**: NUNCA escribas texto directamente en el código de la interfaz para el usuario.

1. **[EN]** Use `self.tr("key", "default")`. / **[ES]** Usa `self.tr("clave", "default")`.
2. **[EN]** Add keys to `locales/en.json` and `locales/es.json`. / **[ES]** Añade las claves en `locales/en.json` y `locales/es.json`.
3. **[EN]** Format: `tool.module_name.element_id`. / **[ES]** Formato: `tool.nombre_modulo.id_elemento`.

---

## 🔗 6. Communication / Comunicación (EventBus)

**[EN]** Use the `EventBus` for global actions to avoid tight coupling:

**[ES]** Usa el `EventBus` para acciones globales y evitar el acoplamiento fuerte:

```python
# [EN] Navigate back to main Dashboard
# [ES] Volver al Dashboard principal
self.context.get('event_bus').publish("navigate", "dashboard")
```

---

## 📂 7. Centralized Cache System / Sistema de Cache Centralizado

> [!IMPORTANT]
> **[EN]** All tools must use the centralized cache system. Do NOT create arbitrary folders next to original files.
>
> **[ES]** Todas las herramientas deben usar el sistema de cache centralizado. No crear carpetas arbitrarias junto a los originales.

### Structure / Estructura

```
PANOPTICON_CACHE/              # [EN] Defined in core/paths.py / [ES] Definido en core/paths.py
├── watermarked/               # Watermarker output
├── optimized/                 # Image Optimizer output
├── cropped/                   # Cropper output
├── scored/                    # Quality Scorer output
│   ├── 100%/
│   ├── 90%/
│   └── ...
├── converted/                 # Format Converter output
└── temp/                      # [EN] Intermediate ops / [ES] Operaciones intermedias
```

### Implementation / Implementación

```python
from core.paths import CachePaths

class MyToolModule(BaseModule):
    def get_output_folder(self):
        return CachePaths.get_tool_cache("my_tool")

    def get_default_input_folder(self):
        return CachePaths.get_cache_root()

    def on_process_complete(self, output_folder):
        CachePaths.open_folder(output_folder)
```

### Rules / Reglas

1. **[EN]** Output always to cache. / **[ES]** Output siempre a cache. Nunca crear carpetas junto a originales.
2. **[EN]** File dialogs should default to `PANOPTICON_CACHE`. / **[ES]** Diálogos de selección inician en `PANOPTICON_CACHE`.
3. **[EN]** Call `CachePaths.open_folder()` when processing finishes. / **[ES]** Llamar `CachePaths.open_folder()` al terminar.
4. **[EN]** Cache cleanup is manual from Settings. / **[ES]** Usuario puede limpiar cache manualmente desde Settings.

---

## 🔐 8. Metadata Handling / Manejo de Metadata

> [!CAUTION]
> **[EN]** Prompts, tags, and ratings are critical data. They must never be accidentally lost.
>
> **[ES]** Los prompts, tags y ratings son datos críticos. Nunca deben perderse accidentalmente.

### Core Libraries / Librerías Core

| File / Archivo | Use / Uso |
|---|---|
| `core/metadata/extractor.py` | Read metadata from any format / Leer metadata de cualquier formato |
| `core/metadata/stamper.py` | Write metadata preserving existing / Escribir metadata preservando existente |
| `core/metadata/verifier.py` | Verify integrity post-copy / Verificar integridad post-copia |

### Tool Types / Tipos de Herramientas

#### Tools that TRANSFER metadata / Herramientas que TRANSFIEREN metadata
*(Optimizer, Cropper, Quality Scorer, Format Converter)*

```python
from core.metadata.stamper import MetadataStamper

def save_processed(self, source, dest):
    processed_img.save(dest)
    MetadataStamper.transfer(source, dest)  # [EN] Copy metadata from original / [ES] Copiar metadata del original
```

#### Tools that STRIP metadata / Herramientas que LIMPIAN metadata
*(Watermarker — for public distribution / para distribución pública)*

```python
def save_watermarked(self, source, dest):
    watermarked_img.save(dest)
    # [EN] Do NOT call transfer() — intentional privacy strip
    # [ES] NO llamar transfer() — limpieza intencional de privacidad
```

### Post-Batch Verification / Verificación Post-Batch

**[EN]** For bulk operations (100K+ images):

**[ES]** Para operaciones masivas (100K+ imágenes):

```python
from core.metadata.batch_verifier import BatchVerifier

def on_batch_complete(self):
    verifier = BatchVerifier(self.source_folder, self.output_folder)
    report = verifier.verify_all()
    self.show_verification_report(report)
```

---

## 🛡️ 9. Security Principles / Principios de Seguridad

### RULE #1 / REGLA #1: Never Modify Originals / Nunca Modificar Originales

```python
# ❌ [EN] FORBIDDEN / [ES] PROHIBIDO
img.save(original_path)

# ✅ [EN] CORRECT / [ES] CORRECTO
output_path = CachePaths.get_tool_cache("my_tool") / filename
img.save(output_path)
```

### RULE #2 / REGLA #2: Copies First, Cleanup Later / Copias Primero, Cleanup Después

```
1. [EN] Process → create copies in cache    / [ES] Procesar → crear copias en cache
2. [EN] Verify  → confirm metadata intact  / [ES] Verificar → confirmar metadata intacta
3. [EN] Report  → user reviews             / [ES] Mostrar reporte → usuario revisa
4. [EN] Cleanup (OPTIONAL) → user approves / [ES] Cleanup (OPCIONAL) → solo si usuario aprueba
```

### RULE #3 / REGLA #3: Preserve on Error / Preservar Errores

```python
# [EN] If verification fails, NEVER delete the original
# [ES] Si falla la verificación, NUNCA borrar el original
if verification.status == "FAILED":
    move_to_failed_folder(original, copy)
    log_error(verification.issues)
```

---

## ✅ 10. Implementation Checklist / Checklist de Implementación Correcta

### Structure / Estructura
- [ ] **[EN]** Does my class inherit from `BaseModule`? / **[ES]** ¿Mi clase hereda de `BaseModule`?
- [ ] **[EN]** Are the 4 metadata fields correctly set? / **[ES]** ¿Están definidos los 4 campos de metadatos?
- [ ] **[EN]** Am I using `StandardToolLayout`? / **[ES]** ¿Uso `StandardToolLayout` para mantener la consistencia?

### Performance / Rendimiento
- [ ] **[EN]** Is heavy processing running on a `QThread`? / **[ES]** ¿La lógica pesada corre en un `QThread` separado?
- [ ] **[EN]** Am I using lazy loading for UI? / **[ES]** ¿Uso lazy loading para componentes de UI?

### Localization / Localización
- [ ] **[EN]** Is every single user-facing string localized via `self.tr()`? / **[ES]** ¿Cada cadena de texto está localizada mediante `self.tr()`?

### Cache & Files / Cache y Archivos
- [ ] **[EN]** Does output go to `CachePaths.get_tool_cache()`? / **[ES]** ¿Output va a `CachePaths.get_tool_cache()`?
- [ ] **[EN]** Is the folder opened when processing finishes? / **[ES]** ¿Se abre la carpeta al finalizar el proceso?
- [ ] **[EN]** Does the file dialog start in the cache? / **[ES]** ¿El diálogo de selección inicia en el cache?

### Metadata
- [ ] **[EN]** Am I using `MetadataStamper.transfer()` for copies? / **[ES]** ¿Uso `MetadataStamper.transfer()` para copias?
- [ ] **[EN]** Or am I intentionally stripping metadata (like Watermarker)? / **[ES]** ¿O limpio metadata intencionalmente (como Watermarker)?
- [ ] **[EN]** For batch ops, am I using `BatchVerifier`? / **[ES]** ¿Para batch, uso `BatchVerifier` post-proceso?

### Security / Seguridad
- [ ] **[EN]** Do I NEVER modify original files? / **[ES]** ¿NUNCA modifico archivos originales?
- [ ] **[EN]** Are originals preserved on error? / **[ES]** ¿Preservo originales en caso de error?
- [ ] **[EN]** Does cleanup require user approval? / **[ES]** ¿El cleanup requiere aprobación del usuario?

---

> [!IMPORTANT]
> **[EN]** Adhering to these standards ensures your module remains stable, performant, and future-proof within the Panopticon framework.
>
> **[ES]** Seguir estos estándares asegura que tu módulo sea estable, eficiente y compatible con futuras actualizaciones del framework Panopticon.
