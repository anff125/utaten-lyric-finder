"""Japanese text normalization and two-phase fuzzy lyric synchronization."""

import re
import threading
import time
from typing import Any, Callable

from thefuzz import fuzz

from utaten_finder.core import config, events
from utaten_finder.lyrics.trace import MatchTraceRecorder

try:
    from pykakasi import kakasi

    _kakasi = kakasi()
    KAKASI_AVAILABLE = True
except Exception:
    _kakasi = None
    KAKASI_AVAILABLE = False


def normalize_match_text(text):
    text = text or ""
    text = text.lower()
    text = re.sub(r"[\s\u3000]", "", text)
    text = re.sub(r"[、。,.!！?？『』「」\\ー〜~…-]", "", text)
    return text


def normalize_to_hiragana(text):
    text = (text or "").strip()
    if not text:
        return ""
    if KAKASI_AVAILABLE and _kakasi is not None:
        try:
            converted = _kakasi.convert(text)
            return "".join(part.get("hira", "") for part in converted)
        except Exception:
            return text
    return text


def _length_coverage_ratio(source_text, target_text):
    if not target_text:
        return 0.0
    return min(len(source_text) / len(target_text), 1.0)


def merge_overlapped_text(existing_text, new_text, min_overlap=2, threshold=0.8):
    """
    比較現有字串的尾部與新字串的頭部，尋找重疊部分並進行合併去重。
    包含精確比對與模糊容錯機制。
    """
    if not existing_text:
        return new_text
    if not new_text:
        return existing_text

    max_len = min(len(existing_text), len(new_text))

    for overlap_len in range(max_len, min_overlap - 1, -1):
        if existing_text[-overlap_len:] == new_text[:overlap_len]:
            return existing_text + new_text[overlap_len:]

    best_overlap_len = 0
    best_score = 0
    fuzzy_min = max(min_overlap, 3)

    for overlap_len in range(max_len, fuzzy_min - 1, -1):
        suffix = existing_text[-overlap_len:]
        prefix = new_text[:overlap_len]
        score = fuzz.ratio(suffix, prefix) / 100.0

        if score >= threshold and score > best_score:
            best_overlap_len = overlap_len
            best_score = score
            if score >= 0.95:
                break

    if best_overlap_len > 0:
        return existing_text + new_text[best_overlap_len:]

    return existing_text + new_text


def score_similarity_detailed(
    na: str, nb: str, min_coverage_ratio: float = 0.4
) -> dict[str, float]:
    """Return detailed similarity breakdown matching calculate_similarity_normalized."""
    if not na or not nb:
        return {
            "coverage": 0.0,
            "raw_partial_score": 0.0,
            "partial_score": 0.0,
            "ratio_score": 0.0,
            "final_score": 0.0,
        }

    coverage = _length_coverage_ratio(na, nb)

    # 1. partial_ratio: 解決 Whisper 只聽到一半的問題
    raw_partial_score = fuzz.partial_ratio(na, nb) / 100.0
    partial_score = raw_partial_score
    if coverage < min_coverage_ratio:
        partial_score *= coverage / min_coverage_ratio

    # 2. ratio: 解決 Whisper 聽錯少數假名的問題 (基於 Levenshtein 編輯距離)
    ratio_score = fuzz.ratio(na, nb) / 100.0

    final_score = max(partial_score, ratio_score)
    return {
        "coverage": coverage,
        "raw_partial_score": raw_partial_score,
        "partial_score": partial_score,
        "ratio_score": ratio_score,
        "final_score": final_score,
    }


def calculate_similarity_normalized(na, nb, min_coverage_ratio=0.4):
    return score_similarity_detailed(na, nb, min_coverage_ratio)["final_score"]


def calculate_ratio_similarity_normalized(na, nb):
    if not na or not nb:
        return 0.0

    # 迷失恢復模式僅使用 ratio，避免 partial_ratio 對片段誤命中過於寬鬆
    return fuzz.ratio(na, nb) / 100.0


