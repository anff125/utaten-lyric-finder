# AGENTS.md — AI Coding Agent Guidelines for `utaten-lyric-finder`

This document provides context, architecture details, and mandatory verification rules for AI coding agents working on `utaten-lyric-finder`.

---

## 1. Project Overview & Architecture

`utaten-lyric-finder` is a Windows desktop application that detects the currently playing song (via Spotify Web API or Windows SMTC), scrapes Japanese lyrics with furigana/hiragana from [UtaTen](https://utaten.com/), opens an interactive Microsoft Edge window via Playwright, and uses `faster-whisper` ASR to automatically scroll and highlight the current lyric line in real time.

### Core Modules
- **`gui_app.py`**: CustomTkinter GUI entry point. Manages user settings, console log redirection, and background worker startup.
- **`auto_lyrics.py`**: Core orchestration loop (polls current track, looks up cache/scraper, triggers Playwright lyric browser and ASR worker).
- **`asr.py`**: Audio capture loop + `faster-whisper` (`ctranslate2`) transcription pipeline.
  - Dynamically registers pip-installed NVIDIA CUDA DLL directories (`nvidia/cublas/bin`, `nvidia/cudnn/bin`) into both `os.add_dll_directory` and `os.environ["PATH"]` before importing `faster_whisper`.
  - Runs a 1-second silent warm-up transcription on startup and automatically falls back to CPU (`compute_type="int8"`) if CUDA/cuBLAS fails during initialization or runtime.
- **`process_loopback.py`**: Windows 11 native WASAPI Process Loopback (`ActivateAudioInterfaceAsync` via `ctypes`/COM) to capture audio exclusively from `Spotify.exe` without system/game/Discord noise.
- **`matching.py`**: Japanese text normalization (`pykakasi` / `jaconv` to hiragana) and `LyricsAgent` two-phase fuzzy matching (local lookahead + lost-position recovery).
- **`lyrics_browser.py`**: Playwright (`channel="msedge"`) persistent browser controller that injects CSS/JS for active line highlighting and manual lyric jump buttons.
- **`web_scraper.py`**: Searches DuckDuckGo (`ddgs`) / UtaTen and parses lyric HTML (`BeautifulSoup`).
- **`spotify_api.py` & `system_media.py`**: Track metadata providers for Spotify Web API (`spotipy`) and Windows SMTC (`winrt`).
- **`song_cache.py` & `song_cache.json`**: Local JSON cache mapping track keys to UtaTen URLs and match quality status.
- **`config.py`**: Global configuration and persistent runtime settings manager (`.cache/runtime_settings.json`).
- **`build_portable.ps1` & `start.bat`**: Portable Windows packaging script and launcher batch script.

---

## 2. Environment & Tooling Notes (Windows)

- **Virtual Environment**: Local Python environment is located at `.venv\` (`.venv\Scripts\python.exe`, `.venv\Scripts\ruff.exe`).
- **Git Executable**: If `git` is not on the default PowerShell `PATH`, use `"$env:LOCALAPPDATA\Programs\Git\cmd\git.exe"` or `"C:\Program Files\Git\cmd\git.exe"`.
- **File Encodings & Line Endings**:
  - **`*.bat` files (`start.bat`, `update.bat`)**: MUST use Windows **`CRLF` (`\r\n`)** line endings and **UTF-8 without BOM** (or pure ASCII). `cmd.exe` misparses byte offsets if a batch file uses `LF` line endings or multi-byte characters without `chcp 65001 >nul`.
  - **`*.ps1` files (`build_portable.ps1`)**: Must include a **UTF-8 BOM** if it contains non-ASCII characters (such as Traditional Chinese comments), as required by Windows PowerShell 5.1 and `PSScriptAnalyzer`.
  - **`requirements.txt`**: Keep encoded in UTF-8.

---

## 3. Mandatory Pre-Commit & CI Checks

GitHub Actions CI (`.github/workflows/ci.yml`) runs on every push and pull request to `main` touching `**/*.py`, `requirements.txt`, `build_portable.ps1`, or `.github/workflows/ci.yml`.

**Before creating a commit**, agents MUST run and pass all applicable checks below:

### Check 1: Python Linter (`ruff`)
```powershell
.\.venv\Scripts\ruff.exe check .
```
- Must exit with `0` (`All checks passed!`).
- Watch out for common Ruff rules:
  - `F541`: Do not use `f"..."` prefix on strings without `{...}` placeholders.
  - `F401` / `F821`: Unused imports or undefined names (ensure `nonlocal` / `global` variables in nested functions like `_process_audio_chunk` in `asr.py` are properly declared).

### Check 2: Runtime Smoke Test
```powershell
.\.venv\Scripts\python.exe -c "import tkinter; import customtkinter; import spotify_api; import web_scraper; import matching; import process_loopback; print('[SUCCESS] runtime smoke imports loaded')"
```
- Ensure module-level imports do not crash when optional hardware or audio devices are absent.

### Check 3: PowerShell Script Analyzer (when editing `*.ps1`)
If `build_portable.ps1` is modified:
```powershell
Invoke-ScriptAnalyzer -Path ./build_portable.ps1 -EnableExit
```
- Also ensure `build_portable.ps1` retains its UTF-8 with BOM encoding and that any newly added `.py` module in the repo root is added to `$filesToCopy` and `update.bat` inside `build_portable.ps1`.

---

## 4. Coding Conventions & Best Practices

1. **Maintain Resilient Fallbacks in `asr.py` & `process_loopback.py`**:
   - Never assume a CUDA GPU or `Spotify.exe` process is always present.
   - Keep `ctypes` DLL preloading (`_prepare_windows_cuda_dll_paths()`) intact before importing `faster_whisper` / `ctranslate2`.
   - When reassigning outer-scope variables inside nested worker functions (e.g., `model` fallback to CPU in `_process_audio_chunk`), always declare `nonlocal model`.
2. **Keep Portable Build in Sync**:
   - Whenever a new Python file is added to the repository root, update:
     1. `$filesToCopy` in `build_portable.ps1`
     2. The `files = [...]` list inside the `update.bat` template in `build_portable.ps1`
     3. The runtime smoke test in `.github/workflows/ci.yml` (if applicable)
3. **Bilingual Documentation**:
   - User-facing workflow or feature changes should be reflected in both `README.md` (English) and `README.zh-TW.md` (Traditional Chinese).
