param(
    [string]$PythonVersion = "3.11.9",
    [string]$PythonArch = "amd64",
    [string]$CudaIndexUrl = "https://download.pytorch.org/whl/cu121",
    [string]$BuildRoot = (Join-Path $PSScriptRoot "build\portable"),
    [string]$PackageName = "utaten-lyric-finder",
    [bool]$SkipEnvBuild = $false  # 新增：控制是否跳過環境建置的參數
)

$ErrorActionPreference = "Stop"

function Write-Section {
    param([string]$Message)
    Write-Host ""
    Write-Host "=== $Message ==="
}

function Invoke-CheckedCommand {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $($LASTEXITCODE): $FilePath $($ArgumentList -join ' ')"
    }
}

function Copy-ProjectFile {
    param(
        [string]$SourcePath,
        [string]$DestinationPath
    )

    if (Test-Path $SourcePath) {
        Copy-Item -LiteralPath $SourcePath -Destination $DestinationPath -Force
    }
}

$repoRoot = $PSScriptRoot
$outputRoot = Join-Path $BuildRoot $PackageName
$pythonRoot = Join-Path $outputRoot "python_env"
$sitePackagesRoot = Join-Path $pythonRoot "Lib\site-packages"
$zipPath = Join-Path $BuildRoot "$PackageName.zip"

$pythonInstallerUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-$PythonArch.exe"

$pythonExecutable = Join-Path $pythonRoot "python.exe"
$tempDownloadRoot = Join-Path $env:TEMP "utaten_with_spotify_portable"

$pythonInstallerPath = Join-Path $tempDownloadRoot "python_installer.exe"
$getPipPath = Join-Path $tempDownloadRoot "get-pip.py"

