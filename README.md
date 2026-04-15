# ⚠️ 本專案所有程式內容皆為 AI 產生。

# **UtaTen Lyric Finder 🎶**

UtaTen Lyric Finder 是一個自動化的歌詞獲取與同步工具。它能夠自動偵測您系統或 Spotify 當前正在播放的歌曲，從 [UtaTen](https://utaten.com/) 網站精準抓取日文歌詞，並結合 Whisper 語音辨識 (ASR) 技術，自動將歌詞與音軌對齊，生成帶有時間軸的動態歌詞檔 (.lrc)。

## **✨ 核心功能 (Features)**

- **自動偵測當前歌曲**：支援 Spotify API，或系統媒體來源：Windows 使用 SMTC、Linux (Ubuntu) 使用 MPRIS/playerctl (system_media.py)。
- **UtaTen 歌詞爬蟲**：自動搜尋並解析 UtaTen 網站的歌詞 (web_scraper.py)，特別適合獲取日文流行音樂 (J-Pop) 歌詞。
- **AI 語音辨識與對齊**：內建 Whisper 模型 (asr.py) 分析音檔，並透過文字匹配演算法 (matching.py) 自動校準時間軸，生成精準的 .lrc 動態歌詞。
- **圖形化介面 (GUI)**：提供友善的使用者介面 (gui_app.py & lyrics_browser.py)，方便瀏覽歌詞與操作設定。
- **智慧快取機制**：內建快取系統 (song_cache.py)，已處理過的歌曲無須重複下載與辨識，大幅提升效率。

## **📁 專案架構 (Project Structure)**

- gui_app.py：圖形化主程式進入點。
- auto_lyrics.py：自動化歌詞處理的核心邏輯與工作流程。
- asr.py：負責音訊處理與 Whisper 語音辨識。
- matching.py：負責將爬取的文本歌詞與 ASR 識別出的時間軸進行對齊與匹配。
- web_scraper.py：UtaTen 網站歌詞爬蟲模組。
- spotify_api.py / system_media.py：獲取當前播放媒體資訊的介面。
- lyrics_browser.py：用於在 GUI 中顯示與瀏覽歌詞的元件。
- song_cache.py：本地快取管理，負責儲存與讀取歷史紀錄。
- config.py：專案環境與參數設定檔。

## **🛠️ 安裝與環境設定 (Installation)**

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
   - **Ubuntu**:
     ```bash
     sudo apt update
     sudo apt install -y ffmpeg playerctl
     ```

4. **Linux 系統媒體來源說明 (Ubuntu)**
   - 若在 GUI 選擇 `System Media`，Ubuntu 會透過 MPRIS (`playerctl`) 讀取目前播放曲目。
   - 建議播放器需支援 MPRIS (例如 Spotify、Chrome/Chromium/Firefox 網頁播放器、VLC 等)。
   - 可用以下指令先確認系統是否有可讀取的媒體資訊：
     ```bash
     playerctl --all-players metadata --format "{{playerName}} | {{status}} | {{artist}} - {{title}}"
     ```

5. **環境變數與設定 (Configuration)**  
   若需要使用 Spotify API，請在 config.py 或 .env 檔案中填寫您的 SPOTIPY_CLIENT_ID 與 SPOTIPY_CLIENT_SECRET。

## **🚀 使用說明 (Usage)**

### **啟動圖形化介面 (GUI 模式)**

直接執行 gui_app.py 來開啟主程式介面：

```bash
python gui_app.py
```

### **啟動核心命令列腳本 (CLI 模式)**

若您想直接測試自動抓取與對齊流程，可以執行：

```bash
python auto_lyrics.py
```

## **📝 注意事項 (Disclaimer)**

- **版權聲明**：本工具從 UtaTen 獲取的歌詞僅供個人學習、研究或欣賞使用，請勿用於任何商業用途。歌詞版權歸原作者及 UtaTen 網站所有。
- **API 限制**：爬蟲若頻繁請求可能會被網站暫時封鎖，請合理設置請求間隔。
