@echo off
setlocal enabledelayedexpansion

set "PROJECT_DIR=%~dp0"
set "VENV=%PROJECT_DIR%.venv-win"
set "PYTHON=%VENV%\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [setup] Creating Windows virtual environment...
    py -3.11 -m venv "%VENV%" 2>nul
    if errorlevel 1 (
        echo [setup] Python 3.11 not found, using default Python...
        py -m venv "%VENV%"
    )
    if errorlevel 1 goto VENV_FAIL
    echo [setup] Installing dependencies...
    "%PYTHON%" -m pip install --upgrade pip --quiet
    if exist "%PROJECT_DIR%requirements-win.txt" (
        "%PYTHON%" -m pip install -r "%PROJECT_DIR%requirements-win.txt"
    ) else (
        "%PYTHON%" -m pip install -r "%PROJECT_DIR%requirements.txt"
    )
    if errorlevel 1 goto PIP_FAIL
    echo [setup] Done. Run start.bat again to launch the algo.
    goto END
)

if "%DASH_PORT%"=="" set "DASH_PORT=8080"

netstat -ano 2>nul | findstr ":%DASH_PORT% " | findstr "LISTENING" > "%TEMP%\algohub_ports.tmp"
set "PORT_KILLED="
for /f "tokens=5" %%a in ('type "%TEMP%\algohub_ports.tmp" 2^>nul') do (
    echo Killing PID %%a on port %DASH_PORT%...
    taskkill /F /PID %%a >nul 2>&1
    set "PORT_KILLED=1"
)
del "%TEMP%\algohub_ports.tmp" 2>nul

if "!PORT_KILLED!"=="1" (
    echo Waiting 5s for Angel One WebSocket to release...
    timeout /t 5 /nobreak >nul
)

cd /d "%PROJECT_DIR%"
"%PYTHON%" main.py %*
goto END

:VENV_FAIL
echo ERROR: Could not create venv. Is Python installed?
goto END

:PIP_FAIL
echo ERROR: pip install failed. Check output above.

:END
