$ErrorActionPreference = "Stop"

# Resolve the project directory from the script location, so this works
# regardless of the current working directory.
$ProjectDir = $PSScriptRoot
$PythonW = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"
$Supervisor = Join-Path $ProjectDir "tools\firefly_runtime_supervisor.py"

if (-not (Test-Path $PythonW)) {
    Write-Error "Virtual environment not found: $PythonW"
    exit 1
}

if (-not (Test-Path $Supervisor)) {
    Write-Error "Runtime supervisor not found: $Supervisor"
    exit 1
}

# Launch detached and windowless. The supervisor owns only the optional ESP32
# bridge it creates, starts the unchanged app.py entry point, and closes that
# bridge when the pet exits. app.py continues to enforce the pet single instance.
Start-Process -FilePath $PythonW -ArgumentList ('"{0}"' -f $Supervisor) -WorkingDirectory $ProjectDir

Write-Output "Firefly AI Pet started (ESP32 bridge supervised)."
