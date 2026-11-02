@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title token-research snapshot UI
cd /d "%~dp0"

set "PORT=8020"
set "HOST=127.0.0.1"
set "PY="

echo ============================================
echo   token-research snapshot library
echo ============================================
echo.

REM ---- 1. Pick an interpreter that can actually build a FastAPI app ----
REM    Installing the packages is not enough: this machine's Python 3.12 has
REM    fastapi 0.104.1 paired with starlette 1.6.0, which raises TypeError on
REM    FastAPI() construction. So probe each candidate by constructing the app,
REM    not just by importing.
for %%I in ("D:\develop\PYTHON3.12\python.exe" "D:\develop\PYTHON3.7\python.exe") do (
    if not defined PY (
        if exist %%I (
            %%I -c "import fastapi,uvicorn;fastapi.FastAPI()" >nul 2>&1
            if not errorlevel 1 set "PY=%%~I"
        )
    )
)
if not defined PY (
    python -c "import fastapi,uvicorn;fastapi.FastAPI()" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    echo [X] No usable Python + fastapi found.
    echo.
    echo     Tried: D:\develop\PYTHON3.12, D:\develop\PYTHON3.7, python on PATH.
    echo     Required: Python 3.8+ with a matching fastapi/starlette pair.
    echo     Fix with:
    echo       python -m pip install -U "fastapi" "starlette" uvicorn
    pause
    exit /b 1
)
echo [OK] Interpreter: !PY!
"!PY!" --version

REM ---- 2. markdown is optional: report reader falls back to raw text ----
"!PY!" -c "import markdown" >nul 2>&1
if errorlevel 1 (
    echo [i] markdown not installed - reports will show as plain text.
    echo     Optional: "!PY!" -m pip install markdown
) else (
    echo [OK] markdown present - reports render as HTML
)

REM ---- 3. Sanity: app.py present ----
if not exist "server\app.py" (
    echo [X] server\app.py not found. Run this script from the token-research-ui folder.
    pause
    exit /b 1
)

echo.
echo [..] Starting API server ...
echo      Open http://%HOST%:%PORT%/ in your browser.
echo      Close this window to stop the server.
echo.

"!PY!" server\app.py --host %HOST% --port %PORT%
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [X] Server exited with code %RC%. See the messages above.
) else (
    echo [i] Server stopped.
)
pause
