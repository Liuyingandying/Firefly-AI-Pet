# stop_voice.ps1 - stop ONLY the Voice Service started by start_voice.ps1
# Never uses "taskkill /F /IM python.exe" style mass kills.
param([switch]$Force)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$PidFile = Join-Path $Root ".voice_service.pid"

function Step($msg) { Write-Host "[stop_voice] $msg" }

if (-not (Test-Path $PidFile)) {
    Step "no pid file ($PidFile) - this launcher did not start a service (or it was already stopped)"
    exit 0
}

$targetId = [int](Get-Content $PidFile)
$proc = Get-Process -Id $targetId -ErrorAction SilentlyContinue
if (-not $proc) {
    Step "PID $targetId no longer running - cleaning up pid file"
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    exit 0
}

# Safety: only kill a process that is actually python for this module
$name = $proc.ProcessName.ToLower()
if ($name -notmatch "^python") {
    Step "REFUSING: PID $targetId is '$($proc.ProcessName)', not python - pid file may be stale"
    Step "inspect manually, then delete $PidFile if confirmed"
    exit 1
}

# Double-check this PID really owns port 8300 (guards against PID reuse)
$conn = Get-NetTCPConnection -LocalPort 8300 -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.OwningProcess -eq $targetId }
$ownsPort = [bool]$conn
$healthy = $false
try { Invoke-RestMethod -Uri "http://127.0.0.1:8300/health" -TimeoutSec 3 | Out-Null; $healthy = $true } catch {}

if (-not $ownsPort -and -not $healthy) {
    Step "REFUSING: PID $targetId owns neither port 8300 nor a healthy service - pid file stale?"
    Step "inspect manually, then delete $PidFile if confirmed"
    exit 1
}

Step "stopping PID $targetId (owns 8300: $ownsPort, healthy: $healthy)"
if ($Force) {
    Stop-Process -Id $targetId -Force
} else {
    # graceful first: closeMainWindow is useless for hidden consoles, use taskkill (no /F)
    taskkill /PID $targetId | Out-Null
    # uvicorn exits on SIGTERM-equivalent; give it a moment
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline -and (Get-Process -Id $targetId -ErrorAction SilentlyContinue)) {
        Start-Sleep -Milliseconds 500
    }
    if (Get-Process -Id $targetId -ErrorAction SilentlyContinue) {
        Step "still alive after graceful stop - use -Force for hard kill"
        exit 2
    }
}
Remove-Item $PidFile -ErrorAction SilentlyContinue
Step "STOPPED (PID $targetId)"
exit 0
