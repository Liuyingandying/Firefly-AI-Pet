$ErrorActionPreference = "Stop"

# Create the Firefly AI Pet desktop shortcut. Thin wrapper over the shared
# Python installer so the launch definition stays in exactly one place.
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$Installer = Join-Path $ProjectDir "tools\install_desktop_shortcut.py"

if (-not (Test-Path $Python)) {
    Write-Error "Virtual environment not found: $Python"
    exit 1
}

& $Python $Installer
exit $LASTEXITCODE
