---
name: code-review
description: >-
  Perform a thorough, project-specific code review for utaten-lyric-finder.
  Use when the user runs /code-review or asks to review uncommitted changes,
  a branch, a commit, a GitHub pull request (URL or number), or specific modules
  in this repository, re-review fix commits after earlier feedback, or post /
  resolve review comments on GitHub.
---

# `utaten-lyric-finder` Code Review

Conduct a read-only, evidence-based code review tailored to `utaten-lyric-finder`'s Windows desktop architecture, ASR/lyric-alignment invariants, portable packaging rules, and CI gates.

**Read-only rule**: Do **not** modify repository files, switch the user's checked-out branch, or perform external side effects (posting GitHub comments/reviews, resolving threads, pushing) unless the user explicitly asks. Use a temporary `git worktree` (Step 2) instead of checking out the reviewed ref in the user's working copy.

---

## Step 0: Environment Notes (Windows)

- Tool shells run Windows PowerShell 5.1. `git` / `gh` may not be on `PATH`:
  - Git: `"$env:LOCALAPPDATA\Programs\Git\cmd\git.exe"` or `"C:\Program Files\Git\cmd\git.exe"`
  - GitHub CLI: `"C:\Program Files\GitHub CLI\gh.exe"` (check `gh auth status` before any GitHub API call)
- With `2>&1`, PowerShell 5.1 wraps native stderr output as `NativeCommandError` and may report exit code `1` even on success (e.g., `git fetch`, `git switch`, `unittest`). Judge results from the actual output (`All checks passed!`, `Ran N tests` + `OK`, `[SUCCESS] ...`), not the exit code alone.
- Never run interactive commands or `stdin`-reading flags (see `AGENTS.md`).

---

## Step 1: Determine Review Scope

Inspect what needs to be reviewed based on user arguments or repository state, and **record the exact reviewed SHA(s)** in the report:

1. **GitHub pull request** (URL like `https://github.com/<owner>/<repo>/pull/<n>` or `#<n>`):
   - `git fetch origin pull/<n>/head:pr-<n>` (re-run to refresh; use a new local name such as `pr-<n>-v2` if needed).
   - Review `git diff main...pr-<n>` and `git log --oneline main..pr-<n>`; optionally `gh pr view <n> --json headRefOid,author,state,files`.
   - Note whether the user's current branch equals the PR head (`git rev-parse HEAD pr-<n>`).
2. **Specific target provided** (commit SHA, branch/range, or file paths): inspect that target directly (e.g., `git show <sha>`, `git diff <base>..<ref>`, or reading the specified files).
3. **Uncommitted changes exist** (`git status --short` is non-empty): review staged, unstaged, and untracked changes (`git diff`, `git diff --cached`, plus reading untracked files).
4. **Clean working tree on a feature branch**: review the branch diff against `main` (`git diff main...HEAD` and `git log --oneline main..HEAD`).
5. **Clean working tree on `main` with no target**: review the latest commit (`git show HEAD`) or ask the user which branch, commit, or files to review.

For large diffs, dump the diff to the conversation scratch directory and read it in chunks; exclude pure deletions or generated content only when they are clearly irrelevant (still mention them in the report).

Always read the surrounding functions, callers, corresponding tests, and paired front/back-end files—never review diff hunks in isolation.

---

## Step 2: Run Automated Verification Checks

Run checks **against the reviewed ref**, and record exact results (never assume or fabricate pass status).

- If the reviewed ref is the user's current checkout (or uncommitted changes), run in the repository root.
- Otherwise, create a temporary detached worktree in the conversation scratch directory, run checks there with the main repository's virtual environment, then remove it:
  ```powershell
  git worktree add --detach <scratch>\wt <ref>
  # run checks inside <scratch>\wt using C:\...\utaten-lyric-finder\.venv\Scripts\{ruff,python}.exe
  git worktree remove --force <scratch>\wt
  ```

Checks:

1. **Ruff linter**:
   ```powershell
   .\.venv\Scripts\ruff.exe check .
   ```
