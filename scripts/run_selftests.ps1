<#
.SYNOPSIS
  Corre los selftests de scripts/ con el entorno correcto y resume pasa/falla.

.DESCRIPTION
  - Usa venv\Scripts\python.exe (nunca un Python del sistema).
  - Fija TEMP/TMP a una ruta LARGA (.work\tmp): con el nombre corto 8.3 (VAJRAA~1)
    los selftests de Pickler fallan en falso (ver BITACORA, "Tests y edicion").
  - QT_QPA_PLATFORM=offscreen; un proceso por prueba, en serie (comparten guards/DB).
  - Timeout por prueba; al vencer mata el arbol completo y la marca TIMEOUT.
  - Log por prueba en logs\selftests\<fecha>\<nombre>.log.
  - Codigo de salida 1 si algo falla o vence.

.PARAMETER Filter
  Subcadena(s) del nombre (p. ej. pickler, cherry_queue). Sin valor: todas.

.PARAMETER IncludeToy
  Incluye tambien *_toytest.py (trabajan con carpetas de juguete, pero son mas lentos).

.PARAMETER TimeoutSec
  Limite por prueba. Por defecto 300.

.PARAMETER List
  Solo lista lo que correria.

.EXAMPLE
  .\scripts\run_selftests.ps1 -Filter pickler
  .\scripts\run_selftests.ps1 -List
#>
[CmdletBinding()]
param(
    [string[]]$Filter = @(),
    [switch]$IncludeToy,
    [int]$TimeoutSec = 300,
    [switch]$List
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "No existe $py; ejecuta run.bat para crear el venv." }

# Pruebas a correr
$patterns = @('*_selftest.py')
if ($IncludeToy) { $patterns += '*_toytest.py' }
$tests = foreach ($p in $patterns) { Get-ChildItem (Join-Path $root 'scripts') -Filter $p -File }
$tests = $tests | Sort-Object Name
if ($Filter.Count -gt 0) {
    $tests = $tests | Where-Object { $n = $_.Name; $Filter | Where-Object { $n -like "*$_*" } }
}
if (-not $tests) { Write-Host 'Ninguna prueba coincide.'; exit 0 }

if ($List) { $tests | ForEach-Object { $_.Name }; Write-Host "$(@($tests).Count) pruebas"; exit 0 }

# TEMP largo, dentro del proyecto
$tmp = Join-Path $root '.work\tmp'
New-Item -ItemType Directory -Force $tmp | Out-Null
$tmp = (Resolve-Path $tmp).ProviderPath

$logDir = Join-Path $root ('logs\selftests\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force $logDir | Out-Null

$results = @()
$i = 0
$total = @($tests).Count
foreach ($t in $tests) {
    $i++
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $py
    $psi.Arguments = '"' + $t.FullName + '"'
    $psi.WorkingDirectory = $root
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables['TEMP'] = $tmp
    $psi.EnvironmentVariables['TMP'] = $tmp
    $psi.EnvironmentVariables['QT_QPA_PLATFORM'] = 'offscreen'
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $psi.EnvironmentVariables['PYTHONPATH'] = $root

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $proc = [System.Diagnostics.Process]::Start($psi)
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    $status = 'OK'
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        $proc.Kill($true)
        $proc.WaitForExit()
        $status = 'TIMEOUT'
    } else {
        $proc.WaitForExit()   # vacia los flujos asincronos
        if ($proc.ExitCode -ne 0) { $status = 'FALLA' }
    }
    $sw.Stop()
    $log = Join-Path $logDir ($t.BaseName + '.log')
    $text = "exit=$($proc.ExitCode) status=$status`n--- stdout ---`n$($outTask.Result)`n--- stderr ---`n$($errTask.Result)"
    Set-Content -Path $log -Value $text -Encoding utf8

    $secs = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    $color = if ($status -eq 'OK') { 'Green' } else { 'Red' }
    Write-Host ("[{0}/{1}] {2,-8} {3,6}s  {4}" -f $i, $total, $status, $secs, $t.Name) -ForegroundColor $color
    $results += [pscustomobject]@{ Name = $t.Name; Status = $status; Secs = $secs; Log = $log }
}

$bad = @($results | Where-Object Status -ne 'OK')
Write-Host ''
Write-Host ("{0} pruebas: {1} OK, {2} con problemas. Logs: {3}" -f $total, ($total - $bad.Count), $bad.Count, $logDir)
if ($bad.Count -gt 0) {
    $bad | ForEach-Object { Write-Host ("  {0}  {1}  {2}" -f $_.Status, $_.Name, $_.Log) -ForegroundColor Red }
    exit 1
}
exit 0
