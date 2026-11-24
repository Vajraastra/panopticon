<#
.SYNOPSIS
  Copia rotativa única de la documentación de trabajo que git no respalda.

.DESCRIPTION
  TASKS.md, BITACORA.md, HANDOFF.md y *_DESIGN.md están en .gitignore. Este script los copia
  a .work\session-notes-backup\ sobrescribiendo la copia anterior (una sola copia, sin cadena
  de respaldos). Correrlo ANTES de reescribirlos y al cerrar sesión.

  Salvaguarda: si un original quedó menos de la mitad de grande que su copia, no la
  sobrescribe (probable truncado accidental) salvo con -Force.

.EXAMPLE
  pwsh .\scripts\backup_docs.ps1
#>
[CmdletBinding()]
param([switch]$Force)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root '.work\session-notes-backup'
New-Item -ItemType Directory -Force $dest | Out-Null

$names = @('TASKS.md', 'BITACORA.md', 'HANDOFF.md') +
    @(Get-ChildItem $root -Filter '*_DESIGN.md' -File | ForEach-Object Name)

$copied = 0; $same = 0; $blocked = @()
foreach ($n in $names) {
    $src = Join-Path $root $n
    if (-not (Test-Path $src)) { continue }
    $dst = Join-Path $dest $n
    if (Test-Path $dst) {
        if ((Get-FileHash $src).Hash -eq (Get-FileHash $dst).Hash) { $same++; continue }
        $old = (Get-Item $dst).Length; $new = (Get-Item $src).Length
        if (-not $Force -and $old -gt 0 -and $new -lt ($old / 2)) {
            $blocked += "$n ($new B vs $old B en la copia)"; continue
        }
    }
    $tmp = "$dst.tmp"
    Copy-Item $src $tmp -Force
    Move-Item $tmp $dst -Force   # temp -> reemplazo, nunca escritura directa
    $copied++
    Write-Host "copiado  $n"
}
Write-Host ("{0} copiados, {1} sin cambios, {2} bloqueados. Destino: {3}" -f $copied, $same, $blocked.Count, $dest)
if ($blocked.Count -gt 0) {
    $blocked | ForEach-Object { Write-Host "BLOQUEADO (menos de la mitad del tamaño de la copia): $_" -ForegroundColor Red }
    Write-Host 'Revisa el original; usa -Force solo si el recorte es intencional.' -ForegroundColor Red
    exit 1
}
exit 0
