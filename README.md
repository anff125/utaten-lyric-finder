# ⚠️ 本專案所有程式內容皆為 AI 產生。

# UtaTen Lyric Finder 🎶

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

## 🛠️ 安裝與環境設定 (Installation)

1. **複製專案 (Clone the repository)**

   ```bash
   git clone https://github.com/anff125/utaten-lyric-finder.git
   cd utaten-lyric-finder
   ```

2. **安裝 Python 依賴套件 (Install dependencies)**  
   請確保您的環境為 Python 3.8 或以上版本。

   ```bash
   pip install -r requirements.txt
   ```

3. **安裝 FFmpeg (Whisper 必備)**
   由於專案使用 Whisper 處理音訊，您的系統必須安裝 FFmpeg：

   - **Windows**:

     ```powershell
     winget install ffmpeg
     ```

4. **環境變數與設定 (Configuration)**  
   若需要使用 Spotify API，請在 config.py 或 .env 檔案中填寫您的 SPOTIPY_CLIENT_ID 與 SPOTIPY_CLIENT_SECRET。

### 使用說明 (Usage)

#### 啟動圖形化介面 (GUI 模式)

直接執行 `gui_app.py` 來開啟主程式介面：

```bash
python gui_app.py
```

#### 啟動核心命令列腳本 (CLI 模式)

若您想直接測試自動抓取流程，可以執行：

```bash
python auto_lyrics.py
```

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

## ✨ 核心功能 (Features)

- **自動偵測當前歌曲**：支援透過 Windows 系統媒體控制 (SMTC) (`system_media.py`) 或 Spotify API (`spotify_api.py`) 獲取正在播放的曲目資訊。
- **UtaTen 歌詞爬蟲**：自動搜尋並解析 UtaTen 網站的歌詞 (`web_scraper.py`)，特別適合獲取日文流行音樂 (J-Pop) 歌詞。
- **AI 語音辨識與自動捲動**：內建 Whisper 模型 (`asr.py`) 分析音檔，並搭配文字匹配演算法 (`matching.py`)，在 UtaTen 頁面自動捲動到目前對應的歌詞位置。
- **圖形化介面 (GUI)**：提供友善的使用者介面 (`gui_app.py` 與 `lyrics_browser.py`)，方便瀏覽歌詞與操作設定。
- **智慧快取機制**：內建快取系統 (`song_cache.py`)，已處理過的歌曲無須重複下載與辨識，提升處理效率。

## 📁 專案架構 (Project Structure)

- `gui_app.py`：圖形化主程式進入點。
- `auto_lyrics.py`：自動化歌詞處理的核心邏輯與工作流程。
- `asr.py`：負責音訊處理與 Whisper 語音辨識。
- `matching.py`：負責文本歌詞與辨識結果的匹配處理。
- `web_scraper.py`：UtaTen 網站歌詞爬蟲模組。
- `spotify_api.py` / `system_media.py`：獲取當前播放媒體資訊的介面。
- `lyrics_browser.py`：用於在 GUI 中顯示與瀏覽歌詞的元件。
- `song_cache.py`：本地快取管理，負責儲存與讀取歷史紀錄。
- `config.py`：專案環境與參數設定檔。

## 📝 注意事項 (Disclaimer)

- **版權聲明**：本工具從 UtaTen 獲取的歌詞僅供個人學習、研究或欣賞使用，請勿用於任何商業用途。歌詞版權歸原作者及 UtaTen 網站所有。
- **API 限制**：爬蟲若頻繁請求可能會被網站暫時封鎖，請合理設置請求間隔。
