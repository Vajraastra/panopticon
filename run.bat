@echo off
REM Windows nativo; run.sh permanece best-effort.
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 ( echo [ERROR] No se pudo abrir la carpeta del proyecto. & pause & exit /b 1 )
set "UV_PYTHON_PREFERENCE=only-managed"

echo [1/4] Verificando uv...
where uv.exe >nul 2>&1
if errorlevel 1 (
    echo   -^> Instalando uv...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; irm https://astral.sh/uv/install.ps1 | iex"
    if errorlevel 1 ( echo [ERROR] No se pudo instalar uv. & pause & exit /b 1 )
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
where uv.exe >nul 2>&1
if errorlevel 1 ( echo [ERROR] uv.exe no esta disponible. & pause & exit /b 1 )

REM Timeout 15 min/intento, maximo 3; conserva el venv danado y usa cache propio.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_launcher.ps1" -ProjectRoot "%~dp0."
if errorlevel 1 ( echo [ERROR] Fallo preparando el entorno. Revisa el log. & pause & exit /b 1 )

echo [4/4] Iniciando Panopticon...
"%~dp0venv\Scripts\python.exe" "%~dp0main.py"
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" (
    echo [CRASH] La aplicacion termino con error. Revisa el log.
    pause
)
exit /b %APP_EXIT%