2. **Unit tests**:
   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -v
   ```
3. **Runtime smoke import test**: copy the exact `python -c "..."` command from the **reviewed ref's** `.github/workflows/ci.yml` ("Runtime smoke test" step) so the module list matches that ref (do not hardcode a module list here—modules such as `phonology` may not exist on older refs). Run it with `.\.venv\Scripts\python.exe`.
4. **PowerShell Script Analyzer** (required whenever any `*.ps1` file is in scope):
   ```powershell
   pwsh -NoProfile -NonInteractive -Command "Invoke-ScriptAnalyzer -Path ./scripts/build_portable.ps1 -EnableExit"
   ```
5. **Encoding & line-ending check** (whenever `.bat`, `.ps1`, or `requirements.txt` are in scope):
   - `*.bat` (`start.bat`, `update.bat` template): must use `CRLF` (`\r\n`) and UTF-8 **without** BOM (with `chcp 65001 >nul`).
   - `scripts/build_portable.ps1`: must retain UTF-8 **with** BOM if it contains non-ASCII characters.
   - `requirements.txt`: must be UTF-8 **without** BOM.
6. **Consistency drift**: if the smoke-test module list in `AGENTS.md` (Check 3) differs from `.github/workflows/ci.yml`, flag it as a Nit.

---

## Step 3: Inspect Project-Specific Review Dimensions

Check every applicable area below against the files in scope:

### 1. Core, Concurrency & UI Bridge (`core/`, `ui/`, `assets/web/`)
- **Thread safety & locking**: Verify shared state across the track-polling thread, ASR worker thread, `EventBus`, and PyWebView UI thread. Ensure UI updates marshal safely via `WebViewBridge` / `evaluate_js`. Check lock ordering between nested locks (e.g., `LyricSyncEngine` → `MatchTraceRecorder`) and flag blocking I/O performed while holding hot locks.
- **Lifecycle races**: On stop / shutdown / track change / setting toggles, check in-flight worker callbacks that may arrive after cleanup (e.g., re-opening files or sessions after close).
- **`pythonw.exe` console-less safety**: `sys.stdout` and `sys.stderr` can be `None` under `pythonw.exe`; confirm logging/print paths and `install_console_log_capture()` handle `None` streams without crashing.
- **Python ↔ JS contract**: When `src/utaten_finder/ui/webview_app.py` (`JsApi`, event emissions) or `assets/web/{index.html,styles.css,app.js}` changes, verify method names, argument shapes, and event payload keys match on both sides.

### 2. Audio, CUDA & WASAPI Loopback (`audio/`)
- **Hardware & process fallbacks**: Never assume an NVIDIA GPU, CUDA DLLs, or a running `Spotify.exe` process exists. Verify graceful CPU (`int8`) fallback and `fallback` status reporting.
- **DLL preloading & scope**: Keep `cuda_loader.prepare_windows_cuda_dll_paths()` ahead of `faster_whisper` / `ctranslate2` imports, preserve `SetErrorMode` / `WinError 577` handling, and check `nonlocal` declarations in nested worker callbacks (e.g., `_process_audio_chunk` in `asr_worker.py`).
- **Resource cleanup**: Check COM/WASAPI handles, audio streams, and thread shutdown paths in `process_loopback.py` and `asr_worker.py` for leaks or hangs on track/device switch or app exit.

### 3. Lyrics Scraping, Parsing & Matching (`lyrics/`, `scripts/eval_matching.py`)
- **HTML sanitization & DOM variants**: In `parser.py` and `web_scraper.py`, verify both `<p>` and `<br>` UtaTen layouts, `<rt>` furigana extraction priority, network timeouts/error handling, and strict sanitization before injecting `<ruby>/<rt>` markup into WebView.
- **Matching & beam invariants**: In `matching.py` and `phonology.py`, verify lock discipline (`LyricSyncEngine`), beam hypothesis state consistency (`pos`, `last_line`, clamps/rewinds, duplicate-line TTL, short-line guards), and phonological normalization symmetry.
- **Trace & offline eval parity**: When matching, phonology, or `trace.py` changes, confirm `MatchTraceRecorder` / `load_trace` schema compatibility (including older traces), hyperparameter parity with `scripts/eval_matching.py`, and that replay reproduces live decisions: same clock source for live and replay, initial engine state captured for lazily opened sessions, no `0.0` sentinel collisions, and metrics that are meaningful for the chosen anchor mode.

### 4. Portable Packaging, CI & Docs (`scripts/build_portable.ps1`, `.github/`, `README*.md`)
- **New, renamed, or deleted modules/assets**: Verify sync across:
  - `scripts/build_portable.ps1` (`$filesToCopy`, recursive `src/utaten_finder` / `assets` copies, and the `update.bat` `files = [...]` list).
  - `.github/workflows/ci.yml` (runtime smoke test imports, `RequiredFiles`, `ForbiddenFiles`).
- **Security & repo hygiene**: Ensure no OAuth tokens/secrets, `data/` runtime files, or `build/` artifacts are tracked or leaked.
- **Bilingual documentation**: Confirm user-facing CLI, UI, settings, or data-retention behavior changes (e.g., automatic file pruning) are documented in both `README.md` and `README.zh-TW.md`.
- **Code quality & scope**: Flag duplicated logic, deep nesting, missing edge-case/regression tests in `tests/`, missing docstrings on new public APIs, accidental removal of unrelated comments, and unrelated changes bundled into the PR.

---

## Step 4: Present the Review Report

Structure the response concisely using this format (matching the user's language):

1. **Review Scope & Automated Checks**
   - Summarize the reviewed target (PR / branch / commits / files) and the exact head SHA.
   - List each command executed and its exact observed outcome (`PASS` / `FAIL` / `Not applicable`), and where it ran (repo root or temporary worktree).
2. **Findings** (ordered by severity; omit empty severity sections, or explicitly state "No issues found" if clean)
   - **Blocker**: Correctness bugs, crashes, race conditions, XSS/security risks, broken portable build or CI.
   - **Warning**: Edge-case regressions, missing fallback paths, trace/eval drift, missing unit tests.
   - **Nit**: Readability improvements, minor duplication, missing bilingual doc updates.
   - Format each finding with a clickable `file://` link (including line numbers), the concrete failure scenario or risk, and a clear, minimal fix recommendation.
