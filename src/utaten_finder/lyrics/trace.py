"""Structured JSONL match trace recording and reading utilities.

Trace format (``schema_version: 1``), one JSON object per line:

- ``session_start``:
  ``{"type": "session_start", "schema_version": 1, "ts_utc": str, "track": dict,
     "asr_config": dict, "matcher_config": dict, "lyrics": [{"index": int,
     "original": str, "normalized": str, "ruby_html": str, "stanza_break": bool}]}``
- ``asr_step``:
  ``{"type": "asr_step", "seq": int, "elapsed_sec": float, "ts_utc": str,
     "playback": {"spotify_progress_ms": int | None, "estimated_progress_ms": int | None,
                  "duration_ms": int | None, "is_playing": bool},
     "asr": dict, "normalized_recognized": str, "state_before": dict,
     "buffer_after_append": str, "trimmed": bool, "buffer_after_trim": str,
     "trim_target_length": int | None, "phase1_lookahead_lines": int | None,
     "phase1_range": [int, int] | None, "phase2_range": [int, int] | None,
     "candidates": list[dict], "decision": str, "matched_index": int | None,
     "display_index": int | None, "best_score": float | None,
     "threshold": float | None, "is_recovery": bool, "current_index_after": int}``
- ``manual_jump``:
  ``{"type": "manual_jump", "seq": int, "elapsed_sec": float, "ts_utc": str,
     "playback": dict, "from_index": int, "to_index": int,
     "current_line_text": str, "buffer_before": str}``
- ``session_end``:
  ``{"type": "session_end", "elapsed_sec": float, "ts_utc": str, "reason": str,
     "stats": {"asr_steps": int, "matches_phase1": int,
               "matches_phase2": int, "manual_jumps": int}}``
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, TextIO

from utaten_finder.core import config

TRACE_SCHEMA_VERSION = 1

_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")
_MULTI_UNDERSCORE_RE = re.compile(r"_+")


def sanitize_trace_slug(text: str, max_length: int = 48) -> str:
    """Convert arbitrary track title/artist text into a safe Windows filename slug."""
    cleaned = _INVALID_FILENAME_CHARS_RE.sub("_", text or "")
    cleaned = _WHITESPACE_RE.sub("_", cleaned)
    cleaned = _MULTI_UNDERSCORE_RE.sub("_", cleaned)
    cleaned = cleaned.strip(" ._")
    if max_length > 0 and len(cleaned) > max_length:
        cleaned = cleaned[:max_length].strip(" ._")
    return cleaned or "unknown_track"


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _snapshot_lyrics_for_trace(lyrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    for pos, line in enumerate(lyrics):
        snapshot.append(
            {
                "index": int(line.get("index", pos)),
                "original": str(line.get("original") or ""),
                "normalized": str(line.get("normalized") or ""),
                "ruby_html": str(line.get("ruby_html") or ""),
                "stanza_break": bool(line.get("stanza_break", False)),
            }
        )
    return snapshot


class MatchTraceRecorder:
    """Thread-safe JSONL writer for recording lyric synchronization sessions."""

    def __init__(
        self,
        traces_dir: Path | None = None,
        is_enabled_fn: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._traces_dir = Path(traces_dir) if traces_dir is not None else None
        self._is_enabled_fn = (
            is_enabled_fn
            if is_enabled_fn is not None
            else (lambda: bool(config.MATCH_TRACE_ENABLED))
        )
        self._clock = clock
        self._lock = threading.Lock()
        self._fp: TextIO | None = None
        self._current_path: Path | None = None
        self._session_started_at = 0.0
        self._seq = 0
        self._stats: dict[str, int] = {
            "asr_steps": 0,
            "matches_phase1": 0,
            "matches_phase2": 0,
            "manual_jumps": 0,
        }
        self._pending_session: dict[str, Any] | None = None

    @property
    def current_trace_path(self) -> Path | None:
        with self._lock:
            return self._current_path

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._fp is not None

    def _resolve_traces_dir(self) -> Path:
        return self._traces_dir if self._traces_dir is not None else config.TRACES_DIR

    def _build_session_slug(self, track_meta: dict[str, Any]) -> str:
        artist = str(
            track_meta.get("artist_name") or track_meta.get("artist") or ""
        ).strip()
        title = str(
            track_meta.get("song_name") or track_meta.get("title") or ""
        ).strip()
        if artist and title:
            raw_slug = f"{artist}_{title}"
        elif title or artist:
            raw_slug = title or artist
        else:
            raw_slug = str(track_meta.get("song_id") or "unknown_track")
        return sanitize_trace_slug(raw_slug)

    def _write_line_locked(self, record: dict[str, Any]) -> bool:
        if self._fp is None:
            return False
        try:
            self._fp.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fp.flush()
            return True
        except OSError as exc:
            print(f"⚠️ [MatchTrace] 寫入 Trace 失敗: {exc}")
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None
            self._current_path = None
            return False

    def _open_session_locked(self) -> Path | None:
        if self._pending_session is None:
            return None

        traces_dir = self._resolve_traces_dir()
        try:
            traces_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = self._build_session_slug(self._pending_session["track"])
            base_name = f"{stamp}_{slug}"
            candidate = traces_dir / f"{base_name}.jsonl"
            suffix = 2
            while candidate.exists():
                candidate = traces_dir / f"{base_name}_{suffix}.jsonl"
                suffix += 1

            self._fp = open(candidate, "w", encoding="utf-8", newline="\n")
        except OSError as exc:
            print(f"⚠️ [MatchTrace] 無法建立 Trace 檔案: {exc}")
            self._fp = None
            self._current_path = None
            return None

        self._current_path = candidate
        self._session_started_at = self._clock()
        self._seq = 0
        self._stats = {
            "asr_steps": 0,
            "matches_phase1": 0,
            "matches_phase2": 0,
            "manual_jumps": 0,
        }

        header = {
            "type": "session_start",
            "schema_version": TRACE_SCHEMA_VERSION,
            "ts_utc": _utc_now_iso(),
            "track": self._pending_session["track"],
            "asr_config": self._pending_session["asr_config"],
            "matcher_config": self._pending_session["matcher_config"],
            "lyrics": self._pending_session["lyrics"],
        }
        if not self._write_line_locked(header):
            return None

        print(f"📝 [MatchTrace] 開始記錄比對過程: {candidate}")
        return candidate

    def _close_session_locked(self, reason: str) -> None:
        if self._fp is None:
            return
        elapsed = round(max(0.0, self._clock() - self._session_started_at), 4)
        footer = {
            "type": "session_end",
            "elapsed_sec": elapsed,
            "ts_utc": _utc_now_iso(),
            "reason": reason,
            "stats": dict(self._stats),
        }
        try:
            self._fp.write(json.dumps(footer, ensure_ascii=False) + "\n")
            self._fp.flush()
        except OSError:
            pass
        finally:
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None
            self._current_path = None

    def start_session(
        self,
        track_meta: dict[str, Any] | None,
        lyrics: list[dict[str, Any]],
        *,
        asr_config: dict[str, Any] | None = None,
        matcher_config: dict[str, Any] | None = None,
    ) -> Path | None:
        """Start (or stage for lazy start) a trace session for the loaded lyrics."""
        lyrics_snapshot = _snapshot_lyrics_for_trace(lyrics)
        with self._lock:
            self._close_session_locked("track_changed")
            if not lyrics_snapshot:
                self._pending_session = None
                return None

            self._pending_session = {
                "track": dict(track_meta or {}),
                "lyrics": lyrics_snapshot,
                "asr_config": dict(asr_config or {}),
                "matcher_config": dict(matcher_config or {}),
            }
            if not self._is_enabled_fn():
                return None
            return self._open_session_locked()

    def _ensure_ready_for_event_locked(self) -> bool:
        if not self._is_enabled_fn():
            if self._fp is not None:
                self._close_session_locked("disabled")
            return False
        if self._fp is None:
            if self._pending_session is None:
                return False
            return self._open_session_locked() is not None
        return True

    def record_asr_step(self, step_data: dict[str, Any]) -> bool:
        """Append one ``asr_step`` record if tracing is enabled."""
        with self._lock:
            if not self._ensure_ready_for_event_locked():
                return False
            self._seq += 1
            self._stats["asr_steps"] += 1
            decision = step_data.get("decision")
            if decision == "matched_phase1":
                self._stats["matches_phase1"] += 1
            elif decision == "matched_phase2_recovery":
                self._stats["matches_phase2"] += 1

            record = {
                "type": "asr_step",
                "seq": self._seq,
                "elapsed_sec": round(
                    max(0.0, self._clock() - self._session_started_at), 4
                ),
                "ts_utc": _utc_now_iso(),
                **step_data,
            }
            return self._write_line_locked(record)

    def record_manual_jump(self, jump_data: dict[str, Any]) -> bool:
        """Append one ``manual_jump`` record if tracing is enabled."""
        with self._lock:
            if not self._ensure_ready_for_event_locked():
                return False
            self._seq += 1
            self._stats["manual_jumps"] += 1
            record = {
                "type": "manual_jump",
                "seq": self._seq,
                "elapsed_sec": round(
                    max(0.0, self._clock() - self._session_started_at), 4
                ),
                "ts_utc": _utc_now_iso(),
                **jump_data,
            }
            return self._write_line_locked(record)

    def close_session(
        self, reason: str = "ended", *, clear_pending: bool = False
    ) -> None:
        """Write ``session_end`` and close the active trace file if open."""
        with self._lock:
            self._close_session_locked(reason)
            if clear_pending:
                self._pending_session = None


def load_trace(path: Path | str) -> dict[str, Any]:
    """Load and parse a JSONL match trace file for offline replay or inspection."""
    trace_path = Path(path)
    session_start: dict[str, Any] | None = None
    session_end: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []

    with open(trace_path, "r", encoding="utf-8") as fp:
        for line_no, raw_line in enumerate(fp, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Trace 檔案第 {line_no} 行 JSON 格式錯誤 ({trace_path}): {exc}"
                ) from exc

            rec_type = record.get("type")
            if rec_type == "session_start":
                session_start = record
            elif rec_type in {"asr_step", "manual_jump"}:
                events.append(record)
            elif rec_type == "session_end":
                session_end = record

    if session_start is None:
        raise ValueError(f"Trace 檔案缺少 session_start 標頭: {trace_path}")

    return {
        "path": str(trace_path),
        "session_start": session_start,
        "events": events,
        "session_end": session_end,
    }
