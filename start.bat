@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0src"
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
set "PYTHONW_EXE=%~dp0.venv\Scripts\pythonw.exe"
set "USE_CONSOLE=0"
set "APP_ARGS="

:parse_args
if "%~1"=="" goto after_args
if /i "%~1"=="--console" (
    set "USE_CONSOLE=1"
) else (
    if /i "%~1"=="--cli" set "USE_CONSOLE=1"
    if /i "%~1"=="-h" set "USE_CONSOLE=1"
    if /i "%~1"=="--help" set "USE_CONSOLE=1"
    set "APP_ARGS=%APP_ARGS% %1"
)
shift
goto parse_args

:after_args
:: Set CUDA DLL paths
set "PYTHON_ENV_SITE=%~dp0.venv\Lib\site-packages"
if exist "%PYTHON_ENV_SITE%\nvidia\cublas\bin" (
    set "PATH=%PYTHON_ENV_SITE%\nvidia\cublas\bin;%PYTHON_ENV_SITE%\nvidia\cudnn\bin;%PATH%"
)

if not "%USE_CONSOLE%"=="1" if exist "%PYTHONW_EXE%" (
    start "" "%PYTHONW_EXE%" "%~dp0app.py"%APP_ARGS%
    exit /b 0
)

title UtaTen Lyric Finder (Console Mode)
echo ========================================================
echo   Starting UtaTen Lyric Finder...
echo ========================================================
echo.

"%PYTHON_EXE%" "%~dp0app.py"%APP_ARGS%
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Application exited with error code %EXIT_CODE%.
    pause
    exit /b %EXIT_CODE%
)
