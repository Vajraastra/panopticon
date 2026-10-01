# Windows PowerShell 5.1 and PowerShell 7. No downloads when dot-sourced.
Set-StrictMode -Version Latest

function Assert-LauncherPath {
    param([string]$Root, [string]$Path)
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $pathFull = [IO.Path]::GetFullPath($Path)
    if (-not $pathFull.StartsWith($rootFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Launcher path is outside the project: $pathFull"
    }
    $cursor = $pathFull
    while ($cursor.Length -ge $rootFull.Length) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Launcher refuses a linked path: $cursor"
            }
        }
        if ($cursor -eq $rootFull) { break }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    return $pathFull
}

function ConvertTo-LauncherArgument {
    param([AllowEmptyString()][string]$Value)
    # Windows argv quoting, including backslashes before quotes/trailing slashes.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Clear-LauncherTemporaryCache {
    param([string]$ProjectRoot, [string]$CachePath)
    $cacheFull = Assert-LauncherPath $ProjectRoot $CachePath
    if (-not (Test-Path -LiteralPath $cacheFull)) { return }
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($cacheFull)
    while ($pending.Count) {
        $folder = $pending.Pop()
        foreach ($child in Get-ChildItem -LiteralPath $folder -Force -Directory) {
            $checked = Assert-LauncherPath $ProjectRoot $child.FullName
            if ($child.Name.StartsWith('.tmp', [StringComparison]::Ordinal)) {
                # Verify every descendant before recursive removal; never follow links.
                $scan = [Collections.Generic.Stack[string]]::new()
                $scan.Push($checked)
                while ($scan.Count) {
                    foreach ($entry in Get-ChildItem -LiteralPath $scan.Pop() -Force) {
                        $null = Assert-LauncherPath $ProjectRoot $entry.FullName
                        if ($entry.PSIsContainer) { $scan.Push($entry.FullName) }
                    }
                }
                Remove-Item -LiteralPath $checked -Recurse -Force
            } else { $pending.Push($checked) }
        }
    }
}

function Stop-LauncherProcess {
    param([Diagnostics.Process]$Process)
    if ($Process.HasExited) { return }
    # Retain native handles before killing the parent. Kill(true) can miss children
    # under Windows job objects, and WaitForExit only confirms the parent exited.
    $children = [Collections.Generic.List[Diagnostics.Process]]::new()
    $pending = [Collections.Generic.Queue[int]]::new()
    $pending.Enqueue($Process.Id)
    try {
        while ($pending.Count) {
            $parentId = $pending.Dequeue()
            foreach ($entry in Get-CimInstance Win32_Process -Filter "ParentProcessId=$parentId") {
                try { $child = [Diagnostics.Process]::GetProcessById($entry.ProcessId) }
                catch [ArgumentException] { continue }
                try {
                    # Reject PID reuse between the CIM snapshot and handle opening.
                    $null = $child.Handle
                    if ($child.HasExited) { $child.Dispose(); continue }
                    $difference = ($child.StartTime.ToUniversalTime() - $entry.CreationDate.ToUniversalTime()).TotalMilliseconds
                    if ([Math]::Abs($difference) -gt 1) { $child.Dispose(); continue }
                    $children.Add($child)
                    $pending.Enqueue($child.Id)
                } catch { $child.Dispose(); throw }
            }
        }
        try { $Process.Kill($true) }
        catch [System.Management.Automation.MethodException] {
            # Windows PowerShell 5.1 uses .NET Framework without Kill(bool).
            & "$env:SystemRoot\System32\taskkill.exe" /PID $Process.Id /T /F | Out-Host
            if ($LASTEXITCODE -ne 0 -and -not $Process.HasExited) {
                throw "Could not stop launcher process tree $($Process.Id)"
            }
        }
        catch { if (-not $Process.HasExited) { throw } }
        foreach ($child in $children) {
            if (-not $child.HasExited) {
                try { $child.Kill() } catch { if (-not $child.HasExited) { throw } }
            }
        }
        if (-not $Process.WaitForExit(10000)) { throw 'Launcher process did not stop.' }
        foreach ($child in $children) {
            if (-not $child.WaitForExit(10000)) { throw 'Launcher child did not stop.' }
        }
    } finally {
        # A failed CIM/identity check must not leave the timed-out installer alive.
        try {
            if (-not $Process.HasExited) {
                & "$env:SystemRoot\System32\taskkill.exe" /PID $Process.Id /T /F | Out-Host
                if (-not $Process.HasExited) { $Process.Kill() }
                if (-not $Process.WaitForExit(10000)) { throw 'Launcher process cleanup failed.' }
            }
        } finally {
            foreach ($child in $children) {
                try {
                    if (-not $child.HasExited) { $child.Kill(); $null = $child.WaitForExit(10000) }
                } finally { $child.Dispose() }
            }
        }
    }
}

