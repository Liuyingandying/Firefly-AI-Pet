$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "Python venv not found: $Python"
    exit 1
}

Write-Host "[1/4] Python syntax check..."
& $Python -m py_compile `
    (Join-Path $Root "app.py") `
    (Join-Path $Root "state_broker.py") `
    (Join-Path $Root "ui\companion_panel.py") `
    (Join-Path $Root "ui\process_launcher.py") `
    (Join-Path $Root "ui\quick_chat_protocol.py") `
    (Join-Path $Root "ui\workspace_store.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Push-Location $Root
try {
    Write-Host "[2/4] State broker regression..."
    & $Python "tools\test_state_broker.py"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[3/4] Phase 7B protocol/store tests..."
    & $Python "tools\test_phase7b_core.py"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[4/4] Required files..."
    $required = @(
        "ui\companion_panel.py",
        "ui\process_launcher.py",
        "ui\quick_chat_protocol.py",
        "ui\workspace_store.py",
        "tools\run_cli.ps1"
    )
    foreach ($rel in $required) {
        if (-not (Test-Path (Join-Path $Root $rel))) {
            Write-Error "Missing required file: $rel"
            exit 1
        }
    }
} finally {
    Pop-Location
}

Write-Host "Phase 7B verification passed."
