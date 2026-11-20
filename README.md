# ⚠️ All source code in this project is AI-generated.

# UtaTen Lyric Finder 🎶

Language: [English](README.md) | [繁體中文](README.zh-TW.md)

UtaTen Lyric Finder is an automated Japanese lyric acquisition and live synchronization desktop app. It detects the song currently playing from Spotify or Windows System Media Transport Controls (SMTC), scrapes and sanitizes furigana (`<ruby>/<rt>`) lyrics from [UtaTen](https://utaten.com/) inside a clean single-window dark **PyWebView (Edge WebView2)** interface, and combines `faster-whisper` ASR with two-phase Japanese fuzzy matching to highlight and center-scroll the active lyric line in real time.

## 📦 Release and Portable Build

### Download the latest portable release

1. Go to [GitHub Releases](https://github.com/anff125/utaten-lyric-finder/releases) and download the latest `utaten-lyric-finder.zip`.
2. Extract the ZIP file to any folder (for example, Desktop).
3. Open the extracted folder and run `start.bat`.

### Portable package files

- `start.bat`: Launches the single-window PyWebView desktop app silently (`pythonw.exe`, no black console window). Supports `--console`, `--debug`, and `--cli` flags.
- `update.bat`: Syncs application source files and web UI assets from the GitHub `main` branch for quick updates.
- `python_env/`: Bundled portable Python + dependencies environment. You usually do not need to install Python separately.

### Portable package update flow

1. Close the running app.
2. Run `update.bat` in the portable package folder.
3. Run `start.bat` again after the update finishes.

## 🛠️ Installation from Source (Post-`git clone` Setup)

If you cloned the repository from source, follow the steps below to set up your environment and launch the app:

### 1. Clone the repository

```powershell
git clone https://github.com/anff125/utaten-lyric-finder.git
cd utaten-lyric-finder
```

### 2. Create a `.venv` virtual environment and install dependencies

> [!TIP]
> The bundled `start.bat` script automatically looks for Python and NVIDIA CUDA runtime DLLs inside a `.venv` folder in the project root. Naming your virtual environment `.venv` is strongly recommended.

Make sure you have **Python 3.10 or newer** installed, then run:

```powershell
# 1. Create a virtual environment named .venv
python -m venv .venv

# 2. Activate the virtual environment (PowerShell)
.\.venv\Scripts\Activate.ps1

# 3. Upgrade pip and install all dependencies (including PyWebView, faster-whisper, and NVIDIA CUDA runtime wheels)
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3. System Requirements

- **Microsoft Edge WebView2**: PyWebView uses the Edge WebView2 runtime built into Windows 10/11 to render the single-window lyric stage (no Playwright browser download or external browser automation needed).
- **FFmpeg (Optional)**: `faster-whisper` uses bundled PyAV for decoding, though you can optionally install system FFmpeg via `winget install ffmpeg`.

### 4. Launch the Application (Usage)

Once `.venv` is installed, you can start the application in any of the following ways:

- **Option A: Run `start.bat` (Recommended, Console-less GUI)**
  ```powershell
  .\start.bat
  ```
  By default, `start.bat` configures `.venv` CUDA/cuDNN DLL paths and launches `app.py` via `pythonw.exe` without keeping a console window open. All console and Python logger output is visible inside the in-app **📜 Logs** drawer.

- **Option B: Launch with Console / DevTools / Headless CLI Flags**
  ```powershell
  # Keep a console window attached for troubleshooting
  .\start.bat --console

  # Launch GUI with WebView2 DevTools enabled
  .\start.bat --debug

  # Launch headless CLI orchestrator (opens matched UtaTen URL in your default browser)
  .\start.bat --cli
  ```

- **Option C: Launch directly via Python**
  ```powershell
  # Single-window PyWebView desktop app
  .\.venv\Scripts\python.exe app.py

  # With DevTools enabled
  .\.venv\Scripts\python.exe app.py --debug

  # Headless CLI mode
  .\.venv\Scripts\python.exe app.py --cli
  ```

### 5. First-Time Configuration & Interface Guide

All runtime configuration and caches are stored centrally under `data/` (`data/runtime_settings.json`, `data/song_cache.json`, `data/spotify_token_cache`, `data/spotify_last_callback_url.txt`). Legacy files from `.cache/` or root `song_cache.json` are automatically migrated on startup.

- **Main Lyrics Stage**:
  - Clean ad-free `<ruby>/<rt>` furigana rendering supporting both `<p>` and `<br>` UtaTen page layouts.
  - Active singing line glows and automatically smooth-scrolls to the center (`🎯` recenter button appears if you scroll away).
  - Click any lyric line to jump and re-anchor ASR matching immediately (`jump_to_line`).
  - Use the **`Aa` typography popover** to customize lyric font size, furigana size, furigana visibility, and auto-scroll.
  - Use **🔄 Re-search** or **🔗 Fix URL** in the top bar (or open the original UtaTen page in your default browser when needed).
- **Bottom ASR & Audio Dock**:
  - Click **▶ Start Listening** / **⏹ Stop Listening** to control playback polling and speech recognition.
  - **Live Audio VU Meter** (~12.5 Hz) visualizes incoming RMS/peak energy and active capture source (`Spotify.exe` Process Loopback or system loopback).
  - Displays the active inference device badge (`CUDA GPU`, `CPU Mode`, or `CPU Fallback`), startup/model load progress bar, live recognized Japanese transcript preview, and match score badge.
- **Slide-over Drawers**:
  - **⚙️ Settings Drawer**: Configure Spotify `Client ID`, `Client Secret`, `Redirect URI`, one-click Spotify OAuth authorization, Audio Source (`Spotify API` / `System Media`), `Spotify Process Loopback`, ASR auto-scroll toggle, Whisper model (`turbo`, `kotoba-whisper-v2.0`, `large-v3`, `medium`, `small`, `base`), compute device (`auto` / `cuda` / `cpu`), Polling/Recognition debug toggles, and **Match Trace JSONL recording** (`data/traces/*.jsonl`).
  - **📜 Live Logs Drawer**: Captures `stdout`, `stderr`, and Python `logging` events with level filtering (`all`, `info`, `warn`, `error`, `debug`), auto-scroll, copy, and clear actions.

### Offline Match Evaluation & Strategy Comparison

Enable **Record match trace (`data/traces/*.jsonl`)** in **⚙️ Settings** while playing songs (any manual lyric clicks are also recorded as ground-truth anchors). You can then inspect timelines and compare matcher strategies/thresholds offline:

```powershell
# Inspect step-by-step ASR scores and manual jump anchors
.\.venv\Scripts\python.exe scripts\eval_matching.py data\traces\<trace>.jsonl --timeline

# Compare multiple strategies or thresholds on recorded traces
.\.venv\Scripts\python.exe scripts\eval_matching.py data\traces\ --strategy baseline --strategy baseline:threshold=0.70,recovery=0.55 --diff

# Compare baseline pykakasi normalization vs UtaTen <rt> furigana readings
.\.venv\Scripts\python.exe scripts\eval_matching.py data\traces\ --strategy baseline --strategy rt_reading --diff
```

### Build your own portable release package

Run this in the project root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_portable.ps1
```

Common option example (reuse existing `python_env` cache for faster packaging):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_portable.ps1 -SkipEnvBuild
```

After completion, the portable folder and ZIP file will be generated under `build\portable`.

## ✨ Features

- **Single-Window PyWebView Desktop UI**: Combines track controls, clean `<ruby>` furigana lyrics, live audio VU meter, settings drawer, and live log viewer in one lightweight dark window.
- **Automatic current-song detection**: Supports Windows SMTC (`system_media.py`) and Spotify Web API (`spotify_api.py`).
- **Spotify Process Loopback**: Native Windows 11 WASAPI Process Loopback (`process_loopback.py`) captures audio exclusively from `Spotify.exe`, eliminating game, Discord, and browser noise.
- **Automatic CUDA DLL Resolution & CPU Fallback**: Preloads pip-installed NVIDIA `cublas64_12.dll` / `cudnn` libraries (`cuda_loader.py`) with startup warm-up verification, and seamlessly falls back to CPU (`int8`) mode if GPU initialization or inference fails.
- **Enhanced Japanese ASR & Sliding-Window Matching**:
  - Supports `kotoba-whisper-v2.0-faster` and `turbo` with polyphase resampling (`scipy.signal.resample_poly`) and sliding-window capture (`asr_worker.py`).
  - Two-phase lyric matching (local lookahead + lost-position recovery) with Japanese hiragana normalization (`matching.py`) and click-to-jump sync.
- **Browserless UtaTen Scraper & Sanitized `<ruby>` Parser**: Fetches and parses UtaTen lyrics directly in Python (`web_scraper.py`, `parser.py`), prioritizing `<rt>` furigana readings (including ateji/gikun) when building normalized sync targets, and caches matches in `song_cache.py`.

## 📁 Project Structure

```text
utaten-lyric-finder/
├── app.py                              # Unified entry point (PyWebView GUI default, --cli, --debug)
├── start.bat                           # Console-less launcher (supports --console / --debug / --cli)
├── requirements.txt                    # Pinned Python dependencies (UTF-8)
├── assets/
│   ├── app.ico                         # Application icon
│   └── web/                            # PyWebView frontend (index.html, styles.css, app.js)
├── data/                               # Runtime settings, OAuth tokens, song cache, WebView profile
├── scripts/
│   ├── build_portable.ps1              # Windows portable ZIP build script
│   └── eval_matching.py                # Offline JSONL match trace replay & benchmark CLI
├── tests/                              # Unit test suite (parser, JsApi/EventBus, trace/eval, Phase 3)
└── src/utaten_finder/
    ├── core/                           # config.py, events.py (EventBus + logging capture), orchestrator.py
    ├── media/                          # spotify_api.py (API + OAuth server), system_media.py (SMTC)
    ├── audio/                          # cuda_loader.py, process_loopback.py, asr_worker.py (ASR + VU meter)
    ├── lyrics/                         # web_scraper.py, parser.py, matching.py, song_cache.py, trace.py
    └── ui/                             # webview_app.py (PyWebView controller & JsApi bridge)
```

## 📝 Disclaimer

- **Copyright**: Lyrics fetched from UtaTen are for personal learning/research/enjoyment only. Do not use for commercial purposes. Copyright belongs to the original rights holders and UtaTen.
- **Rate limits**: Very frequent scraping requests may trigger temporary blocking. Please use reasonable request intervals.
