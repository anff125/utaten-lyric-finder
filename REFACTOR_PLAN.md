# `utaten-lyric-finder` 整體重構與 PyWebView UI/UX 升級計畫

> **文件用途**：本文件為 `utaten-lyric-finder` 的完整重構與介面升級藍圖，採用 **方案 B：PyWebView（Python 後端 + 純淨內建歌詞視窗）**。每完成一個階段的項目，請更新對應的 `[ ]` 勾選狀態並提交至 Git，以便任何新開啟的對話能無縫接續開發。

---

## 1. 專案重構目標總覽 (Executive Summary)

### 目標一：解決根目錄混亂與全域狀態耦合
目前專案的 11 個 `.py` 模組、設定檔、快取資料與 Playwright 瀏覽器設定檔全部混放在根目錄，且存在多項技術債：
- **快取路徑不一致**：`config.py` 將執行期設定寫入 `.cache/runtime_settings.json`，`build_portable.ps1` 建立 `_cache`，而 `spotify_api.py` 同時相容 `.cache` 與 `_cache`；`song_cache.py` 則直接將 `song_cache.json` 寫在根目錄，導致專案目錄雜亂。
- **關注點未分離**：
  - `gui_app.py` 混雜了 CustomTkinter UI 繪製與 `_CallbackHTTPServer`（Spotify OAuth 本地 HTTP 監聽）。
  - `lyrics_browser.py` 同時承擔 Playwright 瀏覽器生命週期管理、DOM 解析、上百行內嵌 CSS/JS 字串、以及兩階段歌詞比對狀態機（`queue_scroll_when_next_line_matches`）。
  - `asr.py` 同時包含 Windows CUDA DLL 防護與預載、音訊錄製迴圈與 Whisper 推論邏輯。
- **重構方向**：建立 `src/utaten_finder/`、`data/`、`assets/`、`scripts/` 分層結構，根目錄僅保留啟動入口、依賴清單與說明文件，並導入執行緒安全的事件匯流排（Event Bus）解耦後端狀態推送。

### 目標二：採用【方案 B：PyWebView（Python 後端 + 純淨內建歌詞視窗）】升級 UI/UX
目前使用者必須同時面對 **三個視窗**：黑底 Console 終端機（看 Log）＋ CustomTkinter 控制視窗 ＋ Playwright 開啟的外部 Edge 視窗（含 UtaTen 廣告與非必要網頁版面）。

改用 **PyWebView（Windows 11 內建 Edge WebView2 核心）** 後，將整合成 **單一現代化深色桌面視窗**：
1. **直接解析並渲染純淨振假名歌詞**：透過 `requests` + `BeautifulSoup` 直接解析 UtaTen `.lyricBody .hiragana` 的振假名結構（將 `<span class="ruby"><span class="rb">漢字</span><span class="rt">かな</span></span>` 清洗為標準 `<ruby>漢字<rt>かな</rt></ruby>`），在內建 WebView2 畫面渲染**無廣告、字體與振假名大小可調、目前唱句發光高亮並平滑置中滾動**的歌詞舞台。
2. **雙向零輪詢橋接 (`js_api`)**：前端點擊任一句歌詞直接呼叫 `window.pywebview.api.jump_to_line(index)`，淘汰原本 Playwright 每 0.5 秒輪詢 `window.__pendingManualLyricJump` 的作法；後端直接主動推送曲目、歌詞、ASR 即時識別字幕、音量能量條與執行日誌。
3. **擺脫重型視窗依賴**：主流程不再需要為顯示歌詞而啟動整套 Playwright 瀏覽器與 `lyrics_browser_profile/`，大幅減少記憶體占用與啟動時間（仍可保留外部連結開啟按鈕作為 `not_found` 或手動開網頁的備援）。

---

## 2. 目標目錄結構藍圖 (Target Directory Layout)

