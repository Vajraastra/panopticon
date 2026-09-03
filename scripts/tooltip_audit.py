#!/usr/bin/env python
"""Cobertura de tooltips: carga las 13 vistas y cuenta controles con y sin ayuda.

Cuenta sobre los widgets REALES, no por grep: un `setToolTip` puede estar escrito y no
llegar al control, y hay controles que no se ven en el fuente porque los crea un helper.

    venv/Scripts/python.exe scripts/tooltip_audit.py

Imprime la cobertura por modulo y luego el detalle de lo que falta, con el texto de cada
control para saber cual es. En Windows, anteponer PYTHONIOENCODING=utf-8: los textos
llevan emoji y la consola por defecto es cp1252.
"""
import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from PySide6.QtWidgets import (QApplication, QPushButton, QToolButton, QCheckBox,
                               QRadioButton, QComboBox, QSpinBox, QDoubleSpinBox,
                               QLineEdit, QSlider, QPlainTextEdit, QTextEdit,
                               QListWidget, QTableWidget, QTabBar)
from core.mod_loader import ModuleLoader
from core.theme_manager import ThemeManager
from core.locale_manager import LocaleManager
from core.event_bus import EventBus

CONTROLS = (QPushButton, QToolButton, QCheckBox, QRadioButton, QComboBox, QSpinBox,
            QDoubleSpinBox, QLineEdit, QSlider)

app = QApplication(sys.argv)
tm = ThemeManager("theme_config.json")
ctx = {"theme_manager": tm, "locale_manager": LocaleManager(),
       "event_bus": EventBus(), "main_window": None}

loader = ModuleLoader()
rows = []
missing_detail = {}
total_c = total_t = 0

for name in sorted(loader.discover_modules()):
    try:
        m = loader.load_module(name, ctx)
        v = m.get_view()
    except Exception as e:
        rows.append((name, -1, -1, f"no carga: {e}"))
        continue

    widgets = []
    for cls in CONTROLS:
        widgets.extend(v.findChildren(cls))
    # los hijos internos de un spin/combo son QLineEdit: no cuentan como control propio
    # Los hijos internos de un spin/combo/lineedit (el QLineEdit del combo, la "x"
    # de limpiar) los crea Qt: no son controles que el modulo deba documentar.
    inner = set()
    for cls in (QSpinBox, QDoubleSpinBox, QComboBox, QLineEdit):
        for w in v.findChildren(cls):
            for child in w.findChildren(QLineEdit):
                inner.add(id(child))
            for child in w.findChildren(QToolButton):
                inner.add(id(child))
    widgets = [w for w in widgets if id(w) not in inner]

    with_tip = [w for w in widgets if w.toolTip().strip()]
    rows.append((name, len(widgets), len(with_tip), ""))
    total_c += len(widgets)
    total_t += len(with_tip)

    faltan = []
    for w in widgets:
        if w.toolTip().strip():
            continue
        label = ""
        for attr in ("text", "placeholderText"):
            if hasattr(w, attr):
                label = (getattr(w, attr)() or "").strip()
                if label:
                    break
        faltan.append(f"{type(w).__name__}: {label[:42] or '(sin texto)'}")
    if faltan:
        missing_detail[name] = faltan
    v.setParent(None)

print(f"{'modulo':22s} {'controles':>10s} {'con tip':>8s} {'cobertura':>10s}")
print("-" * 54)
for name, c, t, err in sorted(rows, key=lambda r: -(r[1] or 0)):
    if err:
        print(f"{name:22s} {err}")
        continue
    pct = f"{100 * t / c:.0f}%" if c else "-"
    print(f"{name:22s} {c:10d} {t:8d} {pct:>10s}")
print("-" * 54)
print(f"{'TOTAL':22s} {total_c:10d} {total_t:8d} "
      f"{100 * total_t / total_c:9.0f}%" if total_c else "")

print("\n\n=== controles sin tooltip, por modulo ===")
for name in sorted(missing_detail):
    print(f"\n-- {name} ({len(missing_detail[name])})")
    for line in missing_detail[name][:100]:
        print("   ", line)
