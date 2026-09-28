# start_voice.ps1 - Firefly Voice Service launcher (migration copy)
# Usage:
#   powershell -ExecutionPolicy Bypass -File start_voice.ps1 [-PythonPath <path>] [-CudnnWorkaround]
#
# TEMPORARY_WORKAROUND: -CudnnWorkaround injects TORCH_CUDNN_V8_API_DISABLED=1
# into the service process only. It is NOT a permanent requirement and is NOT
# written to any persistent config. See docs/voice/MEMORY_REQUIREMENTS.md.
param(
    [string]$PythonPath = "",
    [switch]$CudnnWorkaround
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$PidFile = Join-Path $Root ".voice_service.pid"
$LogDir = Join-Path $Root "logs"
$LogFile = Join-Path $LogDir "voice_service.log"
$ErrFile = Join-Path $LogDir "voice_service.err.log"
$BaseUrl = "http://127.0.0.1:8300"

function Step($msg) { Write-Host "[start_voice] $msg" }

# ---- 1. Python ----
if (-not $PythonPath) {
    $candidates = @(
        (Join-Path $Root "venv\Scripts\python.exe"),
        "python"
    )
    foreach ($c in $candidates) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { $PythonPath = $c; break }
    }
}
if (-not $PythonPath) { Step "FAIL: no python found (use -PythonPath)"; exit 1 }
try {
    $ver = & $PythonPath --version 2>&1
} catch { Step "FAIL: cannot run $PythonPath"; exit 1 }
Step "python: $ver"
if ("$ver" -notmatch "3\.10\.") {
    Step "WARNING: golden environment is Python 3.10.21; continuing but this is unverified"
}

# ---- 2. Models ----
$requiredModels = @(
    "models\firefly\firefly-chinese.pth",
    "models\firefly\firefly-chinese_v2.index",
    "models\hubert_base.pt",
    "models\rmvpe.pt"
)
$missing = @($requiredModels | Where-Object { -not (Test-Path (Join-Path $Root $_)) })
if ($missing.Count -gt 0) {
    Step "FAIL: missing model files:"
    $missing | ForEach-Object { Step "  - $_" }
    Step "see models\README.md and models\manifest.json for acquisition"
    exit 1
}
Step "models: all 4 required files present"

# ---- 3. Dependencies ----
$depCheck = & $PythonPath -c "import torch, edge_tts, fastapi, uvicorn, sounddevice, soundfile, numpy, librosa, parselmouth, faiss" 2>&1
if ($LASTEXITCODE -ne 0) {
    Step "FAIL: dependency import error:"
    Step ($depCheck | Out-String)
    Step "run: pip install -r requirements.txt"
    exit 1
}
Step "deps: import check passed"

# ---- 4. Port 8300 ----
$existing = Get-NetTCPConnection -LocalPort 8300 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Step "port 8300 already listening (PID $($existing[0].OwningProcess)) - checking health"
    try {
        $h = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 5
        Step "ALREADY RUNNING: $($h | ConvertTo-Json -Compress)"
        exit 0
    } catch {
        Step "FAIL: port 8300 busy but /health failed - investigate PID above first"
        exit 1
    }
}

# ---- 5. Launch (Start-Process: OS-level log redirection, python itself is the PID) ----
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
if ($CudnnWorkaround) {
    # process-scoped env only; inherited by the child, never persisted
    $env:TORCH_CUDNN_V8_API_DISABLED = "1"
}
Step "launching: $PythonPath -m uvicorn api.server:app (cwd=$Root)"
$proc = Start-Process -FilePath $PythonPath `
    -ArgumentList "-m", "uvicorn", "api.server:app", "--host", "127.0.0.1", "--port", "8300" `
    -WorkingDirectory $Root -WindowStyle Hidden `
    -RedirectStandardOutput $LogFile -RedirectStandardError $ErrFile -PassThru
if ($CudnnWorkaround) { Remove-Item Env:TORCH_CUDNN_V8_API_DISABLED -ErrorAction SilentlyContinue }
Set-Content -Path $PidFile -Value $proc.Id -Encoding ascii
Step "started PID $($proc.Id), pid file: $PidFile"

# ---- 6. Wait for /health ----
$deadline = (Get-Date).AddSeconds(150)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    try {
        $h = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 3
        Step "READY: $($h | ConvertTo-Json -Compress)"
        Step "log: $LogFile / $ErrFile"
        Step "stop with: stop_voice.ps1"
        exit 0
    } catch {
        if ($proc.HasExited) {
            Step "FAIL: service process exited (code $($proc.ExitCode)). Last stderr:"
            Get-Content $ErrFile -Tail 15 -ErrorAction SilentlyContinue | ForEach-Object { Step "  $_" }
            Remove-Item $PidFile -ErrorAction SilentlyContinue
            exit 1
        }
    }
}
Step "FAIL: /health not ready within 150s. Check $ErrFile"
exit 1
