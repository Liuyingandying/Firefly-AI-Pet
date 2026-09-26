@echo off
rem Firefly AI Pet launcher for this folder (uses the local .venv).
cd /d "%~dp0"

set "PYWEXE="
if exist ".venv\Scripts\pythonw.exe" set "PYWEXE=.venv\Scripts\pythonw.exe"

if not defined PYWEXE (
    echo [X] No virtual environment found. Create one first:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

rem Single-instance: if a pet is already running, this launch exits silently.
start "" "%PYWEXE%" app.py
