@echo off
"%~dp0.venv\Scripts\python.exe" -X utf8 "%~dp0firefly_cli.py" %*
exit /b %errorlevel%
