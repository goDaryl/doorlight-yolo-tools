@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if defined DOORLIGHT_PYTHON (
    "%DOORLIGHT_PYTHON%" web\server.py %*
) else if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe web\server.py %*
) else (
    where py >nul 2>nul
    if errorlevel 1 (
        python web\server.py %*
    ) else (
        py -3 web\server.py %*
    )
)
if errorlevel 1 pause
