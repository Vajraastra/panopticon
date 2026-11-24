"""core.crash_diagnostics deja traza de excepciones, avisos de Qt y fallos nativos."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CrashDiagnostics(unittest.TestCase):
    def run_child(self, body: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        logs = Path(tmp.name) / "logs"
        code = ("import sys; sys.path.insert(0, %r)\n"
                "from core.crash_diagnostics import install_crash_diagnostics\n"
                "assert install_crash_diagnostics(%r)\n" % (str(ROOT), str(logs))) + body
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env, timeout=60)
        path = logs / "crash.log"
        return proc, (path.read_text(encoding="utf-8", errors="replace") if path.exists() else "")

    def test_uncaught_exception_is_logged_with_its_stack(self):
        proc, text = self.run_child("def boom():\n    raise ValueError('fallo-de-prueba')\nboom()\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("excepción sin capturar", text)
        self.assertIn("ValueError: fallo-de-prueba", text)

    def test_uncaught_exception_in_a_thread_is_logged(self):
        _, text = self.run_child(
            "import threading\n"
            "t = threading.Thread(target=lambda: 1/0, name='hilo-x'); t.start(); t.join()\n")
        self.assertIn("hilo hilo-x", text)
        self.assertIn("ZeroDivisionError", text)

    def test_qt_warnings_are_written_but_debug_is_not(self):
        _, text = self.run_child(
            "from PySide6.QtCore import qWarning, qDebug\n"
            "qDebug('ruido'); qWarning('aviso-de-prueba')\n")
        self.assertIn("Qt WARNING: aviso-de-prueba", text)
        self.assertNotIn("ruido", text)

    def test_native_crash_dumps_the_python_stack_before_dying(self):
        proc, text = self.run_child("import faulthandler\nfaulthandler._sigsegv()\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertRegex(text, r"(?i)fatal (python )?(error|exception)")
        self.assertIn("File", text)   # la pila de Python quedó escrita

    @unittest.skipUnless(sys.platform == "win32", "la pila nativa es de Windows")
    def test_native_crash_records_the_native_stack_with_modules(self):
        # Un hilo nativo cuyo punto de entrada es la dirección 0: acceso inválido real, sin
        # que ctypes lo convierta en excepción de Python (como el crash dentro de python313.dll).
        proc, text = self.run_child(
            "import ctypes, time\n"
            "ctypes.windll.kernel32.CreateThread(None, 0, ctypes.c_void_p(0), None, 0, None)\n"
            "time.sleep(10)\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("excepción nativa 0xc0000005", text)
        self.assertIn("pila nativa", text)
        frames = [line for line in text.splitlines() if line.startswith("  ") and "+0x" in line]
        self.assertGreaterEqual(len(frames), 2, text)
        # La pila nombra módulos reales, no solo direcciones.
        self.assertRegex(text, r"(?i)(ntdll|kernel32|kernelbase)\.dll\+0x")

    def test_install_never_raises_when_logs_dir_is_unusable(self):
        sys.path.insert(0, str(ROOT))
        from core.crash_diagnostics import install_crash_diagnostics
        with tempfile.NamedTemporaryFile() as blocker:
            # Un archivo donde debería haber una carpeta: no puede crear logs/.
            self.assertIsNone(install_crash_diagnostics(Path(blocker.name) / "logs"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
