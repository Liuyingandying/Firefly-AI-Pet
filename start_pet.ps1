$ErrorActionPreference = "Stop"

# Resolve the project directory from the script location, so this works
# regardless of the current working directory.
$ProjectDir = $PSScriptRoot
$PythonW = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"
$App = Join-Path $ProjectDir "app.py"

if (-not (Test-Path $PythonW)) {
    Write-Error "Virtual environment not found: $PythonW"
    exit 1
}

if (-not (Test-Path $App)) {
    Write-Error "app.py not found: $App"
    exit 1
}

# Launch detached: Start-Process returns immediately, and pythonw.exe runs
# with no console window. The pet keeps running after this shell exits.
# app.py enforces a single instance via a named QLocalServer, so running
# this script repeatedly still results in exactly one pet.
Start-Process -FilePath $PythonW -ArgumentList ('"{0}"' -f $App) -WorkingDirectory $ProjectDir

Write-Output "Firefly AI Pet started."
