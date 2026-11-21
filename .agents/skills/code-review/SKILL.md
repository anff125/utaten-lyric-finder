---
name: code-review
description: >-
  Perform a thorough, project-specific code review for utaten-lyric-finder.
  Use when the user runs /code-review or asks to review uncommitted changes,
  a branch, a commit, a pull request, or specific modules in this repository.
---

# `utaten-lyric-finder` Code Review

Conduct a read-only, evidence-based code review tailored to `utaten-lyric-finder`'s Windows desktop architecture, ASR/lyric-alignment invariants, portable packaging rules, and CI gates. Do **not** modify repository files during the review unless the user explicitly asks to apply fixes.

---

## Step 1: Determine Review Scope

Inspect what needs to be reviewed based on user arguments or repository state:

1. **Specific target provided** (commit SHA, branch/PR range, or file paths): inspect that target directly (e.g., `git show <sha>`, `git diff <base>..HEAD`, or reading the specified files).
2. **Uncommitted changes exist** (`git status --short` is non-empty): review staged, unstaged, and untracked changes (`git diff`, `git diff --cached`, plus reading untracked files).
3. **Clean working tree on a feature branch**: review the branch diff against `main` (`git diff main...HEAD` and `git log --oneline main..HEAD`).
4. **Clean working tree on `main` with no target**: review the latest commit (`git show HEAD`) or ask the user which branch, commit, or files to review.

Always read the surrounding functions, callers, corresponding tests, and paired front/back-end files—never review diff hunks in isolation.

---

## Step 2: Run Automated Verification Checks

Execute the applicable checks and record exact results (never assume or fabricate pass status):

1. **Ruff linter**:
   ```powershell
   .\.venv\Scripts\ruff.exe check .
   ```
2. **Unit tests**:
   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -v
   ```
3. **Runtime smoke import test** (mirrors `.github/workflows/ci.yml`):
   ```powershell
   .\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'src'); import webview; from utaten_finder.core import config, events, orchestrator; from utaten_finder.media import spotify_api, system_media; from utaten_finder.lyrics import web_scraper, parser, matching, phonology, song_cache, trace; from utaten_finder.audio import process_loopback, cuda_loader, asr_worker; from utaten_finder.ui import webview_app; print('[SUCCESS] runtime smoke imports loaded')"
   ```
4. **PowerShell Script Analyzer** (required whenever any `*.ps1` file is in scope):
   ```powershell
   pwsh -NoProfile -NonInteractive -Command "Invoke-ScriptAnalyzer -Path ./scripts/build_portable.ps1 -EnableExit"
   ```
5. **Encoding & line-ending check** (whenever `.bat`, `.ps1`, or `requirements.txt` are in scope):
   - `*.bat` (`start.bat`, `update.bat` template): must use `CRLF` (`\r\n`) and UTF-8 **without** BOM (with `chcp 65001 >nul`).
   - `scripts/build_portable.ps1`: must retain UTF-8 **with** BOM if it contains non-ASCII characters.
   - `requirements.txt`: must be UTF-8 **without** BOM.

---

## Step 3: Inspect Project-Specific Review Dimensions

Check every applicable area below against the files in scope:

### 1. Core, Concurrency & UI Bridge (`core/`, `ui/`, `assets/web/`)
- **Thread safety & locking**: Verify shared state across the track-polling thread, ASR worker thread, `EventBus`, and PyWebView UI thread. Ensure UI updates marshal safely via `WebViewBridge` / `evaluate_js`.
- **`pythonw.exe` console-less safety**: `sys.stdout` and `sys.stderr` can be `None` under `pythonw.exe`; confirm logging/print paths and `install_console_log_capture()` handle `None` streams without crashing.
- **Python ↔ JS contract**: When `src/utaten_finder/ui/webview_app.py` (`JsApi`, event emissions) or `assets/web/{index.html,styles.css,app.js}` changes, verify method names, argument shapes, and event payload keys match on both sides.

### 2. Audio, CUDA & WASAPI Loopback (`audio/`)
- **Hardware & process fallbacks**: Never assume an NVIDIA GPU, CUDA DLLs, or a running `Spotify.exe` process exists. Verify graceful CPU (`int8`) fallback and `fallback` status reporting.
- **DLL preloading & scope**: Keep `cuda_loader.prepare_windows_cuda_dll_paths()` ahead of `faster_whisper` / `ctranslate2` imports, preserve `SetErrorMode` / `WinError 577` handling, and check `nonlocal` declarations in nested worker callbacks (e.g., `_process_audio_chunk` in `asr_worker.py`).
- **Resource cleanup**: Check COM/WASAPI handles, audio streams, and thread shutdown paths in `process_loopback.py` and `asr_worker.py` for leaks or hangs on track/device switch or app exit.

### 3. Lyrics Scraping, Parsing & Matching (`lyrics/`, `scripts/eval_matching.py`)
- **HTML sanitization & DOM variants**: In `parser.py` and `web_scraper.py`, verify both `<p>` and `<br>` UtaTen layouts, `<rt>` furigana extraction priority, network timeouts/error handling, and strict sanitization before injecting `<ruby>/<rt>` markup into WebView.
- **Matching & beam invariants**: In `matching.py` and `phonology.py`, verify lock discipline (`LyricSyncEngine`), beam hypothesis state consistency (`pos`, `last_line`, clamps/rewinds, duplicate-line TTL, short-line guards), and phonological normalization symmetry.
- **Trace & offline eval parity**: When matching, phonology, or `trace.py` changes, confirm `MatchTraceRecorder` / `load_trace` schema compatibility and hyperparameter parity with `scripts/eval_matching.py`.

### 4. Portable Packaging, CI & Docs (`scripts/build_portable.ps1`, `.github/`, `README*.md`)
- **New, renamed, or deleted modules/assets**: Verify sync across:
  - `scripts/build_portable.ps1` (`$filesToCopy`, recursive `src/utaten_finder` / `assets` copies, and the `update.bat` `files = [...]` list).
  - `.github/workflows/ci.yml` (runtime smoke test imports, `RequiredFiles`, `ForbiddenFiles`).
- **Security & repo hygiene**: Ensure no OAuth tokens/secrets, `data/` runtime files, or `build/` artifacts are tracked or leaked.
- **Bilingual documentation**: Confirm user-facing CLI, UI, or workflow changes are documented in both `README.md` and `README.zh-TW.md`.
- **Code quality & tests**: Flag duplicated logic, missing edge-case/regression tests in `tests/`, missing docstrings on new public APIs, or accidental removal of unrelated comments.

---

## Step 4: Present the Review Report

Structure the response concisely using this format (matching the user's language):

1. **Review Scope & Automated Checks**
   - Summarize the reviewed target (branch/commits/files).
   - List each command executed and its exact observed outcome (`PASS` / `FAIL` / `Not applicable`).
2. **Findings** (ordered by severity; omit empty severity sections, or explicitly state "No issues found" if clean)
   - **Blocker**: Correctness bugs, crashes, race conditions, XSS/security risks, broken portable build or CI.
   - **Warning**: Edge-case regressions, missing fallback paths, trace/eval drift, missing unit tests.
   - **Nit**: Readability improvements, minor duplication, missing bilingual doc updates.
   - Format each finding with a clickable `file://` link (including line numbers), the concrete failure scenario or risk, and a clear, minimal fix recommendation.
3. **Verdict**
   - Conclude with **`Approve`**, **`Approve with minor nits`**, or **`Request changes`**, and note any residual risk or untested hardware paths (e.g., live CUDA GPU or Windows 11 `Spotify.exe` WASAPI capture).
