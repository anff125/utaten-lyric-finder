@echo off
setlocal
cd /d "%~dp0"

title UtaTen Lyric Finder (Console Mode)
set "PYTHONUTF8=1"

:: 設定 CUDA DLL 路徑
set "PYTHON_ENV_SITE=%~dp0.venv\Lib\site-packages"
if exist "%PYTHON_ENV_SITE%\nvidia\cublas\bin" (
    set "PATH=%PYTHON_ENV_SITE%\nvidia\cublas\bin;%PYTHON_ENV_SITE%\nvidia\cudnn\bin;%PATH%"
)

echo ========================================================
echo   Starting UtaTen Lyric Finder...
echo ========================================================
echo.

".venv\Scripts\python.exe" "gui_app.py"

if errorlevel 1 (
    echo.
    echo [ERROR] Application exited with error code %ERRORLEVEL%.
    pause
)
