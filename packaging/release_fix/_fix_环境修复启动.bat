@echo off
chcp 936 >nul
title 流萤 AI Pet - 环境修复
cd /d "%~dp0"

echo ============================================
echo  流萤 AI Pet - 启动环境修复与诊断
echo ============================================
echo.

REM 1) 检测系统版本（Win7/8 无法运行，需 Win10 及以上）
ver | findstr /i "10\.0" >nul
if errorlevel 1 (
    echo [X] 检测到系统版本低于 Windows 10，本程序不支持该系统。
    echo     请升级到 Windows 10 或 Windows 11 后再试。
    pause
    exit /b 1
)
echo [OK] 系统版本: Windows 10/11

REM 2) 检查 VC++ 运行库关键 DLL（缺失则自动静默安装）
set MISSING=0
if not exist "%SystemRoot%\System32\msvcp140.dll" set MISSING=1
if not exist "%SystemRoot%\System32\vcruntime140.dll" set MISSING=1
if not exist "%SystemRoot%\System32\vcruntime140_1.dll" set MISSING=1
if "%MISSING%"=="1" (
    echo [!] 检测到缺少 VC++ 运行库，正在安装（约 20 秒，可能弹出授权窗口请点"是"）...
    if exist "vc_redist.x64.exe" (
        vc_redist.x64.exe /install /passive /norestart
        echo [OK] 安装完成。
    ) else (
        echo [X] 未找到 vc_redist.x64.exe，请手动下载安装:
        echo     https://aka.ms/vs/17/release/vc_redist.x64.exe
    )
) else (
    echo [OK] VC++ 运行库已存在
)
echo.

REM 3) 检查关键文件是否被杀毒软件删除
set QTBAD=0
if not exist "_internal\PySide6\plugins\platforms\qwindows.dll" set QTBAD=1
if not exist "_internal\PySide6\Qt6.dll" set QTBAD=1
if "%QTBAD%"=="1" (
    echo [X] 程序文件不完整（qwindows.dll/Qt6.dll 缺失）！
    echo     很可能是杀毒软件（360/腾讯管家）解压时删除了文件。
    echo     请: 1. 退出杀毒软件或添加信任目录
    echo        2. 重新解压压缩包（不要在压缩软件里直接双击运行）
    pause
    exit /b 1
)
echo [OK] 程序文件完整
echo.

REM 4) 启动（带调试输出，若仍失败会生成 debug.log 发回给开发者）
echo 正在启动流萤...
set QT_DEBUG_PLUGINS=1
Firefly_AI_Pet.exe 1>debug_out.log 2>debug_err.log
echo.
echo 如果程序正常打开了：可以删除本文件夹里的 debug_out.log/debug_err.log
echo 如果还是失败：请把 debug_err.log 发给开发者定位问题
pause
