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
*   **Principio de Activo Móvil**: Los metadatos (etiquetas, rating) deben ser persistentes. Si un archivo se mueve o renombra, el sistema debe ser capaz de reconocerlo mediante su **Hash** (DNI del archivo).
*   **Sincronización de Metadatos**: Se debe dar la opción de escribir etiquetas directamente en el archivo (chunks de PNG o XMP en JPEG) para asegurar la portabilidad total fuera de Panopticon.
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

## ✅ Checklist de Implementación Correcta

- [ ] ¿Mi clase hereda de `BaseModule`?
- [ ] ¿Están definidos los 4 campos de metadatos?
- [ ] ¿La lógica pesada corre en un `QThread` separado?
- [ ] ¿Uso `StandardToolLayout` para mantener la consistencia?
- [ ] ¿Cada cadena de texto está localizada mediante `self.tr()`?

---
> [!IMPORTANT]
> Seguir estos estándares asegura que tu módulo sea estable, eficiente y compatible con futuras actualizaciones del framework Panopticon.
