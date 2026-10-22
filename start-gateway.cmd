@echo off
rem wb-codex-gateway launcher
rem NOTE: keep this file ASCII-only - cmd.exe misparses UTF-8 Chinese comments.
setlocal
cd /d "%~dp0"

set "PY=%~dp0..\..\python\python.exe"
if not exist "%PY%" set "PY=python"

if not exist "%~dp0config.json" (
    if exist "%~dp0config.example.json" (
        echo [gateway] config.json not found, creating it from config.example.json
        copy /y "%~dp0config.example.json" "%~dp0config.json" >nul
    )
)

rem --- preflight: account scanning needs Node.js ---
where node >nul 2>nul
if errorlevel 1 (
    echo [gateway] WARNING: node.exe not found in PATH.
    echo [gateway]   The gateway itself runs, but "Scan local WorkBuddy accounts" needs Node.js.
    echo [gateway]   Install Node.js LTS, or set WB_NODE_PATH to node.exe, then restart.
    echo.
)

echo.
echo   WorkBuddy Gateway
echo   Console:  http://127.0.0.1:18080/
echo   Codex  :  http://127.0.0.1:18080/v1
echo   Close this window to stop the service.
echo.

"%PY%" gateway.py
if errorlevel 1 (
    echo.
    echo [gateway] failed to start, see the error above.
)
pause
