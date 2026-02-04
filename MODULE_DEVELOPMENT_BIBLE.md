# 📖 La Biblia de Panopticon: Guía de Desarrollo de Módulos

Esta guía detalla la filosofía y las especificaciones técnicas necesarias para crear e integrar nuevas herramientas en el ecosistema **Panopticon**.

---

## 🏛️ 1. Filosofía de Diseño

Panopticon no es solo una aplicación, es un **framework de orquestación visual**. Cada herramienta debe seguir estos tres pilares fundamentales:

### A. Modularización Estricta (Independencia y Escalabilidad)
*   **Responsabilidad Atómica**: Un módulo debe hacer una cosa extremadamente bien (ej. recortar, optimizar). Si una herramienta se vuelve demasiado compleja, debe dividirse en submódulos.
*   **Desacoplamiento Total**: Los módulos nunca deben importarse entre sí directamente. La interacción se maneja exclusivamente a través del `EventBus` o interfaces estándar como `load_image_set`.
*   **Integración Invisible**: El sistema descubre módulos automáticamente escaneando carpetas. Si sueltas una carpeta en `/modules`, debería aparecer en el Dashboard sin tocar el core.

### B. Optimización de Alto Nivel (Rendimiento)
*   **Separación UI/Lógica**: Los algoritmos pesados (IA, procesamiento de imágenes) DEBEN ejecutarse en hilos de fondo (`QThread`). Nunca bloquees el hilo principal de la interfaz.
*   **Carga Perezosa (Lazy Loading)**: No construyas componentes complejos de la UI en el constructor (`__init__`). Inicializa la vista solo dentro de `get_view()` cuando sea estrictamente necesario.
*   **Gestión de Memoria**: Al manejar miles de imágenes, usa generadores o procesamiento por lotes para evitar picos de consumo de RAM.

### C. Profundidad Funcional y Persistencia
*   **Pipelines de Datos**: Diseña las herramientas para recibir datos del `Librarian` (Biblioteca) y poder pasar los resultados a la próxima herramienta.
*   **Principio de Activo Móvil**: Las herramientas que **transforman** archivos (Cropper, Watermarker) deben usar la librería `StampLib` para incrustar los metadatos originales en el nuevo archivo.
*   **Librería Estática, No Servicio**: La persistencia se maneja bajo demanda mediante una librería utilitaria, no procesos en segundo plano.
*   **Limpieza de Metadatos**: Herramientas de solo lectura (como Scorer) no deben tocar el archivo físico.
*   **Capacidad Headless**: Implementa `run_headless()` para que la lógica de tu herramienta pueda usarse en automatizaciones masivas sin necesidad de una interfaz gráfica.

---

## 📁 2. Estructura de un Módulo

Cada módulo vive en su propia subcarpeta dentro de `/modules`.

```text
modules/mi_herramienta/
├── module.py          # Punto de entrada (Clase MiHerramientaModule)
├── logic/             # Lógica pura (Python sin dependencias de UI)
│   └── processing.py
├── assets/            # Iconos, modelos o datos específicos
└── ui/                # (Opcional) Widgets personalizados de PySide6
```

---

## 🛠️ 3. El Contrato de `module.py`

Es el contrato con el sistema. Debe contener una clase que herede de `BaseModule`.

### Requisitos Técnicos Mínimos:

```python
from core.base_module import BaseModule
from core.components.standard_layout import StandardToolLayout

class MiHerramientaModule(BaseModule):
    def __init__(self):
        super().__init__()
        # 1. Metadatos obligatorios para el Dashboard
        self._name = "Mi Herramienta"        # Nombre (fallback)
        self._description = "Descripción."   # Descripción corta
        self._icon = "🚀"                    # Emoji o ruta
        self.accent_color = "#00ffcc"        # Color de identidad
        self.view = None                     # Caché de la vista

    def get_view(self) -> QWidget:
        """Punto de entrada visual."""
        if self.view: return self.view
        
        # 2. Construcción perezosa de la interfaz
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
        """Interfaz estándar para recibir datos masivos."""
        # Procesa las imágenes enviadas desde otros módulos
        pass
```

---

## 🎨 4. Estándar Visual (StandardToolLayout)

Para mantener una estética premium y coherente, todas las herramientas deben usar el Layout de 3 Paneles:
- **Sidebar (Izquierda, 320px fija)**: Para filtros, ajustes y parámetros variables.
- **Canvas (Centro/Superior)**: Área de trabajo principal, previsualización o rejilla de resultados.
- **Action Bar (Inferior)**: Botones de ejecución final (Ej: "Procesar Todo", "Exportar").

### Estética Cyberpunk (Mandatoria)
Todos los módulos deben respetar la paleta de colores del sistema:
- **Cyan (`Theme.ACCENT_MAIN`)**: Acentos principales, títulos y bordes activos.
- **Morado (`Theme.ACCENT_INFO`)**: Información secundaria, botones de acción manual.
- **Verde Neón (`Theme.ACCENT_SUCCESS`)**: Confirmación o estados de éxito.
- **Blanco (`Theme.TEXT_PRIMARY`)**: Datos de alto contraste y valores importantes.
- **Sidebar Data**: Si el Canvas está vacío o es minimalista, el Sidebar debe enriquecerse con metadatos del archivo (Nombre, Peso, Tags, Dimensiones).

### Visualización de Imágenes
- **Regla de 1024px**: Para mantener la consistencia en el Canvas, las imágenes individuales deben escalarse para que su lado más largo sea **exactamente 1024px** (conservando relación de aspecto).
- **Centrado**: Siempre centrar vertical y horizontalmente en el viewport.


---

## 🌍 5. Localización Global (i18n)

