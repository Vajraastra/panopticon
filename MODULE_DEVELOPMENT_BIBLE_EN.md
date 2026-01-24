# 📖 The Panopticon Bible: Module Development Guide

This guide details the philosophy and technical specifications required to build and integrate new tools into the **Panopticon** ecosystem.

---

## 🏛️ 1. Core Philosophy

Panopticon is not just an application; it is a **visual orchestration framework**. Every tool must adhere to these three pillars:

### A. Strict Modularization (Independence & Scalability)
*   **Atomic Responsibility**: A module must do one thing extremely well (e.g., crop, optimize, score). If a tool becomes too complex, split it.
*   **Decoupling**: Modules must never import each other directly. Interaction is handled solely through the `EventBus` or standard interfaces (`load_image_set`).
*   **Invisible Integration**: The system discovers modules automatically via folder scanning. If you drop a folder into `/modules`, it should "just work."

### B. High Performance (Optimization)
*   **UI/Logic Separation**: Heavy algorithms (AI, image processing) MUST run in background threads (`QThread`). Never freeze the main GUI thread.
*   **Lazy Loading**: Do not build complex UI components in the constructor. Only initialize the view inside `get_view()` when the user actually switches to your tool.
*   **Memory Stewardship**: Large image sets should be processed using generators or batching to prevent RAM spikes.

### C. Functional Depth
*   **Data Pipelines**: Tools should be designed to receive data from the `Librarian` or `Gallery` and potentially pass results forward.
*   **Headless Capability**: Implement `run_headless()` so your tool logic can be used in bulk automation without a GUI.

---

## 📁 2. Module Structure

Each module lives in its own subfolder within `/modules`.

```text
modules/my_tool/
├── module.py          # Entry point (MyToolModule class)
├── logic/             # Pure Python logic (No UI dependencies)
│   └── processing.py
├── assets/            # Icons, models, or tool-specific data
└── ui/                # (Optional) Subclassed PySide6 widgets
```

---

## 🛠️ 3. The `module.py` Contract

Every module must inherit from `BaseModule`.

### Technical Requirements:

```python
from core.base_module import BaseModule
from core.components.standard_layout import StandardToolLayout

class MyToolModule(BaseModule):
    def __init__(self):
        super().__init__()
        # 1. Mandatory Metadata for the Dashboard
        self._name = "My Tool"               # Display Name
        self._description = "Brief info."    # Short description
        self._icon = "🚀"                    # Emoji or path
        self.accent_color = "#00ffcc"        # Identity color
        self.view = None                     # Cached view

    def get_view(self) -> QWidget:
        """Visual entry point."""
        if self.view: return self.view
        
        # 2. Lazy UI Construction
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
        """Standard interface for receiving bulk data."""
        # Process incoming images from other modules
        pass
```

---

## 🎨 4. Visual Standards (StandardToolLayout)

To maintain a premium feel, tools must use the 3-Panel Layout:
- **Sidebar (Left, 320px fixed)**: For filters, settings, and tweakable parameters.
- **Canvas (Center/Top)**: Main work area, preview, or results grid.
- **Action Bar (Bottom)**: Execution buttons (e.g., "Process All", "Export").

---

## 🌍 5. Global Localization (i18n)

**Golden Rule**: NEVER hardcode user-facing strings.

1.  Use `self.tr("key", "default")`.
2.  Add keys to `locales/en.json` and `locales/es.json`.
3.  Format: `tool.module_name.element_id`.

---

## 🔗 6. Communication (EventBus)

Use the `EventBus` for global actions to avoid tight coupling:
```python
# Navigate back to main screen
self.context.get('event_bus').publish("navigate", "dashboard")
```

---

## ✅ Implementation Checklist

- [ ] Does my class inherit from `BaseModule`?
- [ ] Are the 4 metadata fields correctly set?
- [ ] Is heavy processing running on a `QThread`?
- [ ] Am I using `StandardToolLayout` for UI consistency?
- [ ] Is every single string localized via `self.tr()`?

---
> [!IMPORTANT]
> Adhering to these standards ensures your module remains stable, performant, and future-proof within the Panopticon framework.