# =========================================================================
# 核心修改：判斷是否需要完整建置 Python 環境
# =========================================================================
if (-Not $SkipEnvBuild -or -Not (Test-Path $pythonRoot)) {
    Write-Section "Preparing output folders for full build"
    if (Test-Path $outputRoot) {
        Remove-Item -LiteralPath $outputRoot -Recurse -Force
    }
    if (Test-Path $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    New-Item -ItemType Directory -Path $sitePackagesRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $tempDownloadRoot -Force | Out-Null

    Write-Section "Downloading official Python $PythonVersion (with tkinter)"
    Invoke-WebRequest -UseBasicParsing -Uri $pythonInstallerUrl -OutFile $pythonInstallerPath

    Write-Section "Installing Python portably to $pythonRoot"
    New-Item -ItemType Directory -Path $pythonRoot -Force | Out-Null
    $installArgs = "/quiet InstallAllUsers=0 TargetDir=`"$pythonRoot`" Include_launcher=0 Include_tcltk=1 Include_test=0 Include_pip=1 AssociateFiles=0 Shortcuts=0 Include_doc=0"
    Start-Process -FilePath $pythonInstallerPath -ArgumentList $installArgs -Wait

    Remove-Item -LiteralPath $pythonInstallerPath -Force

    $siteCustomizeContent = @'
import os
from pathlib import Path


def _add_dll_directories() -> None:
    site_packages = Path(__file__).resolve().parent
    candidates = []

    torch_lib = site_packages / "torch" / "lib"
    if torch_lib.is_dir():
        candidates.append(torch_lib)

    nvidia_root = site_packages / "nvidia"
    if nvidia_root.is_dir():
        for bin_dir in nvidia_root.rglob("bin"):
            if bin_dir.is_dir():
                candidates.append(bin_dir)

    for path in candidates:
        try:
            os.add_dll_directory(str(path))
        except Exception:
            pass


_add_dll_directories()
'@
    Set-Content -LiteralPath (Join-Path $sitePackagesRoot "sitecustomize.py") -Value $siteCustomizeContent -Encoding ASCII

    Write-Section "Bootstrapping pip"
    Invoke-WebRequest -UseBasicParsing -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPipPath
    Invoke-CheckedCommand -FilePath $pythonExecutable -ArgumentList @($getPipPath)

    $env:PATH = "$($pythonRoot)\Scripts;$($env:PATH)"

    Invoke-CheckedCommand -FilePath $pythonExecutable -ArgumentList @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")

    Write-Section "Installing project requirements"
    Invoke-CheckedCommand -FilePath $pythonExecutable -ArgumentList @(
        "-m",
        "pip",
        "install",
        "--prefer-binary",
        "--extra-index-url",
        $CudaIndexUrl,
        "-r",
        (Join-Path $repoRoot "requirements.txt")
    )
}
else {
    Write-Section "Using cached python_env, skipping environment build"
    # 確保舊的壓縮檔被刪除，以免發生衝突
    if (Test-Path $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
}

# =========================================================================
# 以下步驟無論是否命中快取，每次都要執行 (更新專案代碼與打包)
# =========================================================================

Write-Section "Preparing cache and profile folders"
New-Item -ItemType Directory -Path (Join-Path $outputRoot "_cache") -Force -ErrorAction SilentlyContinue | Out-Null
New-Item -ItemType Directory -Path (Join-Path $outputRoot "lyrics_browser_profile") -Force -ErrorAction SilentlyContinue | Out-Null

Write-Section "Copying application files"
$filesToCopy = @(
    "asr.py",
    "auto_lyrics.py",
    "config.py",
    "gui_app.py",
    "lyrics_browser.py",
    "matching.py",
    "song_cache.py",
    "spotify_api.py",
    "system_media.py",
    "web_scraper.py",
    "requirements.txt",
    "song_cache.json"
)

foreach ($relativePath in $filesToCopy) {
    Copy-ProjectFile -SourcePath (Join-Path $repoRoot $relativePath) -DestinationPath (Join-Path $outputRoot $relativePath)
}

if (Test-Path (Join-Path $repoRoot "_cache")) {
    Copy-Item -Path (Join-Path $repoRoot "_cache\*") -Destination (Join-Path $outputRoot "_cache") -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Section "Creating start.bat"
$startBatContent = @'
@echo off
setlocal
cd /d "%~dp0"
:: 設定 CUDA DLL 路徑
set "PYTHON_ENV_SITE=%~dp0python_env\Lib\site-packages"
set "PATH=%PYTHON_ENV_SITE%\nvidia\cublas\bin;%PYTHON_ENV_SITE%\nvidia\cudnn\bin;%PATH%"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
"%~dp0python_env\python.exe" "%~dp0gui_app.py"
if errorlevel 1 pause
'@
Set-Content -LiteralPath (Join-Path $outputRoot "start.bat") -Value $startBatContent -Encoding ASCII

Write-Section "Creating update.bat"
$updateBatContent = @'
@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo [DEBUG] 開始執行腳本...
echo [DEBUG] 目前目錄: "%CD%"

:: 1. 檢查 Python 執行檔是否存在
set "PY_PATH=%~dp0python_env\python.exe"
echo [DEBUG] 檢查路徑: "%PY_PATH%"

if not exist "%PY_PATH%" (
    echo.
    echo [ERROR] 找不到 Python 環境！
    echo 請確認資料夾 "python_env" 就在此批次檔旁邊。
    echo ---------------------------------------
    pause
    exit /b
)

echo [DEBUG] Python 環境確認成功，準備下載檔案...
echo.

:: 2. 建立多執行緒下載的暫存 Python 腳本 (避免縮排與特殊字元問題，採用前置重定向寫法)
set "TEMP_PY=%~dp0temp_update.py"

> "%TEMP_PY%" echo import urllib.request
>> "%TEMP_PY%" echo import concurrent.futures
>> "%TEMP_PY%" echo repo = 'https://raw.githubusercontent.com/anff125/utaten-lyric-finder/main/'
>> "%TEMP_PY%" echo files = ['asr.py', 'auto_lyrics.py', 'config.py', 'gui_app.py', 'lyrics_browser.py', 'matching.py', 'song_cache.py', 'spotify_api.py', 'system_media.py', 'web_scraper.py']
>> "%TEMP_PY%" echo def download(f):
>> "%TEMP_PY%" echo     try:
>> "%TEMP_PY%" echo         urllib.request.urlretrieve(repo+f, f)
>> "%TEMP_PY%" echo         print(f'[INFO] 更新 {f:20} ... 成功')
>> "%TEMP_PY%" echo     except Exception as e:
>> "%TEMP_PY%" echo         print(f'[ERROR] 更新 {f:20} ... 失敗: {e}')
>> "%TEMP_PY%" echo with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
>> "%TEMP_PY%" echo     executor.map(download, files)

:: 3. 執行並行下載
"%PY_PATH%" "%TEMP_PY%"
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Python 執行過程中發生錯誤。
)

:: 4. 清除暫存腳本
if exist "%TEMP_PY%" del "%TEMP_PY%"

echo.
echo ---------------------------------------
echo 執行結束。
echo ---------------------------------------
pause
'@
Set-Content -LiteralPath (Join-Path $outputRoot "update.bat") -Value $updateBatContent -Encoding UTF8

Write-Section "Creating ZIP archive using 7-Zip"
$7zArgs = @("a", "-tzip", "-mx=5", "-mmt", $zipPath, "$outputRoot\*")
Invoke-CheckedCommand -FilePath "7z.exe" -ArgumentList $7zArgs

Write-Host ""
Write-Host "Portable package created: $zipPath"
Write-Host "Unzip it, then run start.bat from the extracted folder."