**Regla de Oro**: NUNCA escribas texto directamente en el código de la interfaz para el usuario.

1.  Usa `self.tr("clave", "default")`.
2.  Añade las claves en `locales/en.json` y `locales/es.json`.
3.  Formato: `tool.nombre_modulo.id_elemento`.

---

## 🔗 6. Comunicación (EventBus)

Usa el `EventBus` para acciones globales y evitar el acoplamiento fuerte:
```python
# Volver al Dashboard principal
self.context.get('event_bus').publish("navigate", "dashboard")
```

---

## 📂 7. Sistema de Cache Centralizado

> [!IMPORTANT]
> **Todas las herramientas deben usar el sistema de cache centralizado.**
> No crear carpetas arbitrarias junto a los originales.

### Estructura

```
PANOPTICON_CACHE/              # Definido en core/paths.py
├── watermarked/               # Watermarker output
├── optimized/                 # Image Optimizer output
├── cropped/                   # Cropper output
├── scored/                    # Quality Scorer output
│   ├── 100%/
│   ├── 90%/
│   └── ...
├── converted/                 # Format Converter output
└── temp/                      # Operaciones intermedias
```

### Implementación

```python
from core.paths import CachePaths

class MiHerramientaModule(BaseModule):
    def get_output_folder(self):
        """Retorna la subcarpeta de cache para este módulo."""
        return CachePaths.get_tool_cache("mi_herramienta")
    
    def get_default_input_folder(self):
        """Retorna la carpeta por defecto para seleccionar archivos."""
        return CachePaths.get_cache_root()
    
    def on_process_complete(self, output_folder):
        """Al finalizar, abrir la carpeta de output."""
        CachePaths.open_folder(output_folder)
```

### Reglas

1. **Output siempre a cache**: Nunca crear carpetas junto a originales
2. **Default input**: Diálogos de selección inician en `PANOPTICON_CACHE`
3. **Auto-abrir al finalizar**: Llamar `CachePaths.open_folder()` al terminar
4. **Limpieza**: Usuario puede limpiar cache manualmente desde Settings

---

## 🔐 8. Manejo de Metadata

> [!CAUTION]
> **Los prompts, tags y ratings son datos críticos.** Nunca deben perderse accidentalmente.

### Librerías Core

| Archivo | Uso |
|---------|-----|
| `core/metadata/extractor.py` | Leer metadata de cualquier formato |
| `core/metadata/stamper.py` | Escribir metadata preservando existente |
| `core/metadata/verifier.py` | Verificar integridad post-copia |

### Tipos de Herramientas

#### Herramientas que TRANSFIEREN metadata
*(Optimizer, Cropper, Quality Scorer, Format Converter)*

```python
from core.metadata.stamper import MetadataStamper

def save_processed(self, source, dest):
    # Guardar imagen procesada
    processed_img.save(dest)
    # Transferir metadata de original a copia
    MetadataStamper.transfer(source, dest)
```

#### Herramientas que LIMPIAN metadata
*(Watermarker - para distribución pública)*

```python
def save_watermarked(self, source, dest):
    # Guardar SIN metadata (privacidad)
    watermarked_img.save(dest)
    # NO llamar transfer() - intencional
```

### Verificación Post-Batch

Para operaciones masivas (100K+ imágenes):

```python
from core.metadata.batch_verifier import BatchVerifier

def on_batch_complete(self):
    verifier = BatchVerifier(self.source_folder, self.output_folder)
    report = verifier.verify_all()
    self.show_verification_report(report)
```

---

## 🛡️ 9. Principios de Seguridad

### REGLA #1: Nunca Modificar Originales

```python
# ❌ PROHIBIDO
img.save(original_path)

# ✅ CORRECTO
output_path = CachePaths.get_tool_cache("mi_herramienta") / filename
img.save(output_path)
```

### REGLA #2: Copias Primero, Cleanup Después

```
1. Procesar → crear copias en cache
2. Verificar → confirmar metadata intacta
3. Mostrar reporte → usuario revisa
4. Cleanup (OPCIONAL) → solo si usuario aprueba
```

### REGLA #3: Preservar Errores

```python
# Si falla la verificación, NUNCA borrar original
if verification.status == "FAILED":
    # Mover a failed/ para revisión manual
    move_to_failed_folder(original, copy)
    log_error(verification.issues)
```

---

## ✅ 10. Checklist de Implementación Correcta

### Estructura
- [ ] ¿Mi clase hereda de `BaseModule`?
- [ ] ¿Están definidos los 4 campos de metadatos?
- [ ] ¿Uso `StandardToolLayout` para mantener la consistencia?

### Rendimiento
- [ ] ¿La lógica pesada corre en un `QThread` separado?
- [ ] ¿Uso lazy loading para componentes de UI?

### Localización
- [ ] ¿Cada cadena de texto está localizada mediante `self.tr()`?

### Cache y Archivos
- [ ] ¿Output va a `CachePaths.get_tool_cache()`?
- [ ] ¿Se abre la carpeta al finalizar el proceso?
- [ ] ¿El diálogo de selección inicia en el cache?

### Metadata
- [ ] ¿Uso `MetadataStamper.transfer()` para copias?
- [ ] ¿O limpio metadata intencionalmente (como Watermarker)?
- [ ] ¿Para batch, uso `BatchVerifier` post-proceso?

### Seguridad
- [ ] ¿NUNCA modifico archivos originales?
- [ ] ¿Preservo originales en caso de error?
- [ ] ¿El cleanup requiere aprobación del usuario?

---
> [!IMPORTANT]
> Seguir estos estándares asegura que tu módulo sea estable, eficiente y compatible con futuras actualizaciones del framework Panopticon.
