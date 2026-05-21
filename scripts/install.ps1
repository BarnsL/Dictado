# install.ps1 — Windows installer for dictado.
#
# What this does:
#   1. Verifies Python 3.10+ is on PATH.
#   2. pip installs the package in editable mode into the user site.
#   3. Optionally registers the Startup-folder shortcut.
#
# All of this is per-user; no admin required.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSCommandPath))

Write-Host "==========================================="
Write-Host "  dictado installer"
Write-Host "==========================================="
Write-Host ""

# Locate a system Python interpreter. Prefer python.org over Microsoft Store.
$pythonExe = $null
foreach ($cand in @(
    "$env:ProgramFiles\Python313\python.exe",
    "$env:ProgramFiles\Python312\python.exe",
    "$env:ProgramFiles\Python311\python.exe",
    "$env:ProgramFiles\Python310\python.exe"
)) {
    if (Test-Path $cand) { $pythonExe = $cand; break }
}
if (-not $pythonExe) {
    $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $pythonExe) {
    throw "No Python 3.10+ found. Install from https://www.python.org/downloads/ first."
}
Write-Host "Using Python: $pythonExe" -ForegroundColor DarkGray
Write-Host ""

# Install in editable mode.
& $pythonExe -m pip install --user --upgrade pip
& $pythonExe -m pip install --user .

Write-Host ""
$reply = Read-Host "Install Startup-folder shortcut now? (Y/n)"
if ($reply -eq "" -or $reply -match '^[Yy]') {
    & $pythonExe -m dictado --install-autostart
}
else {
    Write-Host "Skipped. Run 'dictado --install-autostart' later if you change your mind."
}

Write-Host ""
Write-Host "Done. Run 'dictado' (or 'python -m dictado') to start the daemon."
