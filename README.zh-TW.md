# ⚠️ 本專案所有程式內容皆為 AI 產生。

# UtaTen Lyric Finder 🎶

語言切換: [English](README.md) | [繁體中文](README.zh-TW.md)

UtaTen Lyric Finder 是一個自動化的日文歌詞獲取與即時同步桌面應用程式。它會偵測 Spotify 或 Windows 系統媒體控制 (SMTC) 正在播放的歌曲，在純淨的單一深色 **PyWebView (Edge WebView2)** 視窗內抓取並渲染無廣告的 [UtaTen](https://utaten.com/) 振假名 (`<ruby>/<rt>`) 歌詞，並結合 `faster-whisper` 語音辨識 (ASR) 與兩階段日語模糊比對演算法，即時高亮並置中捲動至目前演唱的歌詞行。

## 📦 Release 與可攜版使用方式 (Release & Portable)

### 下載 Release 可攜版

1. 前往 [GitHub Releases](https://github.com/anff125/utaten-lyric-finder/releases) 頁面下載最新的 `utaten-lyric-finder.zip`。
2. 將 ZIP 解壓縮到任意資料夾（例如桌面）。
3. 進入解壓後的資料夾，直接執行 `start.bat`。

### 可攜版檔案說明

- `start.bat`：預設以無黑窗模式 (`pythonw.exe`) 啟動 PyWebView 單一主視窗；亦支援 `--console`、`--debug` 與 `--cli` 參數。
- `update.bat`：從 GitHub `main` 分支同步最新 Python 核心程式碼與前端網頁資源，快速更新功能修正。
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

# 3. 升級 pip 並安裝所有依賴套件 (含 PyWebView、faster-whisper 與 NVIDIA CUDA 執行期套件)
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3. 系統環境需求

- **Microsoft Edge WebView2**：本專案透過 PyWebView 使用 Windows 10/11 系統內建的 Edge WebView2 執行階段渲染歌詞視窗，不需額外安裝 Playwright 瀏覽器。
- **FFmpeg (選配)**：`faster-whisper` 已內建 PyAV 音訊解碼，但若系統環境需要處理額外音訊格式，可透過以下指令安裝：
  ```powershell
  winget install ffmpeg
  ```

### 4. 啟動應用程式 (Usage)

完成上述安裝後，您可以透過以下任一方式啟動：

- **方式 A：直接雙擊或執行 `start.bat`（推薦，預設無黑底終端機視窗）**
  ```powershell
  .\start.bat
  ```
  預設會自動設定 `.venv` 的 CUDA / cuDNN 路徑，並以 `pythonw.exe` 背景無窗啟動 `app.py`。所有主控台輸出與日誌皆可在視窗內的 **📜 日誌** 抽屜即時檢視。

- **方式 B：透過 `start.bat` 切換除錯或命令列模式**
  ```powershell
  # 保留終端機視窗方便查看原始 Console 輸出
  .\start.bat --console

  # 啟用 PyWebView DevTools 開發者工具
  .\start.bat --debug

  # 純命令列模式 (偵測到歌詞後會使用系統預設瀏覽器開啟)
  .\start.bat --cli
  ```

- **方式 C：直接透過 Python 啟動**
  ```powershell
  # 啟動單一 PyWebView 桌面視窗
  .\.venv\Scripts\python.exe app.py

  # 啟用 WebView2 DevTools
  .\.venv\Scripts\python.exe app.py --debug

  # 啟動純命令列 (CLI) 模式
  .\.venv\Scripts\python.exe app.py --cli
  ```

### 5. 初次使用設定與介面操作說明

所有執行期設定與快取均統一儲存於 `data/` 目錄（包含 `data/runtime_settings.json`、`data/song_cache.json`、`data/spotify_token_cache` 與 `data/spotify_last_callback_url.txt`）；若偵測到舊版 `.cache/` 或根目錄 `song_cache.json`，首次啟動時會自動遷移。

- **中央純淨歌詞舞台**：
  - 支援 UtaTen `<p>` 與 `<br>` 兩種版型，透過白名單清洗並渲染標準 `<ruby>/<rt>` 振假名，徹底去除網頁廣告與雜訊。
  - 目前演唱句會發光高亮並平滑置中捲動（若手動捲離目前行，右下角會顯示 `🎯 回到目前演唱行` 按鈕）。
  - 點擊任一行歌詞可立即手動定位同步起點並重設 ASR 比對位置。
  - 工具列提供 **`Aa` 排版設定**（調整歌詞字號、振假名大小、顯示/隱藏振假名、自動捲動開關）、**🔄 重新搜尋**、以及 **🔗 修正網址**（支援手動貼上 UtaTen 網址更新快取，或以系統預設瀏覽器開啟原網頁）。
- **底部 ASR 與即時音訊監控列**：
  - 點擊 **▶ 開始監聽** / **⏹ 停止監聽** 控制歌曲輪詢與語音辨識。
  - **即時音訊能量條 (Live Audio VU Meter)**：以約 12.5 Hz 節流顯示目前擷取的音訊 RMS/Peak 能量動態與來源裝置（`Spotify.exe` 獨立進程或系統混音）。
  - 顯示目前 ASR 運算裝置徽章（`CUDA GPU`、`CPU 模式` 或 `CPU 降級`）、模型載入/暖身進度條、最新辨識日文字串預覽與比對相似度分數。
- **滑出式抽屜面板**：
  - **⚙️ 設定抽屜**：設定 Spotify `Client ID`、`Client Secret`、`Redirect URI` 與一鍵 OAuth 授權、切換音源模式（`Spotify API` / `System Media`）、開關 `Spotify Process Loopback`、啟停 ASR 自動捲動、選擇 Whisper 模型（`turbo`、`kotoba-whisper-v2.0`、`large-v3`、`medium`、`small`、`base`）、運算裝置（`auto` / `cuda` / `cpu`）、Polling / Recognition Debug 日誌開關、**比對過程 Trace 記錄 (`data/traces/*.jsonl`)**，以及選配 **16kHz 音訊 WAV 錄製 (`data/recordings/*.wav`)**。
  - **📜 即時日誌抽屜**：統一收集 `stdout`、`stderr` 與 Python `logging` 紀錄，支援依等級篩選（全部 / 資訊 / 警告 / 錯誤 / 除錯）、自動捲動、一鍵複製與清空。

### 離線比對過程檢視與策略評測

在 **⚙️ 設定** 勾選 **「記錄比對過程 Trace (`data/traces/*.jsonl`)」** 後播放歌曲（途中任何手動點擊歌詞定位也會一併記錄為基準錨點；亦可勾選 **「錄製 16kHz 音訊 WAV (`data/recordings/*.wav`)」** 保存單聲道音檔供離線測試不同 Whisper 模型或音訊前處理），即可離線檢視每步分數或比較不同比對策略／門檻的效果：

```powershell
# 檢視單首歌曲的每步 ASR 識別、候選行分數與手動定位時間軸
.\.venv\Scripts\python.exe scripts\eval_matching.py data\traces\<檔名>.jsonl --timeline

# 針對錄製好的 Trace 並列比較不同參數或策略，並列出決策分歧點
.\.venv\Scripts\python.exe scripts\eval_matching.py data\traces\ --strategy baseline --strategy baseline:threshold=0.70,recovery=0.55 --diff

# 比較舊版 pykakasi、UtaTen <rt> 振假名、Issue #3 智慧同步、Issue #4 歌唱音韻／Beam 對齊、Issue #5 ASR 信心過濾，以及 Issue #6 Spotify 位置先驗與倒轉偵測策略
.\.venv\Scripts\python.exe scripts\eval_matching.py data\traces\ --strategy baseline --strategy rt_reading --strategy rt_smart_sync --strategy phono_sync --strategy phono_weighted --strategy pair_sync --strategy phono_pair_sync --strategy beam_align --strategy beam_asr_opt --strategy beam_pos_prior --diff
```

### 自行建立 Release 可攜包

在專案根目錄執行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_portable.ps1
```

常用參數範例（重用既有 `python_env` 快取，加速打包）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_portable.ps1 -SkipEnvBuild
```

完成後會在 `build\portable` 目錄看到可攜版資料夾與 `utaten-lyric-finder.zip`。

## ✨ 核心功能

- **PyWebView 單一深色桌面視窗**：將曲目列、純淨振假名歌詞舞台、即時音量 VU Meter、設定抽屜與即時日誌整合為單一現代化視窗。
- **自動偵測當前歌曲**：支援透過 Windows 系統媒體控制 (SMTC) (`system_media.py`) 或 Spotify Web API (`spotify_api.py`) 獲取正在播放的曲目資訊。
- **Spotify 獨立進程收音與雙聲道前處理**：在 Windows 11 上原生支援單獨錄製 `Spotify.exe` 音訊 (`process_loopback.py`)，透過雙聲道 `(L + R) / 2` Mid 聲道混音 (`downmix_to_mono`) 強化中央主唱人聲並選配 Butterworth 人聲帶通濾波 (`apply_vocal_bandpass`)，排除遊戲、Discord、瀏覽器等雜音干擾。
- **GPU 自動加速與智慧 CPU 降級**：自動掛載虛擬環境中的 NVIDIA CUDA (`cublas64_12.dll` / `cudnn`) 路徑並於啟動時進行暖身推理驗證；若 GPU 不可用或發生執行期錯誤，自動無縫切換為 CPU (`int8`) 模式不中斷辨識。
- **高精準度日文語音辨識與滑動視窗匹配**：
  - 支援 `kotoba-whisper-v2.0-faster` 與 `turbo` 等模型，使用多相濾波重採樣 (`scipy.signal.resample_poly`)、滑動視窗收音 (`asr_worker.py`)、接下來數行歌詞上下文提示 (`initial_prompt` 搭配 `condition_on_previous_text=False`)，以及 segment 信心度與間奏幻覺過濾（`avg_logprob`、`no_speech_prob`、`compression_ratio`、重複迴圈與詞庫感知語氣詞防護）。
  - 內建連續字元時間軸 Top-K Beam 追蹤與清濁音加權 Smith-Waterman 局部對齊 (`matching.py`、`phonology.py`)，整合日文歌唱音韻正規化（助詞、四假名、長音、促音）、未消耗尾端保留、短句誤觸防護、連續重複句智慧去重、自適應高亮時機與點擊歌詞任意行手動跳轉定位（亦支援 RapidFuzz 單行／相鄰跨行拼接模式）。
  - 當 Spotify 播放進度 (`progress_ms` / `duration_ms`) 可用時，會將進度百分比映射至歌詞字元時間軸作為軟性位置先驗（降低副歌重複段誤跳並保留前奏／間奏寬容區間），並在偵測到手動倒轉／快轉時自動重新錨定搜尋視窗（不影響 System Media 模式）。
- **無瀏覽器 UtaTen 爬蟲與 `<ruby>` 解析器**：直接由 Python 後端抓取並清洗 UtaTen HTML (`web_scraper.py`、`parser.py`)，優先採用 `<rt>` 振假名讀音（支援義訓／当て字）產生正規化比對目標，並透過本地快取 (`song_cache.py`) 秒開已聽過的歌曲。

## 📁 專案架構 (Project Structure)

```text
utaten-lyric-finder/
├── app.py                              # 統一啟動入口 (預設 PyWebView 視窗，支援 --cli / --debug)
├── start.bat                           # 預設無黑窗啟動腳本 (支援 --console / --debug / --cli)
├── requirements.txt                    # Python 依賴套件清單 (UTF-8)
├── assets/
│   ├── app.ico                         # 應用程式圖示
│   └── web/                            # PyWebView 前端介面 (index.html, styles.css, app.js)
├── data/                               # 執行期設定、Spotify Token、歌曲快取與 WebView 設定檔
├── scripts/
│   ├── build_portable.ps1              # Windows 可攜版 ZIP 打包腳本
│   └── eval_matching.py                # 離線 JSONL 比對 Trace 重播與評測腳本
├── tests/                              # 單元測試 (歌詞解析器、JsApi/EventBus、Trace/評測、Phase 3)
└── src/utaten_finder/
    ├── core/                           # config.py, events.py (EventBus + logging 擷取), orchestrator.py
    ├── media/                          # spotify_api.py (API + OAuth 伺服器), system_media.py (SMTC)
    ├── audio/                          # cuda_loader.py, process_loopback.py, asr_worker.py (ASR + VU Meter)
    ├── lyrics/                         # web_scraper.py, parser.py, phonology.py, matching.py, song_cache.py, trace.py
    └── ui/                             # webview_app.py (PyWebView 視窗控制器與 JsApi 橋接)
```

## 📝 注意事項 (Disclaimer)

- **版權聲明**：本工具從 UtaTen 獲取的歌詞僅供個人學習、研究或欣賞使用，請勿用於任何商業用途。歌詞版權歸原作者及 UtaTen 網站所有。
- **API 限制**：爬蟲若頻繁請求可能會被網站暫時封鎖，請合理設置請求間隔。