class LyricsAgent:
    """Sliding normalized ASR text buffer and scoring helper."""

    def __init__(self, min_coverage_ratio=0.4, max_buffer_ratio=1.8):
        self.min_coverage_ratio = min_coverage_ratio
        self.max_buffer_ratio = max_buffer_ratio
        self.asr_buffer = ""

    def reset(self):
        self.asr_buffer = ""

    def append(self, normalized_text):
        if normalized_text:
            self.asr_buffer = merge_overlapped_text(self.asr_buffer, normalized_text)

    def trim_if_needed(self, target_length):
        if target_length <= 0:
            return False

        max_length = int(target_length * self.max_buffer_ratio)
        if max_length <= 0:
            return False

        if len(self.asr_buffer) > max_length:
            drop = len(self.asr_buffer) // 2
            self.asr_buffer = self.asr_buffer[drop:]
            return True

        return False

    def score_detailed(self, target_normalized):
        return score_similarity_detailed(
            self.asr_buffer,
            target_normalized,
            self.min_coverage_ratio,
        )

    def score(self, target_normalized):
        return calculate_similarity_normalized(
            self.asr_buffer,
            target_normalized,
            self.min_coverage_ratio,
        )

    def ratio_score(self, target_normalized):
        return calculate_ratio_similarity_normalized(self.asr_buffer, target_normalized)


