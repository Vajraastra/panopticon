@echo off
title Panopticon Debug Launcher
setlocal enabledelayedexpansion
color 0A

echo ---------------------------------------------------
echo     PANOPTICON DEBUG MODE
echo ---------------------------------------------------

set VENV_DIR=venv
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [ERROR] Venv not found. Run standard run.bat first to setup.
    pause
    exit /b
)

call "%VENV_DIR%\Scripts\activate.bat"

echo.
echo [LAUNCH] Starting Panopticon in verbose mode...
python main.py
echo.
echo [EXIT] Application finished.
pause
