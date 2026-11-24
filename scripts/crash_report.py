"""Informe del último cierre brusco de Panopticon, listo para leer.

Uso (con el Python del venv):
    venv\\Scripts\\python.exe scripts\\crash_report.py [--logs logs] [--horas 72]

Reúne tres fuentes que por separado no bastan:
  1. `logs/crash.log`: el bloque del último proceso que murió (faulthandler, pila nativa,
     avisos de Qt y excepciones sin capturar). Lo escribe `core.crash_diagnostics`.
  2. El registro de Windows (Application Error): módulo, código y desplazamiento del fallo.
  3. Simbolización: cada `módulo+0xOFFSET` se traduce a la función exportada más cercana
     por debajo, leyendo la tabla de exportaciones de esa DLL (python313.dll, Qt6Core.dll,
     shiboken6…). Sin símbolos de depuración es una aproximación: nombra la zona, no la línea.
"""

from __future__ import annotations

import argparse
import re
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAME = re.compile(r"^\s+([\w.\-]+\.(?:dll|exe|pyd))\+0x([0-9a-fA-F]+)\s*$", re.I)
MARKERS = re.compile(r"Windows fatal exception|Fatal Python error|excepción nativa|excepción sin capturar|Qt FATAL", re.I)


def last_crash_block(path: Path) -> str | None:
    """Último bloque `=== arranque ===` que contiene un fallo."""
    if not path.exists():
        return None
    blocks = path.read_text(encoding="utf-8", errors="replace").split("\n=== ")
    for block in reversed(blocks):
        if MARKERS.search(block):
            return "=== " + block
    return None


def windows_events(hours: int) -> list[str]:
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Application Error'; "
        f"StartTime=(Get-Date).AddHours(-{int(hours)})}} -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Message -match 'python' } | ForEach-Object { "
        "'{0}  {1}' -f $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'), "
        "(($_.Message -split \"`n\" | Select-Object -Index 0,2,3,4 | ForEach-Object { $_.Trim() }) -join ' | ') }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=60).stdout
    except Exception as exc:  # no es esencial
        return [f"(no se pudo leer el registro de Windows: {exc})"]
    return [line for line in out.splitlines() if line.strip()]


def find_dll(name: str) -> Path | None:
    candidates = [Path(sys.executable).parent / name, Path(sys.base_prefix) / name]
    for pattern in (f"venv/Lib/site-packages/PySide6/{name}", f"venv/Lib/site-packages/shiboken6/{name}",
                    f"venv/Lib/site-packages/**/{name}"):
        candidates += list(ROOT.glob(pattern))
    windir = Path("C:/Windows/System32") / name
    candidates.append(windir)
    return next((c for c in candidates if c.is_file()), None)


def export_table(dll: Path) -> list[tuple[int, str]]:
    data = dll.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    optsz = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    directory = opt + (112 if magic == 0x20B else 96)
    exp_rva, _ = struct.unpack_from("<II", data, directory)
    if not exp_rva:
        return []
    sections = []
    for i in range(nsec):
        o = opt + optsz + i * 40
        vsize, vrva, rsize, rptr = struct.unpack_from("<IIII", data, o + 8)
        sections.append((vrva, max(vsize, rsize), rptr))

    def off(rva: int) -> int:
        for vrva, size, rptr in sections:
            if vrva <= rva < vrva + size:
                return rptr + rva - vrva
        raise ValueError(rva)

    e = off(exp_rva)
    n_funcs, n_names = struct.unpack_from("<II", data, e + 20)
    f_rva, n_rva, o_rva = struct.unpack_from("<III", data, e + 28)
    funcs = struct.unpack_from(f"<{n_funcs}I", data, off(f_rva))
    names = struct.unpack_from(f"<{n_names}I", data, off(n_rva))
    ords = struct.unpack_from(f"<{n_names}H", data, off(o_rva))
    table = []
    for name_rva, ordinal in zip(names, ords):
        start = off(name_rva)
        table.append((funcs[ordinal], data[start:data.index(b"\0", start)].decode("ascii", "replace")))
    return sorted(table)


_cache: dict[str, list[tuple[int, str]] | None] = {}


def symbolize(module: str, offset: int) -> str:
    if module not in _cache:
        dll = find_dll(module)
        try:
            _cache[module] = export_table(dll) if dll else None
        except Exception:
            _cache[module] = None
    table = _cache[module]
    if not table:
        return "(sin tabla de exportaciones)"
    below = [item for item in table if item[0] <= offset]
    if not below:
        return "(antes de la primera exportación)"
    start, name = below[-1]
    return f"{name}+{offset - start:#x}"


def annotate(block: str) -> str:
    lines = []
    for line in block.splitlines():
        match = FRAME.match(line)
        if match:
            line = f"{line}    ≈ {symbolize(match.group(1), int(match.group(2), 16))}"
        lines.append(line)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--logs", default=str(ROOT / "logs"))
    parser.add_argument("--horas", type=int, default=72, help="ventana del registro de Windows")
    args = parser.parse_args()
    # La consola de Windows suele ser cp1252: el informe lleva tildes y símbolos.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logs = Path(args.logs)

    print("=== Registro de Windows (cierres de python.exe) ===")
    events = windows_events(args.horas)
    print("\n".join(events) if events else "(sin cierres anormales en la ventana)")
    for line in events:
        match = re.search(r"python313\.dll.*?(0x[0-9a-fA-F]+)\s*$|Desplazamiento con errores: (0x[0-9a-fA-F]+)", line)
        if match:
            print("  → desplazamiento:", match.group(1) or match.group(2))

    print("\n=== Último bloque con fallo en crash.log ===")
    block = last_crash_block(logs / "crash.log")
    if block is None:
        print("(crash.log no registra ningún fallo)")
    else:
        # Los avisos de plugin offscreen no aportan nada al diagnóstico.
        text = "\n".join(line for line in block.splitlines() if "This plugin does not support" not in line)
        print(annotate(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
