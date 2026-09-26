@echo off
rem Firefly AI Pet - test entry (stable core test set)
rem Double-click to run; prefers local .venv, falls back to the sibling dev copy.
cd /d "%~dp0"

set "PYEXE="
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
if not defined PYEXE if exist "..\agent2026-firefly-agent\.venv\Scripts\python.exe" set "PYEXE=..\agent2026-firefly-agent\.venv\Scripts\python.exe"

if not defined PYEXE (
    echo [X] No virtual environment found. Create one first:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo Interpreter: %PYEXE%
echo === core test set ===
"%PYEXE%" -m pytest tests\test_hover_chrome.py tests\test_agent_dock_presence.py tests\test_agent_launcher.py tests\test_system_tray.py tests\test_custom_providers.py tests\test_ui_consolidation_p0.py tests\test_character_switch.py tests\test_tju_llm_health.py -q
set "RC=%ERRORLEVEL%"
echo.
echo === exit code: %RC% (0 = all passed) ===
pause