### 2.1 重構後目錄樹
```text
utaten-lyric-finder/
├── app.py                              # 唯一啟動入口 (支援 --cli / --debug / --legacy-ui)
├── start.bat                           # 一鍵啟動腳本 (CRLF, UTF-8 without BOM)
├── requirements.txt                    # Python 依賴清單 (新增 pywebview)
├── AGENTS.md                           # AI Agent 開發與預檢規範
├── REFACTOR_PLAN.md                    # 本重構計畫文件
├── README.md / README.zh-TW.md         # 雙語說明文件
│
├── assets/                             # 靜態資源與 PyWebView 前端檔案
│   ├── app.ico                         # 應用程式圖示
│   └── web/                            # 純淨歌詞與控制介面前端
│       ├── index.html                  # 單頁式應用程式骨架
│       ├── styles.css                  # 深色毛玻璃主題與 <ruby> 振假名樣式
│       └── app.js                      # 狀態渲染、手動跳行與設定/日誌抽屜控制
│
├── data/                               # 所有執行期產物集中管理 (列入 .gitignore)
│   ├── runtime_settings.json           # 使用者設定 (原 .cache/runtime_settings.json)
│   ├── spotify_token_cache             # Spotify OAuth Token (原 .cache/spotify_token_cache)
│   ├── spotify_last_callback_url.txt   # 最後一次 OAuth Callback URL
│   └── song_cache.json                 # 歌曲配對快取 (原根目錄 song_cache.json)
│
├── scripts/                            # 建置、打包與維護腳本
│   └── build_portable.ps1              # 攜帶版打包腳本 (UTF-8 with BOM)
│
└── src/
    └── utaten_finder/
        ├── __init__.py
        ├── core/
        │   ├── __init__.py
        │   ├── config.py               # 集中設定管理、data/ 路徑與舊快取自動遷移
        │   ├── events.py               # Thread-safe Event Bus 與 Console 日誌擷取
        │   └── orchestrator.py         # 主監聽協調迴圈 (原 auto_lyrics.py)
        ├── media/
        │   ├── __init__.py
        │   ├── spotify_api.py          # Spotify API + OAuth Callback HTTP Server
        │   └── system_media.py         # Windows SMTC 媒體偵測
        ├── audio/
        │   ├── __init__.py
        │   ├── cuda_loader.py          # Windows CUDA DLL 預載、SetErrorMode 與 WinError 577 重試
        │   ├── process_loopback.py     # Windows 11 WASAPI Process Loopback (Spotify.exe)
        │   └── asr_worker.py           # 音訊擷取迴圈、RMS/Peak 能量計算與 Whisper 推論
        ├── lyrics/
        │   ├── __init__.py
        │   ├── web_scraper.py          # UtaTen / DuckDuckGo 搜尋與 HTML 擷取
        │   ├── parser.py               # UtaTen HTML -> 結構化歌詞行 + 乾淨 <ruby> HTML
        │   ├── matching.py             # 日文平假名正規化與兩階段模糊比對狀態機 (LyricSyncEngine)
        │   └── song_cache.py           # 歌曲快取讀寫 (指向 data/song_cache.json)
        └── ui/
            ├── __init__.py
            └── webview_app.py          # PyWebView 視窗控制器、JsApi 橋接與生命週期管理
```

### 2.2 舊檔案與新模組對照表 (Module Mapping)

| 現有檔案 | 目標位置 | 拆分與調整重點 |
| :--- | :--- | :--- |
| `config.py` | `src/utaten_finder/core/config.py` | 統一所有執行期路徑至 `data/`，並於啟動時自動遷移舊版 `.cache/` 與根目錄 `song_cache.json`。 |
| `auto_lyrics.py` | `src/utaten_finder/core/orchestrator.py` | 移除對 Playwright 的直接呼叫，改透過 `lyrics/parser.py` 載入歌詞並由 `events.py` 推送至 UI。 |
| `spotify_api.py` + `gui_app.py` (OAuth Server) | `src/utaten_finder/media/spotify_api.py` | 將 `_CallbackHTTPServer` 與 `_CallbackHandler` 從 GUI 抽離至此模組，統一管理 OAuth 授權流程。 |
| `system_media.py` | `src/utaten_finder/media/system_media.py` | 維持 Windows SMTC 偵測介面，僅調整 import 路徑。 |
| `asr.py` (CUDA DLL 部分) | `src/utaten_finder/audio/cuda_loader.py` | 獨立封裝 `_suppress_windows_loader_popups`、`_cuda_dll_load_priority` 與 `_preload_cuda_dll` (含 `WinError 577` 重試)。 |
| `asr.py` (擷取與辨識迴圈) | `src/utaten_finder/audio/asr_worker.py` | 加入音訊區塊音量計算（RMS/Peak）回報給 UI VU Meter，並將 `ASR_DEPS_ERROR` 等狀態送入 Event Bus。 |
| `process_loopback.py` | `src/utaten_finder/audio/process_loopback.py` | 維持原生 WASAPI Process Loopback COM 實作不變。 |
| `web_scraper.py` | `src/utaten_finder/lyrics/web_scraper.py` | 補充直接抓取歌詞頁面 HTML 的函式，供 `parser.py` 使用（含重試與編碼處理）。 |
| `matching.py` + `lyrics_browser.py` (比對狀態機) | `src/utaten_finder/lyrics/matching.py` | 抽離 `LyricsAgent` 與兩階段模糊比對 / 迷失恢復 / 重複片段 TTL 判斷為獨立 `LyricSyncEngine` 類別。 |
| `lyrics_browser.py` (DOM 解析與 JS/CSS) | `src/utaten_finder/lyrics/parser.py` + `assets/web/` | 以 `BeautifulSoup` 直接在後端解析 UtaTen HTML 產生每行資料；UI 樣式與互動抽離至 `assets/web/`。 |
| `song_cache.py` + `song_cache.json` | `src/utaten_finder/lyrics/song_cache.py` + `data/song_cache.json` | 改寫至 `data/song_cache.json`，首次執行自動搬移舊版快取避免重複搜尋。 |
| `gui_app.py` | `app.py` + `src/utaten_finder/ui/webview_app.py` | 以 PyWebView 單一視窗取代原本 CustomTkinter + 外部 Edge + 黑底 Console 三視窗操作模式。 |
| `build_portable.ps1` | `scripts/build_portable.ps1` | 改為打包 `app.py`、`src/`、`assets/`、`data/`，並更新 `update.bat` 下載邏輯與 `.github/workflows/ci.yml`。 |

