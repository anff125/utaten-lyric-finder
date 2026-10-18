# ⚠️ 本專案所有程式內容皆為 AI 產生。

# UtaTen Lyric Finder 🎶

語言切換: [English](README.md) | [繁體中文](README.zh-TW.md)

UtaTen Lyric Finder 是一個自動化的歌詞獲取工具。它能夠自動偵測您系統或 Spotify 當前正在播放的歌曲，從 [UtaTen](https://utaten.com/) 網站抓取日文歌詞，並結合 Whisper 語音辨識 (ASR) 與文字匹配流程，在 UtaTen 頁面自動捲動到對應歌詞位置。

## 📦 Release 與可攜版使用方式 (Release & Portable)

### 下載 Release 可攜版

1. 前往 [GitHub Releases](https://github.com/anff125/utaten-lyric-finder/releases) 頁面下載最新的 `utaten-lyric-finder.zip`。
2. 將 ZIP 解壓縮到任意資料夾（例如桌面）。
3. 進入解壓後的資料夾，使用 `start.bat` 啟動程式。

### 可攜版檔案說明

- `start.bat`：啟動主程式 GUI，並自動設定執行所需環境。
- `update.bat`：從 GitHub `main` 分支同步主要程式檔，快速更新功能修正。
- `python_env/`：可攜版內建 Python 與套件環境，通常不需要額外安裝 Python。

### 可攜版更新流程

1. 關閉正在執行的程式。
2. 在可攜版資料夾中執行 `update.bat`。
3. 更新完成後，重新執行 `start.bat`。

## 🛠️ 從原始碼安裝與執行 (`git clone` 後步驟)

如果您是透過 `git clone` 取得專案原始碼，請依照以下步驟建立環境並啟動：

### 1. 複製專案 (Clone the repository)

```powershell
git clone https://github.com/anff125/utaten-lyric-finder.git
cd utaten-lyric-finder
```

### 2. 建立虛擬環境並安裝依賴套件 (建議建立 `.venv`)

> [!TIP]
> 專案根目錄的 `start.bat` 預設會尋找 `.venv` 資料夾中的 Python 環境與 NVIDIA CUDA 動態連結庫 (DLL)。建議將虛擬環境命名為 `.venv`。

請確保系統已安裝 **Python 3.10 或以上版本**，並在專案根目錄執行：

```powershell
# 1. 建立名為 .venv 的虛擬環境
python -m venv .venv

# 2. 啟動虛擬環境 (PowerShell)
.\.venv\Scripts\Activate.ps1

# 3. 升級 pip 並安裝所有依賴套件 (含 faster-whisper 與 NVIDIA CUDA 執行期套件)
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3. 瀏覽器與系統需求檢查

- **Microsoft Edge**：本專案透過 Playwright 直接調用系統內建的 Microsoft Edge (`channel="msedge"`) 開啟歌詞頁面，Windows 10/11 預設已內建，無須額外執行 `playwright install`。
- **FFmpeg (選配)**：`faster-whisper` 已內建 PyAV 音訊解碼，但若系統環境需要處理額外音訊格式，可透過以下指令安裝：
  ```powershell
  winget install ffmpeg
  ```

### 4. 啟動應用程式 (Usage)

完成上述安裝後，您可以透過以下任一方式啟動：

- **方式 A：直接雙擊或執行 `start.bat`（推薦，需先建立 `.venv`）**
  ```powershell
  .\start.bat
  ```
  `start.bat` 會自動載入 `.venv` 內的 CUDA / cuDNN 路徑並啟動 GUI 介面。

- **方式 B：手動透過 Python 啟動 GUI 模式**
  ```powershell
  .\.venv\Scripts\python.exe gui_app.py
  ```

- **方式 C：啟動命令列腳本 (CLI 模式)**
  ```powershell
  .\.venv\Scripts\python.exe auto_lyrics.py
  ```

### 5. 初次使用設定 (GUI 內建設定)

啟動 `gui_app.py` 後，可直接在視窗介面中調整（設定會自動儲存於 `.cache/runtime_settings.json`）：
- **音源模式 (`Audio Source`)**：
  - `System Media (SMTC)`：免申請 API Key，直接讀取 Windows 系統媒體控制資訊。
  - `Spotify`：可搭配 Windows 11 **獨立進程收音 (Process Loopback)**，只錄製 `Spotify.exe` 的聲音，完全不受遊戲、Discord 或瀏覽器雜音影響（若需使用 Spotify API 偵測曲目，可在介面中填入 `Client ID` 與 `Client Secret`，或於 `config.py` 設定）。
- **語音辨識模型 (`ASR Model`)**：支援 `turbo`、`kotoba-whisper-v2.0`（日語強化）、`large-v3`、`medium`、`small`、`base` 等模型，首次啟動時會自動從 Hugging Face 下載。
- **運算裝置 (`Device`)**：預設 `auto` 會自動載入 pip 安裝的 NVIDIA cuBLAS / cuDNN DLL 並進行 GPU 暖身測試；若環境無 NVIDIA 顯卡或驅動不支援，會**自動無縫降級至 CPU (`int8`) 模式**運作。

### 自行建立 Release 可攜包

在專案根目錄執行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_portable.ps1
```

常用參數範例（重用既有 `python_env` 快取，加速打包）：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_portable.ps1 -SkipEnvBuild $true
```

完成後會在 `build\portable` 目錄看到可攜版資料夾與 ZIP 檔。

## ✨ 核心功能與近期更新 (Features & Recent Updates)

- **自動偵測當前歌曲**：支援透過 Windows 系統媒體控制 (SMTC) (`system_media.py`) 或 Spotify API (`spotify_api.py`) 獲取正在播放的曲目資訊。
- **Spotify 獨立進程收音 (Process Loopback)**：在 Windows 11 上原生支援單獨錄製 `Spotify.exe` 音訊 (`process_loopback.py`)，徹底排除遊戲、Discord、瀏覽器等其他系統雜音干擾。
- **GPU 自動加速與智慧 CPU 降級**：自動掛載虛擬環境中的 NVIDIA CUDA (`cublas64_12.dll` / `cudnn`) 路徑並於啟動時進行暖身推理驗證；若 GPU 不可用或發生執行期錯誤，自動切換為 CPU 模式不中斷辨識。
- **高精準度日文語音辨識與滑動視窗匹配**：
  - 支援 `kotoba-whisper-v2.0-faster` 與 `turbo` 等模型，使用多相濾波重採樣 (`scipy.signal.resample_poly`) 與滑動視窗收音。
  - 內建兩階段歌詞匹配（局部前瞻 + 迷失恢復機制）與日文平假名正規化 (`matching.py`)，並支援點擊歌詞旁按鈕手動跳轉定位。
- **UtaTen 歌詞爬蟲與智慧快取**：自動搜尋並解析 UtaTen 網站歌詞 (`web_scraper.py`)，並透過本地快取 (`song_cache.py`) 秒開已聽過的歌曲。
- **圖形化介面與細分除錯日誌**：提供友善的 GUI (`gui_app.py` / `lyrics_browser.py`)，支援即時切換音源、模型與獨立開關「輪詢日誌 (Polling Debug)」及「辨識日誌 (Recognition Debug)」。

## 📁 專案架構 (Project Structure)

- `gui_app.py`：圖形化主程式進入點。
- `auto_lyrics.py`：自動化歌詞處理的核心邏輯與工作流程。
- `asr.py`：負責音訊處理與 Whisper 語音辨識。
- `process_loopback.py`：Windows 11 原生 WASAPI 進程級音訊捕獲模組。
- `matching.py`：負責文本歌詞與辨識結果的匹配處理。
- `web_scraper.py`：UtaTen 網站歌詞爬蟲模組。
- `spotify_api.py` / `system_media.py`：獲取當前播放媒體資訊的介面。
- `lyrics_browser.py`：用於在 GUI 中顯示與瀏覽歌詞的元件。
- `song_cache.py`：本地快取管理，負責儲存與讀取歷史紀錄。
- `config.py`：專案環境與參數設定檔。

## 📝 注意事項 (Disclaimer)

- **版權聲明**：本工具從 UtaTen 獲取的歌詞僅供個人學習、研究或欣賞使用，請勿用於任何商業用途。歌詞版權歸原作者及 UtaTen 網站所有。
- **API 限制**：爬蟲若頻繁請求可能會被網站暫時封鎖，請合理設置請求間隔。
