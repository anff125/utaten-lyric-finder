# ⚠️ All source code in this project is AI-generated.

# UtaTen Lyric Finder 🎶

Language: [English](README.md) | [繁體中文](README.zh-TW.md)

UtaTen Lyric Finder is an automated lyric acquisition tool. It detects the song currently playing from your system media session or Spotify, fetches Japanese lyrics from [UtaTen](https://utaten.com/), and combines Whisper ASR with text matching to auto-scroll to the corresponding lyric position on the UtaTen page.

## 📦 Release and Portable Build

### Download the latest portable release

1. Go to [GitHub Releases](https://github.com/anff125/utaten-lyric-finder/releases) and download the latest `utaten-lyric-finder.zip`.
2. Extract the ZIP file to any folder (for example, Desktop).
3. Open the extracted folder and run `start.bat`.

### Portable package files

- `start.bat`: Launches the main GUI app and prepares required runtime settings.
- `update.bat`: Syncs core project files from the GitHub `main` branch for quick updates.
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

# 3. Upgrade pip and install all dependencies (including faster-whisper and NVIDIA CUDA runtime wheels)
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Browser & System Requirements

- **Microsoft Edge**: Playwright launches the system's built-in Microsoft Edge (`channel="msedge"`) to display the interactive lyric page. No extra `playwright install` download is required on Windows 10/11.
- **FFmpeg (Optional)**: `faster-whisper` uses bundled PyAV for decoding, though you can optionally install system FFmpeg via `winget install ffmpeg`.

### 4. Launch the Application (Usage)

Once `.venv` is installed, you can start the application in any of the following ways:

- **Option A: Run `start.bat` (Recommended)**
  ```powershell
  .\start.bat
  ```
  `start.bat` automatically configures `.venv` CUDA/cuDNN DLL paths and launches the GUI.

- **Option B: Launch the GUI directly via Python**
  ```powershell
  .\.venv\Scripts\python.exe gui_app.py
  ```

- **Option C: Launch the CLI workflow**
  ```powershell
  .\.venv\Scripts\python.exe auto_lyrics.py
  ```

### 5. First-Time Configuration (In-App GUI Settings)

All runtime settings can be configured directly inside the GUI (and are persisted to `.cache/runtime_settings.json`):
- **Audio Source (`spotify` / `system_media`)**:
  - `System Media (SMTC)`: Works out of the box without API keys using Windows System Media Transport Controls.
  - `Spotify`: Supports Windows 11 **Process Loopback** to capture audio exclusively from `Spotify.exe`, completely ignoring game, Discord, or browser audio. (For Spotify Web API track polling, enter your `Client ID` and `Client Secret` in the GUI or `config.py`).
- **ASR Model**: Choose between `turbo`, `kotoba-whisper-v2.0` (Japanese-specialized), `large-v3`, `medium`, `small`, or `base`. The model is downloaded automatically on first run.
- **Compute Device**: Defaults to `auto`, which registers pip-installed NVIDIA cuBLAS/cuDNN DLLs and performs a GPU warm-up pass; if CUDA is unavailable or fails at runtime, it **automatically falls back to CPU (`int8`) mode**.

### Build your own portable release package

Run this in the project root:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_portable.ps1
```

Common option example (reuse existing `python_env` cache for faster packaging):

```powershell
powershell -ExecutionPolicy Bypass -File .\build_portable.ps1 -SkipEnvBuild $true
```

After completion, the portable folder and ZIP file will be generated under `build\portable`.

## ✨ Features & Recent Updates

- **Automatic current-song detection**: Supports Windows SMTC via `system_media.py` and Spotify API via `spotify_api.py`.
- **Spotify Process Loopback**: Native Windows 11 WASAPI Process Loopback support (`process_loopback.py`) to capture audio exclusively from `Spotify.exe`, eliminating background game, Discord, and browser noise.
- **Automatic CUDA DLL Resolution & CPU Fallback**: Automatically loads pip-installed NVIDIA `cublas64_12.dll` / `cudnn` libraries with startup warm-up verification, and seamlessly falls back to CPU mode if GPU inference fails.
- **Enhanced Japanese ASR & Sliding-Window Matching**:
  - Supports `kotoba-whisper-v2.0-faster` and `turbo` with polyphase resampling (`scipy.signal.resample_poly`) and sliding-window capture.
  - Two-phase lyric matching (local lookahead + lost-position recovery) with Japanese hiragana normalization (`matching.py`) and manual jump buttons on every line.
- **UtaTen lyric scraping & smart caching**: Automatically searches and parses lyrics from UtaTen (`web_scraper.py`) and caches results in `song_cache.py` for instant loading on repeat plays.
- **GUI & Granular Debug Logging**: User-friendly interface (`gui_app.py` and `lyrics_browser.py`) with live model/source switching and separate toggles for Polling Debug and Recognition Debug logs.

## 📁 Project structure

- `gui_app.py`: Main entry point for GUI mode.
- `auto_lyrics.py`: Core lyric automation workflow.
- `asr.py`: Audio processing and Whisper ASR pipeline.
- `process_loopback.py`: Native Windows 11 WASAPI process-specific audio capture module.
- `matching.py`: Text matching logic between ASR output and lyric lines.
- `web_scraper.py`: UtaTen scraping module.
- `spotify_api.py` / `system_media.py`: Current media metadata providers.
- `lyrics_browser.py`: Lyric display/browser component used by the GUI.
- `song_cache.py`: Local cache manager for processed song records.
- `config.py`: Environment and runtime configuration.

## 📝 Disclaimer

- **Copyright**: Lyrics fetched from UtaTen are for personal learning/research/enjoyment only. Do not use for commercial purposes. Copyright belongs to the original rights holders and UtaTen.
- **Rate limits**: Very frequent scraping requests may trigger temporary blocking. Please use reasonable request intervals.
