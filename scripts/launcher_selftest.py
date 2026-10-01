"""Windows-only private launcher/process fixtures; no installation or network."""
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PS = str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe')
HELPER = ROOT/'scripts/launcher_process.ps1'


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != 'nt':
            raise unittest.SkipTest('Windows is the supported launcher target')
        cls.build = tempfile.TemporaryDirectory(prefix='launcher-native-')
        folder = Path(cls.build.name)
        cls.fake_uv, cls.fake_python = folder/'uv.exe', folder/'python.exe'
        python_source = r'''using System; using System.IO;
class FakePython { static int Main(string[] args) {
  if(args.Length > 0 && args[0] == "-c") {
    string version = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "version.txt");
    return File.Exists(version) && File.ReadAllText(version) == "bad" ? 1 : 0;
  }
  File.WriteAllText(Environment.GetEnvironmentVariable("PANOPTICON_TEST_APP_LOG"),
    String.Join("\n", args));
  return Int32.Parse(Environment.GetEnvironmentVariable("PANOPTICON_TEST_APP_EXIT") ?? "0");
} }'''
        uv_source = r'''using System; using System.IO;
class FakeUv { static int Main(string[] args) {
  File.AppendAllText(Environment.GetEnvironmentVariable("PANOPTICON_TEST_UV_LOG"),
    String.Join("|", args) + "|" + Environment.GetEnvironmentVariable("UV_PYTHON_PREFERENCE")
    + "|" + Environment.GetEnvironmentVariable("UV_CACHE_DIR") + "\n");
  int code = Int32.Parse(Environment.GetEnvironmentVariable("PANOPTICON_TEST_UV_EXIT") ?? "0");
  if(code != 0) return code;
  if(args[0] == "venv") {
    string scripts = Path.Combine(args[1], "Scripts"); Directory.CreateDirectory(scripts);
    File.Copy(Environment.GetEnvironmentVariable("PANOPTICON_TEST_PYTHON_TEMPLATE"),
      Path.Combine(scripts,"python.exe"));
  }
  return 0;
} }'''
        script = folder/'compile.ps1'
        script.write_text("$ErrorActionPreference='Stop'\n" +
            "Add-Type -TypeDefinition @'\n" + python_source + "\n'@ -OutputAssembly " + literal(cls.fake_python) + " -OutputType ConsoleApplication\n" +
            "Add-Type -TypeDefinition @'\n" + uv_source + "\n'@ -OutputAssembly " + literal(cls.fake_uv) + " -OutputType ConsoleApplication\n", encoding='utf-8-sig')
        result = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)], capture_output=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace'))

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='panopticon launcher ! á ')
        self.root = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env.update(PANOPTICON_TEST_UV_LOG=str(self.root/'uv.log'),
            PANOPTICON_TEST_APP_LOG=str(self.root/'app.log'),
            PANOPTICON_TEST_PYTHON_TEMPLATE=str(self.fake_python),
            PANOPTICON_TEST_UV_EXIT='0', PANOPTICON_TEST_APP_EXIT='0')

    def tearDown(self):
        self.temp.cleanup()

    def ps(self, code, *, host=PS):
        script = self.root/'check.ps1'
        script.write_text("$ErrorActionPreference='Stop'\n. " + literal(HELPER) + '\n' + code, encoding='utf-8-sig')
        return subprocess.run([host, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
            env=self.env, capture_output=True, text=True, errors='replace', timeout=40)

    def invoke(self, args, *, timeout=5000, attempts=3, host=PS, cache=''):
        argv = '@(' + ','.join(literal(a) for a in args) + ')'
        return self.ps("$code = Invoke-LauncherProcess " + literal(sys.executable) + ' ' + argv + ' ' + literal(self.root) +
            f' -TimeoutMilliseconds {timeout} -Attempts {attempts} -CachePath ' + literal(cache) + '\nexit $code\n', host=host)

    def test_normal_completion_does_not_retry(self):
        target = self.root/'count.txt'
        result = self.invoke(['-c', 'from pathlib import Path; p=Path(__import__("sys").argv[1]); p.write_text(p.read_text()+"x" if p.exists() else "x")', str(target)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(), 'x')
        self.assertEqual(result.stdout.count('attempt '), 1)

    def test_failure_retries_exactly_three_times(self):
        target = self.root/'count.txt'
        result = self.invoke(['-c', 'import sys; from pathlib import Path; p=Path(sys.argv[1]); p.write_text(p.read_text()+"x" if p.exists() else "x"); sys.exit(7)', str(target)])
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(target.read_text(), 'xxx')

    def test_impossible_timeout_kills_and_retries(self):
        result = self.invoke(['-c', 'import time; time.sleep(30)'], timeout=1, attempts=2)
        self.assertEqual(result.returncode, 1460, result.stderr)
        self.assertEqual(result.stdout.count('Timeout after'), 2)

    def test_timeout_stops_child_tree(self):
        self.check_child_tree(PS)

    def test_modern_timeout_stops_child_tree(self):
        self.check_child_tree(shutil.which('pwsh'))

    def test_failed_tree_discovery_still_stops_installer(self):
        code = ('function Get-CimInstance { throw "private discovery failure" }\n'
            '$psi=[Diagnostics.ProcessStartInfo]::new(); $psi.FileName=' + literal(sys.executable) + '; '
            '$psi.Arguments=\'-c "import time; time.sleep(30)"\'; $psi.UseShellExecute=$false; '
            '$p=[Diagnostics.Process]::Start($psi)\n'
            'try { try { Stop-LauncherProcess $p } catch { if (-not $p.HasExited) { exit 9 }; exit 0 } } finally { $p.Dispose() }\nexit 8')
        result = self.ps(code)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def check_child_tree(self, host):
        pid = self.root/'child.pid'
        result = self.invoke(['-c', 'import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)', str(pid)], timeout=1500, attempts=1, host=host)
        self.assertEqual(result.returncode, 1460, result.stderr)
        self.assertTrue(pid.exists(), result.stdout)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, int(pid.read_text()))
        if handle:
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
            kernel.CloseHandle(handle)
            self.assertNotEqual(code.value, 259, 'child still running')

    def test_modern_powershell_normal_completion(self):
        host = shutil.which('pwsh')
        self.assertIsNotNone(host)
        result = self.invoke(['-c', 'pass'], host=host)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('attempt '), 1)

    def test_modern_powershell_timeout(self):
        result = self.invoke(['-c', 'import time; time.sleep(30)'], timeout=1, attempts=2, host=shutil.which('pwsh'))
        self.assertEqual(result.returncode, 1460, result.stderr)
        self.assertEqual(result.stdout.count('Timeout after'), 2)

    def test_argument_roundtrip(self):
        args = ['', 'á ! % _', 'a"b', 'trailing\\', 'slash\\"quote', str(self.root)]
        target = self.root/'args.json'
        result = self.invoke(['-c', 'import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))', str(target), *args])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(target.read_text()), args)

    def test_retry_cleans_only_own_temporary_cache(self):
        cache = self.root/'.uv-launcher-cache'
        scratch = cache/'wheels/.tmp-orphan'
        scratch.mkdir(parents=True)
        (scratch/'partial').write_text('partial')
        keep = cache/'wheels/complete.whl'
        keep.write_text('keep')
        foreign = self.root/'foreign/.tmp-other'
        foreign.mkdir(parents=True)
        result = self.invoke(['-c', 'import sys; sys.exit(3)'], attempts=2, cache=str(cache))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse(scratch.exists())
        self.assertTrue(keep.exists())
        self.assertTrue(foreign.exists())

    def test_outside_cleanup_is_refused(self):
        result = self.ps('Clear-LauncherTemporaryCache ' + literal(self.root) + ' ' + literal(self.root.parent))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.root.exists())

    def test_cache_junction_does_not_touch_target(self):
        cache = self.root/'.uv-launcher-cache'
        foreign = self.root/'foreign'
        foreign.mkdir()
        marker = foreign/'keep.txt'
        marker.write_text('keep')
        result = self.ps('New-Item -ItemType Junction -Path ' + literal(cache) + ' -Target ' + literal(foreign) + ' > $null\nClear-LauncherTemporaryCache ' + literal(self.root) + ' ' + literal(cache))
        try:
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(marker.read_text(), 'keep')
        finally:
            if cache.exists():
                os.rmdir(cache)  # Remove this fixture junction only, never its target.

    def prepare_project(self, *, existing=None):
        (self.root/'scripts').mkdir()
        for name in ['setup_launcher.ps1', 'launcher_process.ps1']:
            shutil.copyfile(ROOT/'scripts'/name, self.root/'scripts'/name)
        shutil.copyfile(ROOT/'run.bat', self.root/'run.bat')
        (self.root/'requirements.txt').write_text('# private fake fixture\n')
        (self.root/'main.py').write_text('# fake fixture only\n')
        bindir = self.root/'tools'
        bindir.mkdir()
        shutil.copyfile(self.fake_uv, bindir/'uv.exe')
        self.env['PATH'] = str(bindir) + os.pathsep + self.env['PATH']
        if existing:
            scripts = self.root/'venv/Scripts'
            scripts.mkdir(parents=True)
            (self.root/'venv/precious.txt').write_text('preserve me')
            if existing != 'missing':
                shutil.copyfile(self.fake_python, scripts/'python.exe')
                if existing == 'bad': (scripts/'version.txt').write_text('bad')

    def setup(self):
        return subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(self.root/'scripts/setup_launcher.ps1'), '-ProjectRoot', str(self.root)],
            env=self.env, capture_output=True, text=True, errors='replace', timeout=40)

    def test_missing_venv_created_before_install(self):
        self.prepare_project()
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        calls = (self.root/'uv.log').read_text(encoding='utf-8-sig').splitlines()
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].startswith('venv|'))
        self.assertTrue(calls[1].startswith('pip|install|--python|'))
        self.assertTrue(all('only-managed' in c for c in calls))
        for call in calls:
            self.assertTrue(Path(call.rsplit('|', 1)[1]).samefile(self.root/'.uv-launcher-cache'))

    def test_healthy_venv_kept(self):
        self.prepare_project(existing='good')
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(len((self.root/'uv.log').read_text(encoding='utf-8-sig').splitlines()), 1)
        self.assertEqual((self.root/'venv/precious.txt').read_text(), 'preserve me')
        self.assertFalse(list(self.root.glob('venv.repair-*')))

    def test_broken_venv_is_preserved(self):
        self.prepare_project(existing='bad')
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        backups = list(self.root.glob('venv.repair-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0]/'precious.txt').read_text(), 'preserve me')
        self.assertTrue((self.root/'venv/Scripts/python.exe').exists())

    def test_failed_repair_preserves_old_environment(self):
        self.prepare_project(existing='missing')
        self.env['PANOPTICON_TEST_UV_EXIT'] = '9'
        result = self.setup()
        self.assertEqual(result.returncode, 1)
        backups = list(self.root.glob('venv.repair-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0]/'precious.txt').read_text(), 'preserve me')
        self.assertEqual(len((self.root/'uv.log').read_text(encoding='utf-8-sig').splitlines()), 1)

    def test_install_failure_does_not_launch(self):
        self.prepare_project(existing='good')
        self.env['PANOPTICON_TEST_UV_EXIT'] = '8'
        result = self.run_batch()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(len((self.root/'uv.log').read_text(encoding='utf-8-sig').splitlines()), 3)
        self.assertFalse((self.root/'app.log').exists())

    def run_batch(self):
        return subprocess.run([str(Path(os.environ['SystemRoot'])/'System32/cmd.exe'), '/d', '/c', str(self.root/'run.bat')], cwd=ROOT,
            env=self.env, input='\n', capture_output=True, text=True, errors='replace', timeout=40)

    def test_batch_resolves_own_directory_and_special_path(self):
        self.prepare_project(existing='good')
        result = self.run_batch()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((self.root/'app.log').read_text(encoding='utf-8-sig'), str(self.root/'main.py'))
        for step in range(1, 5):
            self.assertIn(f'[{step}/4]', result.stdout)

    def test_batch_preserves_app_exit_code(self):
        self.prepare_project(existing='good')
        self.env['PANOPTICON_TEST_APP_EXIT'] = '6'
        result = self.run_batch()
        self.assertEqual(result.returncode, 6, result.stdout)
        self.assertIn('[CRASH]', result.stdout)

    def test_concurrent_setup_lock_rejects_before_uv(self):
        self.prepare_project(existing='good')
        result = self.ps("$lock=[IO.File]::Open(" + literal(self.root/'.launcher.lock') + ",[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)\ntry { & " + literal(PS) + " -NoProfile -ExecutionPolicy Bypass -File " + literal(self.root/'scripts/setup_launcher.ps1') + ' -ProjectRoot ' + literal(self.root) + '\nexit $LASTEXITCODE } finally { $lock.Dispose() }')
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertFalse((self.root/'uv.log').exists())


if __name__ == '__main__':
    unittest.main()
