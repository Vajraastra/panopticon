@echo off
title Panopticon Launcher
echo ---------------------------------------------------
echo     PANOPTICON - Modular Image Organizer
echo ---------------------------------------------------

:: Check Python version and path
echo [DEBUG] Python Path:
where python
echo [DEBUG] Python Version:
python --version
echo [DEBUG] Pip Version:
python -m pip --version

if %errorlevel% neq 0 (
    echo [ERROR] Python is not found! Please install Python 3.10+.
    pause
    exit /b
)

:: Upgrade pip to ensure we can find latest wheels
echo.
echo [1/3] Checking/Upgrading pip...
python -m pip install --upgrade pip
python -m pip --version

:: Install dependencies
echo.
echo [2/3] Verifying dependencies...
python -m pip install -r requirements.txt --prefer-binary
if %errorlevel% neq 0 (
    echo [WARNING] Dependency installation had issues.
    echo Trying to continue anyway...
)

:: Run Application
echo.
echo [3/3] Starting Panopticon...
python main.py

pause
