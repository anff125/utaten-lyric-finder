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

## 🛠️ Installation

1. **Clone the repository**

   ```bash
   git clone https://github.com/anff125/utaten-lyric-finder.git
   cd utaten-lyric-finder
   ```

2. **Install dependencies**
   Make sure you are using Python 3.8 or newer.

   ```bash
   pip install -r requirements.txt
   ```

3. **Install FFmpeg (required by Whisper)**
   Because this project uses Whisper for audio processing, FFmpeg is required:

   - **Windows**:

     ```powershell
     winget install ffmpeg
     ```

4. **Configuration**
   If you want Spotify API integration, set `SPOTIPY_CLIENT_ID` and `SPOTIPY_CLIENT_SECRET` in `config.py` or `.env`.

### Usage

#### Launch the GUI mode

Run `gui_app.py`:

```bash
python gui_app.py
```

#### Launch the CLI workflow

If you want to test the core automation flow directly, run:

```bash
python auto_lyrics.py
```

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

## ✨ Features

- **Automatic current-song detection**: Supports Windows SMTC via `system_media.py` and Spotify API via `spotify_api.py`.
- **UtaTen lyric scraping**: Automatically searches and parses lyrics from UtaTen using `web_scraper.py`, especially useful for J-Pop lyrics.
- **ASR-driven auto-scroll**: Uses Whisper in `asr.py` and text matching in `matching.py` to scroll to the current lyric line.
- **GUI application**: User-friendly interface implemented in `gui_app.py` and `lyrics_browser.py`.
- **Smart caching**: `song_cache.py` avoids repeated downloads and processing for previously handled songs.

## 📁 Project structure

- `gui_app.py`: Main entry point for GUI mode.
- `auto_lyrics.py`: Core lyric automation workflow.
- `asr.py`: Audio processing and Whisper ASR pipeline.
- `matching.py`: Text matching logic between ASR output and lyric lines.
- `web_scraper.py`: UtaTen scraping module.
- `spotify_api.py` / `system_media.py`: Current media metadata providers.
- `lyrics_browser.py`: Lyric display/browser component used by the GUI.
- `song_cache.py`: Local cache manager for processed song records.
- `config.py`: Environment and runtime configuration.

## 📝 Disclaimer

- **Copyright**: Lyrics fetched from UtaTen are for personal learning/research/enjoyment only. Do not use for commercial purposes. Copyright belongs to the original rights holders and UtaTen.
- **Rate limits**: Very frequent scraping requests may trigger temporary blocking. Please use reasonable request intervals.
