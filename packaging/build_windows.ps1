# build_windows.ps1 - reproducible Windows build for Firefly AI Pet v1.0-rc1.
#
# Usage (from repo root, or from anywhere - paths are script-relative):
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# Optional switch:
#   -CleanBuild   removes build\ and dist\ before building (OFF by default;
#                 the default run only overwrites via pyinstaller --noconfirm)
#
# Outputs:
#   dist\Firefly_AI_Pet\Firefly_AI_Pet.exe (+ _internal\)
#   packaging\build_report.json
#
# This is the DRY BUILD entry point: no installer, no upload, no signing.

param([switch]$CleanBuild)
$ErrorActionPreference = 'Stop'

$RepoRoot   = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$DistDir    = Join-Path $RepoRoot 'dist\Firefly_AI_Pet_rc2'
$BuildDir   = Join-Path $RepoRoot 'build'
$ReportPath = Join-Path $PSScriptRoot 'build_report.json'

Write-Host '=== [1/5] Python environment ==='
if (-not (Test-Path $VenvPython)) { throw "venv python not found: $VenvPython" }
$PyVersion = (& $VenvPython -V) 2>&1 | Out-String
Write-Host ("python: " + $PyVersion.Trim())

Write-Host '=== [2/5] PyInstaller ==='
$PyiVersion = $null
try { $PyiVersion = (& $VenvPython -m PyInstaller --version 2>$null) | Out-String } catch {}
if ([string]::IsNullOrWhiteSpace($PyiVersion)) {
    Write-Host 'PyInstaller not found -> pip install pyinstaller>=6.10,<7'
    & $VenvPython -m pip install --disable-pip-version-check 'pyinstaller>=6.10,<7'
    if ($LASTEXITCODE -ne 0) { throw 'pip install pyinstaller failed. If pypi is unreachable, set HTTPS_PROXY for this call only.' }
    $PyiVersion = (& $VenvPython -m PyInstaller --version) | Out-String
}
Write-Host ('pyinstaller: ' + $PyiVersion.Trim())

Write-Host '=== [3/5] workspace ==='
if ($CleanBuild) {
    foreach ($d in @($BuildDir, (Join-Path $RepoRoot 'dist'))) {
        if (Test-Path $d) { Remove-Item -Recurse -Force $d }
    }
}

Write-Host '=== [4/5] pyinstaller packaging/firefly.spec ==='
$BuildStart = Get-Date
Push-Location $RepoRoot
try {
    & $VenvPython -m PyInstaller packaging\firefly.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed with exit code $LASTEXITCODE" }
}
finally { Pop-Location }
$DurationS = [math]::Round(((Get-Date) - $BuildStart).TotalSeconds, 1)

Write-Host '=== [5/5] post-build artifacts ==='
$ExePath   = Join-Path $DistDir 'Firefly_AI_Pet.exe'
$ExeExists = Test-Path $ExePath
$Internal  = Join-Path $DistDir '_internal'

# user-facing copies next to the exe: browser extension (staged from the repo
# source - it is a user artifact, never read from _internal at runtime) and
# the env template (bundled into _internal via spec datas)
$ExtCopied = $false
$ExtSrc = Join-Path $RepoRoot 'extensions\pagelens_bridge'
$ExtDst = Join-Path $DistDir 'extensions\pagelens_bridge'
if ((Test-Path $ExtSrc) -and -not (Test-Path $ExtDst)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $DistDir 'extensions') | Out-Null
    Copy-Item -Recurse $ExtSrc $ExtDst
    $ExtCopied = $true
}
$EnvCopied = $false
$EnvSrc = Join-Path $Internal '.env.example'
$EnvDst = Join-Path $DistDir '.env.example'
if ((Test-Path $EnvSrc) -and -not (Test-Path $EnvDst)) { Copy-Item $EnvSrc $EnvDst; $EnvCopied = $true }

$DistSizeMb = 0.0
if (Test-Path $DistDir) {
    $DistSizeMb = [math]::Round(((Get-ChildItem $DistDir -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB, 1)
}

# module inventory: read the PYZ archive TOC (pure-python modules); binaries
# (PySide6 .pyd etc.) live on disk in _internal and are not listed here.
# NOTE: PyInstaller's workpath is named after the SPEC file (build\firefly),
# not the exe name.
$ModuleCount  = $null
$ModuleSample = @()
$ModuleAll    = $null
$PyzPath = Join-Path $BuildDir 'firefly\PYZ-00.pyz'
if (Test-Path $PyzPath) {
    $py = @'
import json, sys
from PyInstaller.archive.readers import ZlibArchiveReader
names = sorted(ZlibArchiveReader(sys.argv[1]).toc.keys())
print(json.dumps({'count': len(names), 'modules': names}))
'@
    try {
        $jsonOut = ((& $VenvPython -c $py $PyzPath) -join '')
        $parsed  = $jsonOut | ConvertFrom-Json
        $ModuleCount = $parsed.count
        $ModuleAll   = @($parsed.modules)
        $ModuleSample = @($parsed.modules | Select-Object -First 40)
    } catch { Write-Host "module inventory parse failed: $_" }
}

$WarnCount = $null
$WarnFile  = Join-Path $BuildDir 'firefly\warn-firefly.txt'
if (Test-Path $WarnFile) {
    $WarnCount = (Get-Content $WarnFile | Where-Object { $_ -match 'missing module' }).Count
}

$report = [ordered]@{
    build_time                  = (Get-Date).ToString('o')
    duration_s                  = $DurationS
    python_version              = $PyVersion.Trim()
    pyinstaller_version         = $PyiVersion.Trim()
    exe                         = @{ path = $ExePath; exists = [bool]$ExeExists }
    dist_size_mb                = $DistSizeMb
    included_module_count       = $ModuleCount
    included_modules            = $ModuleAll
    included_modules_sample     = $ModuleSample
    warn_missing_module_count   = $WarnCount
    extensions_copied_to_root   = $ExtCopied
    env_example_copied_to_root  = $EnvCopied
    mode                        = 'dry-build'
}
$jsonReport = $report | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($ReportPath, $jsonReport, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "report: $ReportPath"
Write-Host "=== BUILD DONE (exe_exists=$ExeExists, size_mb=$DistSizeMb, modules=$ModuleCount) ==="
