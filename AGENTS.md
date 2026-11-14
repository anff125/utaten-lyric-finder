# AGENTS.md — AI Coding Agent Guidelines for `utaten-lyric-finder`

This document provides context, architecture details, and mandatory verification rules for AI coding agents working on `utaten-lyric-finder`.

---

## 1. Project Overview & Architecture

`utaten-lyric-finder` is a Windows desktop application that detects the currently playing song (via Spotify Web API or Windows SMTC), scrapes Japanese lyrics with furigana/hiragana from [UtaTen](https://utaten.com/), opens an interactive Microsoft Edge window via Playwright, and uses `faster-whisper` ASR to automatically scroll and highlight the current lyric line in real time.

### Core Modules
- **`app.py`**: Unified application entry point. Configures `src/` on `sys.path`, supports `--cli` headless mode, and defaults to launching `utaten_finder.ui.legacy_gui`.
- **`src/utaten_finder/ui/legacy_gui.py`**: CustomTkinter GUI controller. Manages user settings, console log redirection, OAuth setup, and background worker startup.
- **`src/utaten_finder/core/`**:
  - `config.py`: Global configuration, path constants (`PROJECT_ROOT`, `DATA_DIR`, `ASSETS_DIR`, `BROWSER_PROFILE_DIR`), legacy data auto-migration from `.cache/` / `_cache/` / root `song_cache.json` to `data/`, and persistent settings (`data/runtime_settings.json`).
  - `events.py`: Thread-safe `EventBus` singleton (`bus`) for startup progress, track updates, lyric highlight changes, and status events.
  - `orchestrator.py`: Core orchestration loop (polls current track, looks up cache/scraper, triggers lyric display and ASR worker).
- **`src/utaten_finder/audio/`**:
  - `cuda_loader.py`: Dynamically registers pip-installed NVIDIA CUDA DLL directories (`nvidia/cublas/bin`, `nvidia/cudnn/bin`) into both `os.add_dll_directory` and `os.environ["PATH"]` before importing `faster_whisper` / `ctranslate2`, with `SetErrorMode` popup suppression and `WinError 577` preloading retries.
  - `asr_worker.py`: Audio capture loop + `faster-whisper` (`ctranslate2`) transcription pipeline with 1-second silent warm-up and automatic CPU (`compute_type="int8"`) fallback.
  - `process_loopback.py`: Windows 11 native WASAPI Process Loopback (`ActivateAudioInterfaceAsync` via `ctypes`/COM) to capture audio exclusively from `Spotify.exe` without system/game/Discord noise.
- **`src/utaten_finder/lyrics/`**:
  - `matching.py`: Japanese text normalization (`pykakasi` / `jaconv` to hiragana), `LyricsAgent` two-phase fuzzy matching (local lookahead + lost-position recovery), and `LyricSyncEngine` decoupled state machine.
  - `browser_sync.py`: Playwright (`channel="msedge"`) persistent browser controller using `data/browser_profile` and delegating lyric matching state to `LyricSyncEngine`.
  - `web_scraper.py`: Searches DuckDuckGo (`ddgs`) / UtaTen and parses lyric HTML (`BeautifulSoup`).
  - `song_cache.py`: Local JSON cache (`data/song_cache.json`) mapping track keys to UtaTen URLs and match quality status.
- **`src/utaten_finder/media/`**:
  - `spotify_api.py` & `system_media.py`: Track metadata providers for Spotify Web API (`spotipy`) + OAuth callback HTTP server (`wait_for_oauth_callback`) and Windows SMTC (`winrt`).
- **`assets/`, `data/`, `scripts/build_portable.ps1`, & `start.bat`**: Static assets (`assets/app.ico`), ignored runtime data (`data/`), portable Windows packaging script, and launcher batch script.

---

## 2. Environment & Tooling Notes (Windows)

- **Virtual Environment**: Local Python environment is located at `.venv\` (`.venv\Scripts\python.exe`, `.venv\Scripts\ruff.exe`).
- **Git Executable**: If `git` is not on the default PowerShell `PATH`, use `"$env:LOCALAPPDATA\Programs\Git\cmd\git.exe"` or `"C:\Program Files\Git\cmd\git.exe"`.
- **File Encodings & Line Endings**:
  - **`*.bat` files (`start.bat`, `update.bat`)**: MUST use Windows **`CRLF` (`\r\n`)** line endings and **UTF-8 without BOM** (or pure ASCII). `cmd.exe` misparses byte offsets if a batch file uses `LF` line endings or multi-byte characters without `chcp 65001 >nul`.
  - **`*.ps1` files (`scripts/build_portable.ps1`)**: Must include a **UTF-8 BOM** if it contains non-ASCII characters (such as Traditional Chinese comments), as required by Windows PowerShell 5.1 and `PSScriptAnalyzer`.
  - **`requirements.txt`**: Keep encoded in UTF-8.

---

## 3. Mandatory Pre-Commit & CI Checks

GitHub Actions CI (`.github/workflows/ci.yml`) runs on every push and pull request to `main` touching `**/*.py`, `requirements.txt`, `scripts/build_portable.ps1`, or `.github/workflows/ci.yml`.

**Before creating a commit**, agents MUST run and pass all applicable checks below:

### Check 1: Python Linter (`ruff`)
```powershell
.\.venv\Scripts\ruff.exe check .
```
- Must exit with `0` (`All checks passed!`).
- Watch out for common Ruff rules:
  - `F541`: Do not use `f"..."` prefix on strings without `{...}` placeholders.
  - `F401` / `F821`: Unused imports or undefined names (ensure `nonlocal` / `global` variables in nested functions like `_process_audio_chunk` in `src/utaten_finder/audio/asr_worker.py` are properly declared).

### Check 2: Runtime Smoke Test
```powershell
.\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'src'); import tkinter; import customtkinter; from utaten_finder.core import config, events, orchestrator; from utaten_finder.media import spotify_api, system_media; from utaten_finder.lyrics import web_scraper, matching, song_cache, browser_sync; from utaten_finder.audio import process_loopback, cuda_loader, asr_worker; from utaten_finder.ui import legacy_gui; print('[SUCCESS] runtime smoke imports loaded')"
```
- Ensure module-level imports do not crash when optional hardware or audio devices are absent.

### Check 3: PowerShell Script Analyzer (when editing `*.ps1`)
If `scripts/build_portable.ps1` is modified:
```powershell
Invoke-ScriptAnalyzer -Path ./scripts/build_portable.ps1 -EnableExit
```
- Also ensure `scripts/build_portable.ps1` retains its UTF-8 with BOM encoding and that any newly added `.py` module in `app.py` or `src/utaten_finder/` is covered by `$filesToCopy` / the `src/utaten_finder` recursive copy and the `files = [...]` list in `update.bat` inside `scripts/build_portable.ps1`.

---

## 4. Coding Conventions & Best Practices

1. **Maintain Resilient Fallbacks in `asr_worker.py`, `cuda_loader.py` & `process_loopback.py`**:
   - Never assume a CUDA GPU or `Spotify.exe` process is always present.
   - Keep `ctypes` DLL preloading (`cuda_loader.prepare_windows_cuda_dll_paths()`) intact before importing `faster_whisper` / `ctranslate2`.
   - When reassigning outer-scope variables inside nested worker functions (e.g., `model` fallback to CPU in `_process_audio_chunk`), always declare `nonlocal model`.
2. **Keep Portable Build in Sync**:
   - Whenever a new Python file or package module is added:
     1. Ensure it is copied in `scripts/build_portable.ps1` (either in `$filesToCopy` or under `src/utaten_finder/`)
     2. Update the `files = [...]` list inside the `update.bat` template in `scripts/build_portable.ps1`
     3. Update the runtime smoke test in `.github/workflows/ci.yml` (if applicable)
3. **Bilingual Documentation**:
   - User-facing workflow or feature changes should be reflected in both `README.md` (English) and `README.zh-TW.md` (Traditional Chinese).
4. **Auto-Commit After Completing Tasks**:
   - Whenever code, configuration, or documentation changes are completed and all mandatory pre-commit checks (Section 3) pass, **automatically stage and create a descriptive `git commit`** without waiting for the user to ask.