---

## 3. PyWebView 單一視窗 UI/UX 設計規格

### 3.1 視窗版面配置 (Single-Window Layout)
整體採用深色護眼背景（類似現代音樂播放器歌詞模式），空間分配如下：

1. **頂部曲目與控制列 (Top Header Bar)**
   - **左側曲目資訊**：歌名、歌手、來源標籤（`Spotify` 綠標 / `System Media` 藍標）與配對狀態徽章（`✅ Perfect` / `⚠️ Partial` / `❌ Not Found` / `⏳ Checking`）。
   - **右側快捷工具列**：
     - `🔄 重新搜尋`（強制繞過快取重抓歌詞）
     - `🔗 修正網址`（僅在 `partial` / `not_found` 或使用者點擊時展開輸入列，支援貼上 UtaTen 網址立即重新解析與更新快取；另提供「在瀏覽器開啟」小按鈕作為備援）
     - `Aa` 排版快速調整 popover（歌詞字號、振假名大小、顯示/隱藏振假名、自動置中捲動開關）
     - `⚙️ 設定`（開啟右側滑出抽屜）
     - `📜 日誌`（開啟底部或右側滑出日誌抽屜）

2. **中央純淨歌詞主舞台 (Main Lyrics Stage — 佔視窗約 75%～80% 面積)**
   - 使用標準 HTML `<ruby>` 與 `<rt>` 標籤渲染振假名（確保漢字與假名垂直對齊、支援複製、無廣告與側邊欄干擾）。
   - **視覺層次**：
     - **當前演唱行 (`is-active`)**：字體放大、高對比發光高亮，並透過 `scrollIntoView({ behavior: 'smooth', block: 'center' })` 自動保持在畫面中央。
     - **已唱過行 (`is-past`)**：適度降低不透明度，閱讀不刺眼。
     - **未唱行 (`is-upcoming`)**：清晰可讀，讓使用者能預看下一段歌詞。
   - **一鍵手動定位**：每行左側顯示懸浮定位點（或直接點擊該行），立即呼叫 `window.pywebview.api.jump_to_line(lyricIndex)` 同步 Python `LyricSyncEngine` 並重置比對起點。
   - **空狀態與搜尋提示**：未播放歌曲時顯示等待動畫與音源提示；`not_found` 時顯示可點擊的「前往 UtaTen 搜尋」外部連結與網址貼上框。

3. **底部即時語音與音訊監控列 (Bottom ASR Dock)**
   - **主控制鈕**：`▶ 開始監聽` / `⏹ 停止監聽`（取代原本佔空間的 Start / Stop 按鈕）。
   - **即時音訊能量條 (Live Audio VU Meter)**：顯示目前從 `Spotify.exe`（Process Loopback）或系統混音收到的音量動態，讓使用者不用切到終端機就知道是否有收到聲音。
   - **ASR 即時辨識預覽**：單行顯示最新 Whisper 辨識出的日文字串、目前累積緩衝長度與最近一次比對相似度分數（例如 `🎤 「きみがすきだよ」 · 比對第 14 行 (0.88)`）。
   - **啟動與模型載入進度條**：直接整合於監控列上方細條，顯示 CUDA DLL 預載（含 Smart App Control 重試狀態）、Whisper 模型下載與暖身推論進度。

