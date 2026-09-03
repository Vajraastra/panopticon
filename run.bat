@echo off
REM run.bat - launcher principal de Panopticon (nativo Windows, sin Git Bash).
REM Plataforma soportada y testeada. run.sh queda best-effort para Linux.
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PYTHON_VERSION=3.13"
set "VENV_DIR=%~dp0venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
REM No engancha el Python de cherry-dl (PEP 514): solo Pythons gestionados por uv.
set "UV_PYTHON_PREFERENCE=only-managed"

echo [1/3] Verificando uv...
where uv >nul 2>&1
if errorlevel 1 (
    echo   -^> Instalando uv...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

echo [2/3] Verificando entorno virtual...
REM uv venv descarga el Python gestionado si falta, sin crear shims en
REM ~/.local/bin (no toca el Python ajeno de cherry-dl). No alcanza con que
REM python.exe exista: el trampoline de uv puede apuntar a un Python managed
REM compartido que desaparecio entre sesiones. Se prueba que arranque de
REM verdad; si no, se borra el venv entero y se recrea.
set "VENV_OK=0"
if exist "%VENV_PY%" "%VENV_PY%" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>nul
if not errorlevel 1 if exist "%VENV_PY%" set "VENV_OK=1"
if "%VENV_OK%"=="0" (
    if exist "%VENV_DIR%" (
        echo   -^> venv ausente, danado o en otra version, recreando...
        rmdir /s /q "%VENV_DIR%"
    ) else (
        echo   -^> Creando venv con Python %PYTHON_VERSION%...
    )
    uv venv "%VENV_DIR%" --python %PYTHON_VERSION%
    if errorlevel 1 ( echo [ERROR] No se pudo crear el venv. & pause & exit /b 1 )
) else (
    echo   -^> venv OK
)

echo [3/3] Instalando dependencias...
uv pip install --python "%VENV_PY%" -r requirements.txt
if errorlevel 1 ( echo [ERROR] Fallo instalando dependencias. & pause & exit /b 1 )

echo [LAUNCH] Iniciando Panopticon...
"%VENV_PY%" main.py
if errorlevel 1 (
    echo.
    echo [CRASH] La aplicacion termino con error. Revisa el log de arriba.
    pause
)
endlocal