class LyricSyncEngine:
    """Thread-safe two-phase lyric line synchronization state machine."""

    def __init__(
        self,
        lookahead_seconds_per_line: float = 1.5,
        *,
        trace_recorder: MatchTraceRecorder | None = None,
        clock: Callable[[], float] = time.monotonic,
        fuzzy_lookahead_lines: int | None = None,
        fuzzy_next_line_threshold: float | None = None,
        recovery_lookahead_lines: int | None = None,
        recovery_threshold: float | None = None,
        duplicate_ttl: float | None = None,
        min_coverage_ratio: float = 0.4,
        max_buffer_ratio: float = 1.8,
        emit_events: bool = True,
    ) -> None:
        self._lock = threading.Lock()
        self._lyrics: list[dict[str, Any]] = []
        self._current_index = -1
        self._pending_scroll_targets: list[int] = []
        self._pending_highlight_index = -1
        self._agent = LyricsAgent(
            min_coverage_ratio=min_coverage_ratio,
            max_buffer_ratio=max_buffer_ratio,
        )
        self._last_success_normalized = ""
        self._last_success_at: float | None = None
        self._buffer_start_at: float | None = None
        self._lookahead_seconds_per_line = lookahead_seconds_per_line
        self._trace_recorder = trace_recorder
        self._clock = clock
        self._fuzzy_lookahead_lines = fuzzy_lookahead_lines
        self._fuzzy_next_line_threshold = fuzzy_next_line_threshold
        self._recovery_lookahead_lines = recovery_lookahead_lines
        self._recovery_threshold = recovery_threshold
        self._duplicate_ttl = duplicate_ttl
        self._emit_events = bool(emit_events)

        self._playback_song_id: str | None = None
        self._playback_progress_ms: int | None = None
        self._playback_duration_ms: int | None = None
        self._playback_polled_at: float | None = None
        self._playback_is_playing = False

    def _effective_lookahead_lines(self) -> int:
        if self._fuzzy_lookahead_lines is not None:
            return int(self._fuzzy_lookahead_lines)
        return int(config.FUZZY_LOOKAHEAD_LINES)

    def _effective_next_line_threshold(self) -> float:
        if self._fuzzy_next_line_threshold is not None:
            return float(self._fuzzy_next_line_threshold)
        return float(config.FUZZY_NEXT_LINE_THRESHOLD)

    def _effective_recovery_lookahead_lines(self) -> int:
        if self._recovery_lookahead_lines is not None:
            return int(self._recovery_lookahead_lines)
        return int(config.RECOVERY_LOOKAHEAD_LINES)

    def _effective_recovery_threshold(self) -> float:
        if self._recovery_threshold is not None:
            return float(self._recovery_threshold)
        return float(config.RECOVERY_THRESHOLD)

    def _effective_duplicate_ttl(self) -> float:
        if self._duplicate_ttl is not None:
            return float(self._duplicate_ttl)
        return float(config.ASR_BLOCK_SECONDS)

    def get_matcher_config(self) -> dict[str, Any]:
        """Return effective matcher hyperparameters for trace metadata."""
        return {
            "fuzzy_lookahead_lines": self._effective_lookahead_lines(),
            "fuzzy_next_line_threshold": self._effective_next_line_threshold(),
            "recovery_lookahead_lines": self._effective_recovery_lookahead_lines(),
            "recovery_threshold": self._effective_recovery_threshold(),
            "lookahead_seconds_per_line": self._lookahead_seconds_per_line,
            "min_coverage_ratio": self._agent.min_coverage_ratio,
            "max_buffer_ratio": self._agent.max_buffer_ratio,
            "duplicate_ttl": self._effective_duplicate_ttl(),
        }

    def update_playback_progress(
        self,
        progress_ms: int | None,
        duration_ms: int | None,
        *,
        song_id: str | None = None,
        is_playing: bool = True,
    ) -> None:
        """Store latest playback progress snapshot (e.g., from Spotify Web API)."""
        with self._lock:
            self._playback_song_id = song_id
            self._playback_progress_ms = (
                int(progress_ms) if progress_ms is not None else None
            )
            self._playback_duration_ms = (
                int(duration_ms) if duration_ms is not None else None
            )
            self._playback_polled_at = self._clock()
            self._playback_is_playing = bool(is_playing)

    def _snapshot_playback_progress_locked(self, now: float) -> dict[str, Any]:
        spotify_ms = self._playback_progress_ms
        duration_ms = self._playback_duration_ms
        estimated_ms = spotify_ms
        if (
            spotify_ms is not None
            and self._playback_is_playing
            and self._playback_polled_at is not None
        ):
            elapsed_ms = max(0.0, now - self._playback_polled_at) * 1000.0
            estimated_ms = int(spotify_ms + elapsed_ms)
            if duration_ms is not None and duration_ms >= 0:
                estimated_ms = min(duration_ms, estimated_ms)
        return {
            "spotify_progress_ms": spotify_ms,
            "estimated_progress_ms": estimated_ms,
            "duration_ms": duration_ms,
            "is_playing": self._playback_is_playing,
        }

    def _elapsed_since_success_locked(self, now: float) -> float:
        if self._last_success_at is not None:
            return max(0.0, now - self._last_success_at)
        if self._buffer_start_at is not None:
            return max(0.0, now - self._buffer_start_at)
        return 0.0

    def _snapshot_initial_state_locked(self, now: float) -> dict[str, Any]:
        elapsed_last = (
            round(max(0.0, now - self._last_success_at), 4)
            if self._last_success_at is not None
            else None
        )
        elapsed_buf = (
            round(max(0.0, now - self._buffer_start_at), 4)
            if self._buffer_start_at is not None
            else None
        )
        return {
            "lazy_opened": True,
            "current_index": self._current_index,
            "buffer": self._agent.asr_buffer,
            "last_success_normalized": self._last_success_normalized,
            "elapsed_since_last_success": elapsed_last,
            "elapsed_since_buffer_start": elapsed_buf,
        }

    def restore_state(
        self,
        *,
        current_index: int = -1,
        buffer: str = "",
        last_success_normalized: str = "",
        elapsed_since_last_success: float | None = None,
        elapsed_since_buffer_start: float | None = None,
    ) -> None:
        """Restore mid-song synchronization state (e.g. from a lazy-opened trace)."""
        with self._lock:
            now = self._clock()
            idx = int(current_index)
            if idx < -1:
                idx = -1
            elif self._lyrics and idx >= len(self._lyrics):
                idx = len(self._lyrics) - 1
            self._current_index = idx
            self._agent.asr_buffer = str(buffer or "")
            self._last_success_normalized = str(last_success_normalized or "")
            self._last_success_at = (
                now - float(elapsed_since_last_success)
                if elapsed_since_last_success is not None
                else None
            )
            self._buffer_start_at = (
                now - float(elapsed_since_buffer_start)
                if elapsed_since_buffer_start is not None
                else None
            )

    def close_trace_session(
        self, reason: str = "stopped", *, clear_pending: bool = False
    ) -> None:
        """Close the active trace session if a trace recorder is attached."""
        if self._trace_recorder is None:
            return
        with self._lock:
            now = self._clock()
            self._trace_recorder.close_session(
                reason, clear_pending=clear_pending, event_time=now
            )

    def reset_with_lyrics(
        self,
        lyrics_lines: list[dict[str, Any]],
        *,
        track_meta: dict[str, Any] | None = None,
    ) -> None:
        """Replace active lyrics and reset synchronization state."""
        with self._lock:
            now = self._clock()
            self._lyrics = list(lyrics_lines)
            self._current_index = -1
            self._pending_scroll_targets = []
            self._pending_highlight_index = -1
            self._agent.reset()
            self._last_success_normalized = ""
            self._last_success_at = None
            self._buffer_start_at = None

            new_song_id = (track_meta or {}).get("song_id")
            if not self._lyrics or (
                new_song_id
                and self._playback_song_id
                and new_song_id != self._playback_song_id
            ):
                self._playback_song_id = new_song_id if self._lyrics else None
                self._playback_progress_ms = None
                self._playback_duration_ms = None
                self._playback_polled_at = None
                self._playback_is_playing = False

            if self._trace_recorder is not None:
                if self._lyrics:
                    asr_cfg = {
                        "model": config.ASR_MODEL_SIZE,
                        "device": config.ASR_DEVICE,
                        "block_seconds": config.ASR_BLOCK_SECONDS,
                        "step_seconds": config.ASR_BLOCK_SECONDS / 2,
                        "language": config.ASR_LANGUAGE,
                    }
                    self._trace_recorder.start_session(
                        track_meta or {},
                        list(self._lyrics),
                        asr_config=asr_cfg,
                        matcher_config=self.get_matcher_config(),
                        event_time=now,
                    )
                else:
                    self._trace_recorder.close_session(
                        "lyrics_cleared", clear_pending=True, event_time=now
                    )

    def get_locator_index_for_lyric(self, index: int) -> int:
        """Return DOM/line locator index for a given lyric index, or -1 if invalid."""
        with self._lock:
            if 0 <= index < len(self._lyrics):
                return int(self._lyrics[index].get("locator_index", index))
            return -1

    def get_buffer_length(self) -> int:
        """Return current length of the normalized ASR sliding buffer."""
        with self._lock:
            return len(self._agent.asr_buffer)

    def get_current_index(self) -> int:
        """Return the current matched/jumped lyric index, or -1 if none."""
        with self._lock:
            return self._current_index

    def apply_manual_jump(self, lyric_index: int) -> dict[str, Any] | None:
        """Jump synchronization anchor to a user-selected lyric line."""
        with self._lock:
            if lyric_index < 0 or lyric_index >= len(self._lyrics):
                return None

            now = self._clock()
            initial_state = self._snapshot_initial_state_locked(now)
            from_index = self._current_index
            buffer_before = self._agent.asr_buffer
            playback = self._snapshot_playback_progress_locked(now)

            self._current_index = lyric_index
            self._pending_scroll_targets = []
            self._pending_highlight_index = lyric_index
            self._agent.reset()
            self._last_success_normalized = ""
            self._last_success_at = None
            self._buffer_start_at = None

            current_line_no = lyric_index + 1
            current_line_text = self._lyrics[lyric_index]["original"]
            next_compare_index = lyric_index + 1
            next_line_no = None
            next_line_text = ""
            if next_compare_index < len(self._lyrics):
                next_line_no = next_compare_index + 1
                next_line_text = self._lyrics[next_compare_index]["original"]

            result = {
                "lyric_index": lyric_index,
                "current_line_no": current_line_no,
                "current_line_text": current_line_text,
                "next_line_no": next_line_no,
                "next_line_text": next_line_text,
            }

            if (
                self._trace_recorder is not None
                and self._trace_recorder.is_enabled()
            ):
                self._trace_recorder.record_manual_jump(
                    {
                        "playback": playback,
                        "from_index": from_index,
                        "to_index": lyric_index,
                        "current_line_text": result["current_line_text"],
                        "buffer_before": buffer_before,
                    },
                    event_time=now,
                    initial_state=initial_state,
                )

        if self._emit_events:
            events.bus.emit(
                "lyric_highlighted",
                {
                    "index": lyric_index,
                    "score": 1.0,
                    "recognized_text": "",
                    "is_recovery": False,
                    "manual": True,
                },
            )
        return result

    @staticmethod
    def _make_step_trace(
        playback: dict[str, Any],
        asr_payload: dict[str, Any],
        normalized_recognized: str,
        current_index: int,
        buffer_text: str,
        elapsed_since_success: float,
        decision: str = "",
    ) -> dict[str, Any]:
        return {
            "playback": playback,
            "asr": asr_payload,
            "normalized_recognized": normalized_recognized,
            "state_before": {
                "current_index": current_index,
                "buffer": buffer_text,
                "elapsed_since_success": round(elapsed_since_success, 4),
            },
            "buffer_after_append": buffer_text,
            "trimmed": False,
            "buffer_after_trim": buffer_text,
            "trim_target_length": None,
            "phase1_lookahead_lines": None,
            "phase1_range": None,
            "phase2_range": None,
            "candidates": [],
            "decision": decision,
            "matched_index": None,
            "display_index": None,
            "best_score": None,
            "threshold": None,
            "is_recovery": False,
            "current_index_after": current_index,
        }

    def _evaluate_locked(
        self,
        now: float,
        cleaned_text: str,
        normalized_recognized: str,
        asr_payload: dict[str, Any],
        build_trace: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        playback = (
            self._snapshot_playback_progress_locked(now) if build_trace else {}
        )
        cur_idx_before = self._current_index
        buffer_before = self._agent.asr_buffer
        elapsed_before = self._elapsed_since_success_locked(now)

        def _empty_result(decision: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
            if not build_trace:
                return None, {}
            return None, self._make_step_trace(
                playback,
                asr_payload,
                normalized_recognized,
                cur_idx_before,
                buffer_before,
                elapsed_before,
                decision=decision,
            )

        if not self._lyrics:
            return _empty_result("ignored_no_lyrics")

        duplicate_ttl = self._effective_duplicate_ttl()
        if self._last_success_normalized and self._last_success_at is not None:
            within_ttl = now - self._last_success_at <= duplicate_ttl
            is_duplicate = normalized_recognized in self._last_success_normalized
            if within_ttl and is_duplicate:
                return _empty_result("skipped_duplicate_ttl")

        start_index = self._current_index + 1
        if start_index >= len(self._lyrics):
            return _empty_result("end_of_lyrics")

        if not self._agent.asr_buffer:
            self._buffer_start_at = now

        self._agent.append(normalized_recognized)
        buffer_after_append = self._agent.asr_buffer
        target_length = len(self._lyrics[start_index]["normalized"])
        trimmed = self._agent.trim_if_needed(target_length)
        buffer_after_trim = self._agent.asr_buffer

        elapsed_since_success = self._elapsed_since_success_locked(now)
        max_lookahead = self._effective_lookahead_lines()
        phase1_threshold = self._effective_next_line_threshold()
        recovery_lookahead = self._effective_recovery_lookahead_lines()
        recovery_threshold = self._effective_recovery_threshold()

        phase1_lookahead_lines = max(
            1,
            min(
                max_lookahead,
                int(elapsed_since_success / self._lookahead_seconds_per_line),
            ),
        )

        # 階段一：常規推進 (時間越久，搜尋範圍越大)
        end_index_phase1 = min(
            start_index + phase1_lookahead_lines,
            len(self._lyrics) - 1,
        )
        best_index: int | None = None
        best_score = 0.0
        candidates: list[dict[str, Any]] = []

        for idx in range(start_index, end_index_phase1 + 1):
            target_normalized = self._lyrics[idx]["normalized"]
            if build_trace:
                details = self._agent.score_detailed(target_normalized)
                score = details["final_score"]
                candidates.append(
                    {
                        "index": idx,
                        "phase": 1,
                        "target_normalized": target_normalized,
                        "coverage": round(details["coverage"], 4),
                        "raw_partial_score": round(
                            details["raw_partial_score"], 4
                        ),
                        "partial_score": round(details["partial_score"], 4),
                        "ratio_score": round(details["ratio_score"], 4),
                        "score": round(score, 4),
                    }
                )
            else:
                score = self._agent.score(target_normalized)
            if score > best_score:
                best_score = score
                best_index = idx

        # 階段二：迷失恢復機制 (階段一達到上限後才啟用)
        is_recovery = False
        phase2_range: list[int] | None = None
        if (
            best_score < phase1_threshold
            and phase1_lookahead_lines >= max_lookahead
        ):
            start_index_phase2 = end_index_phase1 + 1
            end_index_phase2 = min(
                self._current_index + recovery_lookahead,
                len(self._lyrics) - 1,
            )

            if start_index_phase2 <= end_index_phase2:
                phase2_range = [start_index_phase2, end_index_phase2]
                for idx in range(start_index_phase2, end_index_phase2 + 1):
                    target_normalized = self._lyrics[idx]["normalized"]
                    score = self._agent.ratio_score(target_normalized)
                    if build_trace:
                        candidates.append(
                            {
                                "index": idx,
                                "phase": 2,
                                "target_normalized": target_normalized,
                                "ratio_score": round(score, 4),
                                "score": round(score, 4),
                            }
                        )
                    if score > best_score and score >= recovery_threshold:
                        best_score = score
                        best_index = idx
                        is_recovery = True

        threshold_to_pass = (
            recovery_threshold if is_recovery else phase1_threshold
        )

        step_trace: dict[str, Any] = {}
        if build_trace:
            step_trace = self._make_step_trace(
                playback,
                asr_payload,
                normalized_recognized,
                cur_idx_before,
                buffer_before,
                elapsed_since_success,
            )
            step_trace["buffer_after_append"] = buffer_after_append
            step_trace["trimmed"] = bool(trimmed)
            step_trace["buffer_after_trim"] = buffer_after_trim
            step_trace["trim_target_length"] = target_length
            step_trace["phase1_lookahead_lines"] = phase1_lookahead_lines
            step_trace["phase1_range"] = [start_index, end_index_phase1]
            step_trace["phase2_range"] = phase2_range
            step_trace["candidates"] = candidates
            step_trace["threshold"] = round(threshold_to_pass, 4)
            step_trace["best_score"] = (
                round(best_score, 4) if best_index is not None else None
            )
            step_trace["is_recovery"] = is_recovery

        if best_index is None or best_score < threshold_to_pass:
            if build_trace:
                step_trace["decision"] = "no_match"
                step_trace["current_index_after"] = self._current_index
            return None, step_trace

        target_original = self._lyrics[best_index]["original"]
        target_normalized = self._lyrics[best_index]["normalized"]
        buffer_length = len(buffer_after_trim)

        next_display_index = min(best_index + 1, len(self._lyrics) - 1)
        locator_index = int(
            self._lyrics[next_display_index].get(
                "locator_index", next_display_index
            )
        )
        self._pending_scroll_targets.append(locator_index)
        self._pending_highlight_index = next_display_index
        self._current_index = best_index
        self._last_success_normalized = target_normalized
        self._last_success_at = now
        self._buffer_start_at = None
        self._agent.reset()

        if build_trace:
            step_trace["decision"] = (
                "matched_phase2_recovery" if is_recovery else "matched_phase1"
            )
            step_trace["matched_index"] = best_index
            step_trace["display_index"] = next_display_index
            step_trace["current_index_after"] = self._current_index

        match_payload = {
            "matched_index": best_index,
            "display_index": next_display_index,
            "score": best_score,
            "recognized_text": cleaned_text,
            "target_original": target_original,
            "target_normalized": target_normalized,
            "buffer_length": buffer_length,
            "is_recovery": is_recovery,
        }
        return match_payload, step_trace

    def evaluate_recognized_text(
        self,
        recognized_text: str,
        *,
        asr_meta: dict[str, Any] | None = None,
        collect_trace: bool = True,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Match recognized text and return ``(match_payload, step_trace)``."""
        cleaned_text = (recognized_text or "").strip()
        asr_payload = (
            dict(asr_meta)
            if asr_meta is not None
            else {"text": cleaned_text}
        )
        normalized_recognized = (
            normalize_match_text(normalize_to_hiragana(cleaned_text))
            if cleaned_text
            else ""
        )

        match_payload: dict[str, Any] | None = None
        step_trace: dict[str, Any] = {}

        with self._lock:
            now = self._clock()
            should_record = bool(
                self._trace_recorder is not None
                and self._trace_recorder.is_enabled()
            )
            build_trace = bool(collect_trace or should_record)
            initial_state = (
                self._snapshot_initial_state_locked(now)
                if should_record
                else None
            )

            if not cleaned_text or not normalized_recognized:
                empty_decision = (
                    "asr_empty_or_filtered"
                    if (not cleaned_text and asr_meta is not None)
                    else "ignored_empty"
                )
                if build_trace:
                    step_trace = self._make_step_trace(
                        self._snapshot_playback_progress_locked(now),
                        asr_payload,
                        "",
                        self._current_index,
                        self._agent.asr_buffer,
                        self._elapsed_since_success_locked(now),
                        decision=empty_decision,
                    )
                if should_record and asr_meta is not None:
                    assert self._trace_recorder is not None
                    self._trace_recorder.record_asr_step(
                        step_trace,
                        event_time=now,
                        initial_state=initial_state,
                    )
                return None, step_trace

            match_payload, step_trace = self._evaluate_locked(
                now,
                cleaned_text,
                normalized_recognized,
                asr_payload,
                build_trace,
            )
            if should_record:
                assert self._trace_recorder is not None
                self._trace_recorder.record_asr_step(
                    step_trace,
                    event_time=now,
                    initial_state=initial_state,
                )

        if match_payload is not None and self._emit_events:
            events.bus.emit(
                "lyric_highlighted",
                {
                    "index": match_payload["display_index"],
                    "score": match_payload["score"],
                    "recognized_text": match_payload["recognized_text"],
                    "is_recovery": match_payload["is_recovery"],
                    "manual": False,
                },
            )
        return match_payload, step_trace

    def process_recognized_text(
        self,
        recognized_text: str,
        *,
        asr_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Match recognized Japanese text against upcoming lyrics and queue updates."""
        collect_trace = bool(
            self._trace_recorder is not None
            and self._trace_recorder.is_enabled()
        )
        match_payload, _ = self.evaluate_recognized_text(
            recognized_text, asr_meta=asr_meta, collect_trace=collect_trace
        )
        return match_payload

    def has_pending_updates(self) -> bool:
        """Check whether there are queued scroll or highlight updates."""
        with self._lock:
            return (
                bool(self._pending_scroll_targets)
                or self._pending_highlight_index >= 0
            )

    def consume_pending_updates(self) -> tuple[list[int], int]:
        """Atomically pop queued scroll locator indices and highlight index."""
        with self._lock:
            target_indices = list(self._pending_scroll_targets)
            self._pending_scroll_targets = []
            highlight_index = self._pending_highlight_index
            self._pending_highlight_index = -1
            if highlight_index < 0 and target_indices:
                highlight_index = self._current_index
            return target_indices, highlight_index