4. **滑出式抽屜面板 (Slide-over Drawers)**
   - **⚙️ 設定抽屜**：將初次設定後極少修改的項目收納於此，不再長期佔據主畫面：
     - Spotify `Client ID`、`Client Secret`、`Redirect URI`、`Start Spotify Auth` 一鍵授權與 Token 狀態指示燈。
     - 音訊來源模式（`Spotify` / `System Media`）與 `Spotify Process Loopback` 開關。
     - `啟用 ASR 自動捲動`、`ASR 模型選擇`（`turbo` / `kotoba-whisper-v2.0` / `large-v3` / `medium` / `small` / `base`）、`運算裝置`（`auto` / `cuda` / `cpu`）。
     - Debug 日誌開關（輪詢 Debug / 辨識 Debug）。
   - **📜 即時日誌抽屜**：統一收集系統標準輸出與事件紀錄，支援依級別篩選、一鍵複製與清空，使得日常啟動可直接使用 `pythonw.exe` 隱藏黑底終端機視窗。

### 3.2 Python ↔ PyWebView 通訊架構
- **Frontend -> Python (`JsApi` exposed via `webview.create_window(..., js_api=api)`)**：
  - `get_initial_state() -> dict`：前端 DOM 就緒後取得初始設定、Token 狀態與預設模型清單。
  - `start_listening()` / `stop_listening()`：啟停主協調執行緒。
  - `jump_to_line(lyric_index: int)`：手動定位歌詞索引並更新 `LyricSyncEngine`。
  - `research_lyrics()`：觸發強制重新搜尋。
  - `update_manual_url(url: str)`：驗證並儲存手動 UtaTen 網址，立即抓取與切換顯示。
  - `open_external_url(url: str)`：必要時用系統預設瀏覽器開啟頁面。
  - `save_settings(settings_dict: dict)`：即時持久化設定至 `data/runtime_settings.json`。
  - `start_spotify_auth()` / `exchange_callback_token(callback_url: str)`：啟動 OAuth 本地監聽並回傳授權結果。
- **Python -> Frontend (`window.evaluate_js(...)` via `core/events.py`)**：
  - 統一由 UI 排程器節流派發事件至前端 `window.__onBackendEvent(eventName, payload)`：
    - `startup_progress`：`{ fraction, message }`
    - `worker_state`：`{ running, audio_source_label }`
    - `track_updated`：`{ song_id, song_name, artist_name, source, cache_status, cache_url }`
    - `lyrics_loaded`：`{ title, artist, url, lines: [{ index, original, ruby_html }] }`
    - `lyric_highlighted`：`{ index, score, recognized_text, is_recovery }`
    - `audio_level`：`{ rms, peak, silent, source_name }`（約 10–15 FPS 節流推送）
    - `asr_preview`：`{ text, buffer_len, device_mode }`
    - `log_message`：`{ timestamp, level, message }`

### 3.3 UtaTen HTML 純淨振假名解析規則 (`lyrics/parser.py`)
為取代過去需開啟 Playwright 才抓得到 `<p>` 與 `<br>` 斷行的做法，後端解析器須支援 UtaTen 兩種常見 DOM 版型：
1. **取得歌詞根節點**：優先選取 `.lyricBody .hiragana`，若不存在則退回 `.lyricBody`。
2. **斷行正規化**：
   - 若節點內含 `<p>` 子段落，逐個 `<p>` 解析；若無 `<p>`（只有 `<br>` 換行的版型），依 `<br>` 切分為多行片段。
3. **振假名 `<ruby>` 轉換與安全清洗**：
   - 將 UtaTen 常見的 `<span class="ruby"><span class="rb">漢字</span><span class="rt">ふりがな</span></span>`（或原生 `<ruby>`）轉換為乾淨的 `<ruby>漢字<rt>ふりがな</rt></ruby>`。
   - 使用白名單過濾標籤（僅保留純文字、`<ruby>`、`<rb>`、`<rt>`、`<rp>`），徹底移除 `<script>`、`style`、廣告 `div`、`iframe` 與事件屬性，防止 XSS 或版面跑版。