function Invoke-LauncherProcess {
    param([string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory,
          [ValidateRange(1, 3600000)][int]$TimeoutMilliseconds = 900000,
          [ValidateRange(1, 3)][int]$Attempts = 3, [string]$CachePath = '',
          [string]$Label = 'dependency installation')
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        if ($attempt -gt 1 -and $CachePath) {
            Clear-LauncherTemporaryCache $WorkingDirectory $CachePath
        }
        Write-Host "[launcher] $Label - attempt $attempt/$Attempts"
        $psi = [Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $Executable
        $psi.Arguments = ($Arguments | ForEach-Object { ConvertTo-LauncherArgument $_ }) -join ' '
        $psi.WorkingDirectory = $WorkingDirectory
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $proc = $null
        try {
            $proc = [Diagnostics.Process]::Start($psi)
            if (-not $proc.WaitForExit($TimeoutMilliseconds)) {
                Write-Host "[launcher] Timeout after $TimeoutMilliseconds ms; stopping process tree."
                Stop-LauncherProcess $proc
                $code = 1460
            } else {
                $proc.WaitForExit()
                $code = $proc.ExitCode
            }
        } finally { if ($null -ne $proc) { $proc.Dispose() } }
        if ($code -eq 0) { return 0 }
        Write-Host "[launcher] $Label failed with exit code $code."
    }
    return $code
}

function Repair-LauncherVenv {
    param([string]$ProjectRoot, [string]$UvExecutable)
    $venv = Assert-LauncherPath $ProjectRoot (Join-Path $ProjectRoot 'venv')
    $python = Join-Path $venv 'Scripts\python.exe'
    $valid = $false
    if (Test-Path -LiteralPath $python -PathType Leaf) {
        $null = Assert-LauncherPath $ProjectRoot $python
        $code = Invoke-LauncherProcess $python @('-c', 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)') $ProjectRoot -TimeoutMilliseconds 20000 -Attempts 1 -Label 'Python validation'
        $valid = $code -eq 0
    }
    if ($valid) { return $python }
    if (Test-Path -LiteralPath $venv) {
        $saved = Assert-LauncherPath $ProjectRoot ($venv + '.repair-' + [guid]::NewGuid().ToString('N'))
        Move-Item -LiteralPath $venv -Destination $saved
        Write-Host "[launcher] Previous environment preserved at $saved"
    }
    $code = Invoke-LauncherProcess $UvExecutable @('venv', $venv, '--python', '3.13') $ProjectRoot -Attempts 1 -Label 'venv creation'
    if ($code -ne 0) { throw "Could not create the venv (exit $code); previous environment remains preserved." }
    $null = Assert-LauncherPath $ProjectRoot $python
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'uv did not create Scripts\python.exe.' }
    $code = Invoke-LauncherProcess $python @('-c', 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)') $ProjectRoot -TimeoutMilliseconds 20000 -Attempts 1 -Label 'new Python validation'
    if ($code -ne 0) { throw 'New environment Python validation failed.' }
    return $python
}
