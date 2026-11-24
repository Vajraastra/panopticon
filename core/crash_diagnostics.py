"""Diagnóstico de cierres bruscos: deja traza de lo que hoy se pierde.

Windows registra cierres nativos de python.exe (Qt6Core 0xc0000409, python313.dll
0xc0000005, ntdll 0xc0000008) sin que quede nada en los logs: sin consola, el stderr
se pierde y un qFatal/abort mata el proceso antes de que logging escriba. Aquí:

- `faulthandler` vuelca la pila de Python de todos los hilos ante un fallo nativo
  que el sistema permita interceptar (acceso inválido, abort).
- Un manejador de mensajes de Qt escribe avisos/errores/fatales con flush inmediato:
  un qFatal explica su motivo justo antes de abortar (0xc0000409 no se puede capturar,
  pero su mensaje sí).
- Las excepciones sin capturar (hilo principal y secundarios) se registran con su pila.

Todo va a `logs/crash.log`, que se rota a `crash.log.1` al pasar de 1 MiB.
Best-effort: un fallo aquí nunca debe impedir arrancar la app.
"""

from __future__ import annotations

import faulthandler
import logging
import sys
import threading
import time
import traceback
from pathlib import Path

_MAX_BYTES = 1024 * 1024
_file = None  # el handle debe vivir mientras viva el proceso (faulthandler lo usa)
_lock = threading.Lock()
log = logging.getLogger(__name__)


def _write(text: str) -> None:
    if _file is None:
        return
    with _lock:
        try:
            _file.write(text)
            _file.flush()
        except Exception:
            pass


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _qt_handler(mode, context, message) -> None:
    try:
        from PySide6.QtCore import QtMsgType
        if mode in (QtMsgType.QtDebugMsg, QtMsgType.QtInfoMsg):
            return
        where = f" ({context.file}:{context.line})" if getattr(context, "file", None) else ""
        label = {QtMsgType.QtWarningMsg: "WARNING", QtMsgType.QtCriticalMsg: "CRITICAL",
                 QtMsgType.QtFatalMsg: "FATAL"}.get(mode, str(mode))
        _write(f"{_stamp()} Qt {label}: {message}{where}\n")
    except Exception:
        pass


def _excepthook(exc_type, exc, tb) -> None:
    _write(f"{_stamp()} excepción sin capturar (hilo principal)\n"
           + "".join(traceback.format_exception(exc_type, exc, tb)))
    sys.__excepthook__(exc_type, exc, tb)


def _thread_excepthook(args) -> None:
    name = args.thread.name if args.thread else "?"
    _write(f"{_stamp()} excepción sin capturar (hilo {name})\n"
           + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))


_native_filter = None  # referencia viva: el recolector no debe liberar el callback


def _install_native_stack_filter() -> None:
    """Escribe la pila NATIVA (módulo+desplazamiento) ante una excepción sin manejar.

    faulthandler solo ve marcos de Python; un acceso inválido dentro de python313.dll o de
    Qt/PySide queda sin autor. RtlCaptureStackBackTrace, llamado desde el filtro, desenrolla
    a través del despachador de excepciones hasta los marcos que provocaron el fallo. Las
    direcciones se simbolizan después con la tabla de exportaciones de cada DLL.
    """
    global _native_filter
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32")
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.RtlCaptureStackBackTrace.restype = ctypes.c_ushort
    ntdll.RtlCaptureStackBackTrace.argtypes = [
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    kernel32.GetModuleHandleExW.argtypes = [
        wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.HMODULE)]
    kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    kernel32.SetUnhandledExceptionFilter.argtypes = [ctypes.c_void_p]
    kernel32.SetUnhandledExceptionFilter.restype = ctypes.c_void_p
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)

    def describe(address: int) -> str:
        handle = wintypes.HMODULE()
        # FROM_ADDRESS (0x4) | UNCHANGED_REFCOUNT (0x2)
        if kernel32.GetModuleHandleExW(0x6, ctypes.c_void_p(address), ctypes.byref(handle)):
            buffer = ctypes.create_unicode_buffer(520)
            kernel32.GetModuleFileNameW(handle, buffer, 520)
            return f"{Path(buffer.value).name}+{address - handle.value:#x}"
        return f"{address:#x}"

    def handler(info) -> int:
        try:
            record = ctypes.cast(info, ctypes.POINTER(ctypes.c_void_p))[0]
            code = ctypes.cast(record, ctypes.POINTER(ctypes.c_uint32))[0]
            where = ctypes.cast(record, ctypes.POINTER(ctypes.c_void_p))[2]  # ExceptionAddress
            frames = (ctypes.c_void_p * 48)()
            count = ntdll.RtlCaptureStackBackTrace(0, 48, frames, None)
            lines = [f"{_stamp()} excepción nativa {code:#010x} en {describe(where or 0)}",
                     "pila nativa (más reciente primero):"]
            lines += [f"  {describe(frames[i] or 0)}" for i in range(count)]
            _write("\n".join(lines) + "\n")
        except Exception:
            pass
        return 0  # EXCEPTION_CONTINUE_SEARCH: el proceso termina como antes y WER lo registra

    _native_filter = callback_type(handler)
    kernel32.SetUnhandledExceptionFilter(ctypes.cast(_native_filter, ctypes.c_void_p))


def install_crash_diagnostics(logs_dir: Path | str) -> Path | None:
    """Activa el diagnóstico; devuelve la ruta de crash.log, o None si no pudo."""
    global _file
    try:
        logs_dir = Path(logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / "crash.log"
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            path.replace(path.with_name("crash.log.1"))
        _file = open(path, "a", encoding="utf-8", buffering=1)
        _write(f"\n=== {_stamp()} arranque (pid {__import__('os').getpid()}) ===\n")
        faulthandler.enable(file=_file, all_threads=True)
        try:
            _install_native_stack_filter()
        except Exception:
            log.debug("sin filtro de pila nativa", exc_info=True)
        sys.excepthook = _excepthook
        threading.excepthook = _thread_excepthook
        try:
            from PySide6.QtCore import qInstallMessageHandler
            qInstallMessageHandler(_qt_handler)
        except Exception:
            log.debug("sin manejador de mensajes de Qt", exc_info=True)
        return path
    except Exception:
        log.warning("no se pudo activar el diagnóstico de crashes", exc_info=True)
        return None