4. **同步輸出比對欄位**：
   - `original`：移除 `<rt>` 後的純文字（比照現有 `extract_lyrics_from_html` 與 `p.evaluate` 邏輯）。
   - `normalized`：`normalize_match_text(normalize_to_hiragana(original))`。
   - 僅保留 `normalized` 非空的歌詞行，並重新編號 `index = 0, 1, 2, ...`，確保與前端顯示的每一行 1:1 對應。

---

## 4. 分階段實施計畫 (Phased Execution Plan)

> **開發規範提醒**：每個 Phase 完成後，必須依照 `AGENTS.md` 執行 `.\.venv\Scripts\ruff.exe check .`、Runtime Smoke Test（以及變更 `*.ps1` 時的 `Invoke-ScriptAnalyzer`），通過後自動提交 Git commit。

### Phase 1：目錄結構重構與執行期資料遷移（保持現有功能 100% 相容）
**目標**：先把雜亂的檔案整理乾淨、分離邏輯耦合，但暫時維持現有 CustomTkinter + Playwright 正常運作，確保每一步都可驗證。

- [x] **1.1 建立目標資料夾與套件骨架**
  - 建立 `src/utaten_finder/{core,media,audio,lyrics,ui}/__init__.py`、`assets/`、`data/`、`scripts/`。
  - 根目錄新增 `app.py` 作為統一入口；更新 `.gitignore` 將 `data/` 列入忽略並移除不再需要的例外規則。
- [x] **1.2 集中設定管理與舊版資料自動遷移 (`core/config.py`, `lyrics/song_cache.py`, `media/spotify_api.py`)**
  - 統一設定與快取目錄至 `data/`（徹底解決 `.cache` 與 `_cache` 分歧）。
  - 加入一次性遷移函式：若 `data/` 尚無對應檔案，自動從舊 `.cache/runtime_settings.json`、`.cache/spotify_token_cache`、`.cache/spotify_last_callback_url.txt` 與根目錄 `song_cache.json` 搬移或複製至 `data/`。
- [x] **1.3 模組拆遷與邏輯解耦**
  - 將 `asr.py` 拆為 `audio/cuda_loader.py`（保留 `SetErrorMode` 與 `WinError 577` 重試）與 `audio/asr_worker.py`。
  - 將 `process_loopback.py` 移至 `audio/process_loopback.py`；`system_media.py` 移至 `media/system_media.py`。
  - 將 `gui_app.py` 內的 `_CallbackHTTPServer` 移至 `media/spotify_api.py`。
  - 從 `lyrics_browser.py` 抽離 `LyricSyncEngine`（純資料狀態機，不依賴 Playwright）至 `lyrics/matching.py`。
  - 建立 `core/events.py` 統一處理啟動進度、狀態更新與日誌訂閱。
- [x] **1.4 同步更新建置與 CI 腳本**
  - 將 `build_portable.ps1` 移至 `scripts/build_portable.ps1`（維持 **UTF-8 with BOM** 與 `PSScriptAnalyzer` 零警告），更新檔案複製清單以支援 `src/`、`assets/`、`data/`。
  - 更新 `start.bat`（維持 **CRLF** 與 **UTF-8 without BOM**）呼叫 `app.py`。
  - 更新 `.github/workflows/ci.yml` 與 `AGENTS.md` 中的路徑與 Smoke Test 指令。
  - **Phase 1 驗收**：`ruff check .` 通過、Smoke Test 通過、Portable Dry-Run 通過、原有功能可正常啟動運作。

### Phase 2：無瀏覽器歌詞解析器與 PyWebView 單一視窗建置
**目標**：實作 UtaTen HTML 純淨振假名解析器與 PyWebView 前端介面，建立完整雙向橋接。

- [x] **2.1 新增依賴與環境確認**
  - 安裝並鎖定 `pywebview`（Windows 預設使用 Edge WebView2 `edgechromium` 後端）至 `requirements.txt`。
  - 確認 Windows 攜帶版環境（`scripts/build_portable.ps1`）能正常匯入 `webview`（`pythonnet` / `clr_loader` 或 `pywebview` 相依套件無缺漏）。
- [x] **2.2 實作後端歌詞抓取與 `<ruby>` 清洗解析器 (`lyrics/parser.py`, `lyrics/web_scraper.py`)**
  - 新增 `fetch_utaten_lyrics_html(url)`，支援請求標頭、逾時與日文編碼正確處理。
  - 實作 `parse_utaten_lyrics(html_text)`，解析 `<p>` 與 `<br>` 兩種 UtaTen 版型，產出乾淨白名單 `<ruby>` HTML、`original` 與 `normalized`。
  - 撰寫單元測試（或驗證腳本）覆蓋：含 `<p>` 歌詞、僅 `<br>` 歌詞、無振假名純假名/英文歌詞、含 `<script>` 或廣告節點的 HTML 清洗。