3. **Verdict**
   - Conclude with **`Approve`**, **`Approve with minor nits`**, or **`Request changes`**, and note any residual risk or untested hardware paths (e.g., live CUDA GPU or Windows 11 `Spotify.exe` WASAPI capture).

---

## Step 5 (Optional): Post the Review to GitHub

Only when the user explicitly asks to post comments. Ask which language to use for GitHub comments if it is unclear (default: the language of the existing PR/repo conversation).

1. **Build one review with a summary body plus inline comments** and submit it in a single API call:
   ```powershell
   gh api -X POST repos/<owner>/<repo>/pulls/<n>/reviews --input <scratch>\review.json
   ```
   - Generate `review.json` with a small Python script in the scratch directory (avoids PowerShell quoting issues); write it as UTF-8 with `ensure_ascii=False`.
   - Payload: `commit_id` = the reviewed head SHA, `event`, `body`, and `comments: [{path, line, side: "RIGHT", body}]`.
   - `line` is the line number in the **head version of the file** (verify with `Select-String` / `git show <sha>:<path>`), not the diff position, and must fall inside a diff hunk.
2. **Event type**: use `REQUEST_CHANGES` / `APPROVE` when appropriate, but if the authenticated `gh` user is the PR author, GitHub rejects those—use `COMMENT` and mark blockers explicitly in the body.
3. Report the returned review URL to the user.

### Resolving review threads

Only when the user asks. List threads, then resolve by ID:

```powershell
gh api graphql -f query='query{repository(owner:\"<owner>\",name:\"<repo>\"){pullRequest(number:<n>){headRefOid reviewThreads(first:100){nodes{id isResolved path line}}}}}'
gh api graphql -f query="mutation{resolveReviewThread(input:{threadId:\`"<id>\`"}){thread{isResolved}}}"
```

Threads with `line: null` are outdated (the code changed since the comment). Resolving does not verify the fix—tell the user and offer a follow-up re-review (Step 6).

---

## Step 6: Follow-Up Re-Review of Fix Commits

When new commits address earlier feedback:

1. Fetch the new head and diff it against the **previously reviewed SHA**: `git log --oneline <old>..<new>` and `git diff <old> <new>`.
2. Re-run Step 2 checks against the new head (temporary worktree if it is not checked out).
3. Map every original finding to a status table: ✅ fixed / ⚠️ partially fixed / ❌ not fixed, with a one-line explanation of how it was addressed.
4. Review the fix itself for new issues (e.g., I/O moved inside a lock, new undocumented behavior, cache lifetimes) and list them as new findings.
5. End with an updated verdict.
