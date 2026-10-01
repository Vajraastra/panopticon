param([Parameter(Mandatory=$true)][string]$ProjectRoot)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\launcher_process.ps1"
$lock = $null
try {
    $root = [IO.Path]::GetFullPath($ProjectRoot)
    $lockPath = Assert-LauncherPath $root (Join-Path $root '.launcher.lock')
    $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $env:UV_PYTHON_PREFERENCE = 'only-managed'
    $uv = (Get-Command uv.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $cache = Assert-LauncherPath $root (Join-Path $root '.uv-launcher-cache')
    $null = New-Item -ItemType Directory -Path $cache -Force
    $env:UV_CACHE_DIR = $cache
    Write-Host '[2/4] Verificando entorno virtual...'
    $python = Repair-LauncherVenv $root $uv
    Write-Host '[3/4] Instalando dependencias...'
    $code = Invoke-LauncherProcess $uv @('pip', 'install', '--python', $python, '-r', (Join-Path $root 'requirements.txt')) $root -CachePath $cache
    if ($code -ne 0) { throw "Dependency installation failed after three attempts (exit $code)." }
} catch {
    Write-Host "[ERROR] $($_.Exception.Message)"
    exit 1
} finally { if ($null -ne $lock) { $lock.Dispose() } }
exit 0