- [x] **2.3 實作 PyWebView 前端介面 (`assets/web/index.html`, `styles.css`, `app.js`)**
  - 實作頂部曲目與狀態列、中央 `<ruby>` 純淨歌詞主舞台、底部即時語音監控列。
  - 實作設定抽屜（Spotify 憑證/OAuth、音源選擇、ASR 模型與裝置、Debug 開關）與日誌抽屜。
  - 實作字體大小、振假名顯示開關、振假名尺寸等前端排版偏好（可存於 `localStorage` 或 `runtime_settings.json`）。
- [x] **2.4 實作 `ui/webview_app.py` 與 `JsApi` 雙向橋接**
  - 將 `core/events.py` 事件推送至 `window.evaluate_js(...)`，確保跨執行緒呼叫安全與閉窗清理（優雅關閉 `orchestrator` 與 ASR 執行緒）。
  - 實作前端呼叫 `jump_to_line`、`research_lyrics`、`update_manual_url`、`start_spotify_auth` 等 API。
  - **Phase 2 驗收**：執行 `python app.py` 可在單一 PyWebView 視窗內完成 Spotify 授權、搜尋 UtaTen、顯示純淨 `<ruby>` 歌詞並支援點擊任一行即時定位。

### Phase 3：整合即時音訊視覺化、淘汰舊版多視窗並完成打包驗證
**目標**：完成所有進階 UX 細節、清理過渡期的舊碼與 Playwright 重度依賴、更新文件與攜帶版建置。

- [x] **3.1 即時音訊能量與 ASR 辨識狀態推送 (`audio/asr_worker.py`)**
  - 於音訊滑動視窗計算 RMS / Peak 能量並透過 `events.py` 節流（約 10–15 Hz）推送至前端 VU Meter。
  - 將當前實際運作裝置（CUDA GPU 或降級後 CPU 模式）與辨識片段推送至底部狀態列顯示。
  - 將 `print` 與 Logger 輸出同時導向 `events.py` 供日誌抽屜顯示，無需開啟黑底終端機。
- [x] **3.2 切換預設啟動與移除冗餘多視窗程式碼**
  - `app.py` 預設啟動 PyWebView 主視窗（可保留 `--cli` 純終端機模式）。
  - 移除主流程對 Playwright 瀏覽器（`lyrics_browser.py`）與 CustomTkinter 的依賴；當無法自動匹配或使用者想看原網頁時，改用系統內建 `webbrowser.open(url)` 開啟。
  - 清理 `requirements.txt` 中不再需要的 `playwright`、`customtkinter`（確認是否保留或移至選用依賴，大幅縮小攜帶版體積）。
- [x] **3.3 啟動無黑窗優化與打包測試**
  - `start.bat` 改為支援預設無黑窗啟動（`pythonw.exe app.py`）或保留快速 Debug 參數（`start.bat --console`）。
  - 驗證 `scripts/build_portable.ps1` 完整建置與 `update.bat` 檔案更新清單（必須涵蓋 `src/` 巢狀目錄與 `assets/web/` 下所有檔案）。
  - 同步更新 `README.md`（英文）與 `README.zh-TW.md`（繁體中文）的功能介紹與截圖/操作說明。
  - **Phase 3 驗收**：
    - `.\.venv\Scripts\ruff.exe check .` 通過
    - `Invoke-ScriptAnalyzer -Path ./scripts/build_portable.ps1 -EnableExit` 通過
    - Runtime Smoke Test 與 `build_portable.ps1 -SkipEnvBuild` Dry-Run 通過
    - 實測播放歌曲：單一視窗顯示純淨振假名歌詞、VU Meter 跳動、ASR 自動高亮滾動與手動點擊跳行皆正常。

---

## 5. 新對話快速接續指南 (Quick Start Prompt)

日後開啟新對話要繼續執行本計畫時，可直接複製以下指令貼給 AI：

> 請先讀取 `REFACTOR_PLAN.md` 與 `AGENTS.md`，了解目前的架構規劃與預檢規範。  
> 我們現在要開始執行 **Phase 1**（或改為你想執行的 Phase 編號），請先確認目前工作區狀態與該階段的待辦項目，再提出實作計畫並逐步完成與驗證。
