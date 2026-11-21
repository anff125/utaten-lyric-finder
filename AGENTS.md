# AGENTS.md — AI Coding Agent Guidelines for `utaten-lyric-finder`

This document provides context, architecture details, and mandatory verification rules for AI coding agents working on `utaten-lyric-finder`.

---

## 1. Project Overview & Architecture

`utaten-lyric-finder` is a Windows desktop application that detects the currently playing song (via Spotify Web API or Windows SMTC), scrapes and sanitizes Japanese furigana (`<ruby>/<rt>`) lyrics from [UtaTen](https://utaten.com/), renders them in a single-window PyWebView (Edge WebView2) desktop interface, and uses `faster-whisper` ASR to automatically scroll and highlight the current lyric line in real time.

### Core Modules
- **`app.py`**: Unified application entry point. Configures `src/` on `sys.path`, supports `--cli` and `--debug`, installs `EventBus` console/logging capture, and defaults to launching `utaten_finder.ui.webview_app`.
- **`src/utaten_finder/ui/`**:
  - `webview_app.py`: PyWebView single-window desktop controller, `AppController` / `JsApi` bridge, and thread-safe `EventBus` -> `evaluate_js` dispatcher (`WebViewBridge`).
- **`src/utaten_finder/core/`**:
  - `config.py`: Global configuration, path constants (`PROJECT_ROOT`, `DATA_DIR`, `ASSETS_DIR`), legacy data auto-migration from `.cache/` / `_cache/` / root `song_cache.json` to `data/`, and persistent settings (`data/runtime_settings.json`).
  - `events.py`: Thread-safe `EventBus` singleton (`bus`) with `has_subscribers`, `install_console_log_capture()` (wrapping `stdout`/`stderr` and attaching `EventBusLogHandler` to Python `logging` safely even under `pythonw.exe` where streams are `None`), and buffered log history.
  - `orchestrator.py`: Core orchestration loop (polls current track, looks up cache/scraper, loads parsed lyrics into `LyricSyncEngine`, emits `track_updated`/`asr_preview`, and optionally opens matched URLs in the default browser via `open_external_browser` in `--cli` mode).
- **`src/utaten_finder/audio/`**:
  - `cuda_loader.py`: Dynamically registers pip-installed NVIDIA CUDA DLL directories (`nvidia/cublas/bin`, `nvidia/cudnn/bin`) into both `os.add_dll_directory` and `os.environ["PATH"]` before importing `faster_whisper` / `ctranslate2`, with `SetErrorMode` popup suppression and `WinError 577` preloading retries.
  - `asr_worker.py`: Audio capture loop + `faster-whisper` (`ctranslate2`) transcription pipeline with 1-second silent warm-up, automatic CPU (`compute_type="int8"`) fallback (tracked via `fallback` flag in `asr_status`), rolling upcoming-lyric `initial_prompt` (`condition_on_previous_text=False`), confidence/interlude hallucination filtering (`filter_asr_segments`), optional 16kHz WAV recording (`AsrWavRecorder` -> `data/recordings/`), and throttled (~12.5 Hz) real-time RMS/Peak VU meter emission (`compute_audio_level`, `AudioLevelEmitter`).
  - `process_loopback.py`: Windows 11 native WASAPI Process Loopback (`ActivateAudioInterfaceAsync` via `ctypes`/COM) to capture audio exclusively from `Spotify.exe` without system/game/Discord noise, plus stereo Mid-channel `(L + R) / 2` downmixing (`downmix_to_mono`) and optional Butterworth vocal bandpass filtering (`apply_vocal_bandpass`), streaming packet chunks to `on_packet` for live VU meter updates.
- **`src/utaten_finder/lyrics/`**:
  - `matching.py`: Japanese text normalization (`pykakasi` / `jaconv` to hiragana), ASR segment confidence & interlude hallucination filtering (`filter_asr_segments`, `has_repetitive_token_loop`), `LyricsAgent` two-phase fuzzy matching, and `LyricSyncEngine` decoupled state machine (including `get_upcoming_prompt` / `get_asr_context`).
  - `parser.py`: Browserless UtaTen HTML parser (`parse_utaten_lyrics`) producing sanitized `<ruby>/<rt>` lyric lines and normalized matching fields for both `<p>` and `<br>` DOM layouts.
  - `web_scraper.py`: Searches UtaTen / DuckDuckGo (`ddgs`) and fetches UtaTen lyric page HTML (`fetch_utaten_lyrics_html`).
  - `song_cache.py`: Local JSON cache (`data/song_cache.json`) mapping track keys to UtaTen URLs and match quality status.
  - `trace.py`: Thread-safe JSONL match trace recorder (`MatchTraceRecorder`) and reader (`load_trace`) writing structured synchronization diagnostics to `data/traces/`.
- **`src/utaten_finder/media/`**:
  - `spotify_api.py` & `system_media.py`: Track metadata providers for Spotify Web API (`spotipy`) + OAuth callback HTTP server (`wait_for_oauth_callback`) and Windows SMTC (`winrt`).
- **`assets/`, `data/`, `scripts/`, `start.bat`, & `tests/`**: Static assets (`assets/app.ico`, `assets/web/{index.html,styles.css,app.js}`), ignored runtime data (`data/`), Windows portable build & offline match evaluation scripts (`scripts/build_portable.ps1`, `scripts/eval_matching.py`), console-less launcher (`start.bat` using `pythonw.exe` by default with `--console` / `--cli` / `--debug` support), and unit tests (`tests/`).

---

## 2. Environment & Tooling Notes (Windows)

- **Virtual Environment**: Local Python environment is located at `.venv\` (`.venv\Scripts\python.exe`, `.venv\Scripts\pythonw.exe`, `.venv\Scripts\ruff.exe`).
- **Git Executable**: If `git` is not on the default PowerShell `PATH`, use `"$env:LOCALAPPDATA\Programs\Git\cmd\git.exe"` or `"C:\Program Files\Git\cmd\git.exe"`.
- **Shell & Non-Interactive Execution**: Tool shells spawn Windows PowerShell 5.1 by default even when PowerShell 7 (`pwsh`) is installed. Never execute interactive commands that may block waiting for prompts or `stdin`; always pass non-interactive flags (`-NoProfile -NonInteractive`) and explicitly invoke `pwsh -NoProfile -NonInteractive` for PowerShell 7 / `PSScriptAnalyzer` checks.
  - Never use `stdin`-reading flags like `git commit -F -` or `gh pr create --body-file -`, and never pipe text into `& { ... }` scriptblocks (which can hang waiting for `stdin` EOF). Run `git` / `gh` directly in the shell using multiple `-m "..."` flags or a PowerShell here-string variable (`$body = @' ... '@` with `--body $body`), without wrapping them in an extra `pwsh -Command '...'` string.
- **File Encodings & Line Endings**:
  - **`*.bat` files (`start.bat`, `update.bat`)**: MUST use Windows **`CRLF` (`\r\n`)** line endings and **UTF-8 without BOM** (or pure ASCII). `cmd.exe` misparses byte offsets if a batch file uses `LF` line endings or UTF-8 BOM without `chcp 65001 >nul`.
  - **`*.ps1` files (`scripts/build_portable.ps1`)**: Must include a **UTF-8 BOM** if it contains non-ASCII characters (such as Traditional Chinese comments), as required by Windows PowerShell 5.1 and `PSScriptAnalyzer`.
  - **`requirements.txt`**: Keep encoded in **UTF-8 without BOM**.
  - **Python/Text working-copy files (`*.py`, `*.md`, etc.)**: On Windows (`core.autocrlf=true`), preserve **`CRLF` (`\r\n`)** line endings so `git add` does not emit `LF will be replaced by CRLF` warnings.

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

### Check 2: Unit Tests
```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
- All tests in `tests/` must pass (`OK`).

### Check 3: Runtime Smoke Test
```powershell
.\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'src'); import webview; from utaten_finder.core import config, events, orchestrator; from utaten_finder.media import spotify_api, system_media; from utaten_finder.lyrics import web_scraper, parser, matching, song_cache, trace; from utaten_finder.audio import process_loopback, cuda_loader, asr_worker; from utaten_finder.ui import webview_app; print('[SUCCESS] runtime smoke imports loaded')"
```
- Ensure module-level imports do not crash when optional hardware or audio devices are absent.

### Check 4: PowerShell Script Analyzer (when editing `*.ps1`)
If `scripts/build_portable.ps1` is modified:
```powershell
pwsh -NoProfile -NonInteractive -Command "Invoke-ScriptAnalyzer -Path ./scripts/build_portable.ps1 -EnableExit"
```
- Also ensure `scripts/build_portable.ps1` retains its UTF-8 with BOM encoding and that any newly added `.py` module in `app.py` or `src/utaten_finder/` or asset in `assets/web/` is covered by `$filesToCopy` / the `src/utaten_finder` recursive copy and the `files = [...]` list in `update.bat` inside `scripts/build_portable.ps1`.

---

## 4. Coding Conventions & Best Practices

1. **Maintain Resilient Fallbacks in `asr_worker.py`, `cuda_loader.py` & `process_loopback.py`**:
   - Never assume a CUDA GPU or `Spotify.exe` process is always present.
   - Keep `ctypes` DLL preloading (`cuda_loader.prepare_windows_cuda_dll_paths()`) intact before importing `faster_whisper` / `ctranslate2`.
   - When reassigning outer-scope variables inside nested worker functions (e.g., `model` fallback to CPU in `_process_audio_chunk`), always declare `nonlocal model`.
2. **Keep Portable Build in Sync**:
   - Whenever a new Python file or package module is added:
     1. Ensure it is copied in `scripts/build_portable.ps1` (either in `$filesToCopy` or under `src/utaten_finder/` / `assets/`)
     2. Update the `files = [...]` list inside the `update.bat` template in `scripts/build_portable.ps1`
     3. Update the runtime smoke test in `.github/workflows/ci.yml` (if applicable)
3. **Bilingual Documentation**:
   - User-facing workflow or feature changes should be reflected in both `README.md` (English) and `README.zh-TW.md` (Traditional Chinese).
4. **Branch, Auto-Commit & Pull Request Workflow**:
   - Develop feature, accuracy, refactor, and issue-tracked changes on a dedicated branch off `main` (e.g., `feat/issue-<n>-<slug>` or `fix/<slug>`) and merge via Pull Request so GitHub Actions CI validates every change before it reaches `main` (protecting portable `update.bat` users who sync directly from `main`).
   - Whenever code, configuration, or documentation changes are completed and all mandatory pre-commit checks (Section 3) pass, **automatically stage and create a descriptive `git commit`** on the working branch without waiting for the user to ask, and reference `Closes #<n>` in the PR body (and commit when applicable).
   - Tiny low-risk documentation typos may be committed directly if the user prefers.
