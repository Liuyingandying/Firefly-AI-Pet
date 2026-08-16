$ErrorActionPreference = "Stop"

$ProjectDir = $PSScriptRoot
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Error "Virtual environment not found: $Python"
    exit 1
}

Push-Location $ProjectDir
try {
    Write-Host "[1/4] Syntax check..."
    & $Python -m py_compile `
        app.py `
        state_broker.py `
        ui\workspace_store.py `
        ui\process_launcher.py `
        ui\companion_panel.py `
        tools\simulate_event.py `
        tools\test_phase7a_core.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[2/4] State broker tests..."
    & $Python tools\test_state_broker.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[3/4] Phase 7A core tests..."
    $env:PYTHONPATH = $ProjectDir
    & $Python tools\test_phase7a_core.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[4/4] CLI discovery..."
    $codex = Get-Command codex -ErrorAction SilentlyContinue
    $claude = Get-Command claude -ErrorAction SilentlyContinue
    Write-Host ("Codex: " + $(if ($codex) { $codex.Source } else { "NOT FOUND" }))
    Write-Host ("Claude: " + $(if ($claude) { $claude.Source } else { "NOT FOUND" }))

    Write-Host "Phase 7A verification completed. GUI/launch behavior still requires Windows acceptance testing."
} finally {
    Pop-Location
}
