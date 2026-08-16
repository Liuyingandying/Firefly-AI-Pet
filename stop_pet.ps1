$ErrorActionPreference = "Stop"

$ProjectDir = $PSScriptRoot
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$StopScript = Join-Path $ProjectDir "tools\stop_pet.py"

if (-not (Test-Path $Python)) {
    Write-Error "Virtual environment not found: $Python"
    exit 1
}

if (-not (Test-Path $StopScript)) {
    Write-Error "stop_pet.py not found: $StopScript"
    exit 1
}

# Precise shutdown: stop_pet.py talks to the pet's control socket, so it
# never guesses PIDs or kills unrelated Python processes.
& $Python $StopScript
exit $LASTEXITCODE
