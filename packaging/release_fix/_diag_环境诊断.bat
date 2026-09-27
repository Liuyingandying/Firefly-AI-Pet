@echo off
chcp 936 >nul
title 流萤 AI Pet - 环境诊断
cd /d "%~dp0"
echo ===== 系统信息 =====
ver
echo.
echo ===== 关键文件检查 =====
for %%F in ("_internal\PySide6\plugins\platforms\qwindows.dll" "_internal\PySide6\Qt6.dll" "_internal\vcruntime140.dll" "_internal\msvcp140.dll" "Firefly_AI_Pet.exe") do (
    if exist %%F (echo [有] %%~F) else (echo [缺] %%~F)
)
echo.
echo ===== VC++ 运行库 =====
if exist "%SystemRoot%\System32\msvcp140.dll" (echo [有] msvcp140.dll) else (echo [缺] msvcp140.dll)
if exist "%SystemRoot%\System32\vcruntime140_1.dll" (echo [有] vcruntime140_1.dll) else (echo [缺] vcruntime140_1.dll)
echo.
echo ===== 文件总数（应约 1100+） =====
dir /s /b _internal 2>nul | find /c /v ""
echo.
echo ===== Qt 插件调试启动（窗口若闪退请截图本窗口） =====
set QT_DEBUG_PLUGINS=1
Firefly_AI_Pet.exe 1>debug_out.log 2>debug_err.log
echo 已退出，调试详情见 debug_err.log
pause
