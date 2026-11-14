"""Japanese text normalization and two-phase fuzzy lyric synchronization."""

import re
import threading
import time
from typing import Any

from thefuzz import fuzz

from utaten_finder.core import config, events

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


def calculate_similarity_normalized(na, nb, min_coverage_ratio=0.4):
    if not na or not nb:
        return 0.0

    coverage = _length_coverage_ratio(na, nb)

    # 1. partial_ratio: 解決 Whisper 只聽到一半的問題
    partial_score = fuzz.partial_ratio(na, nb) / 100.0
    if coverage < min_coverage_ratio:
        partial_score *= coverage / min_coverage_ratio

    # 2. ratio: 解決 Whisper 聽錯少數假名的問題 (基於 Levenshtein 編輯距離)
    ratio_score = fuzz.ratio(na, nb) / 100.0

    return max(partial_score, ratio_score)


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

    def __init__(self, lookahead_seconds_per_line: float = 1.5) -> None:
        self._lock = threading.Lock()
        self._lyrics: list[dict[str, Any]] = []
        self._current_index = -1
        self._pending_scroll_targets: list[int] = []
        self._pending_highlight_index = -1
        self._agent = LyricsAgent()
        self._last_success_normalized = ""
        self._last_success_at = 0.0
        self._buffer_start_at = 0.0
        self._lookahead_seconds_per_line = lookahead_seconds_per_line

    def reset_with_lyrics(self, lyrics_lines: list[dict[str, Any]]) -> None:
        """Replace active lyrics and reset synchronization state."""
        with self._lock:
            self._lyrics = list(lyrics_lines)
            self._current_index = -1
            self._pending_scroll_targets = []
            self._pending_highlight_index = -1
            self._agent.reset()
            self._last_success_normalized = ""
            self._last_success_at = 0.0
            self._buffer_start_at = 0.0

    def get_locator_index_for_lyric(self, index: int) -> int:
        """Return DOM/line locator index for a given lyric index, or -1 if invalid."""
        with self._lock:
            if 0 <= index < len(self._lyrics):
                return int(self._lyrics[index].get("locator_index", index))
            return -1

    def apply_manual_jump(self, lyric_index: int) -> dict[str, Any] | None:
        """Jump synchronization anchor to a user-selected lyric line."""
        with self._lock:
            if lyric_index < 0 or lyric_index >= len(self._lyrics):
                return None

            self._current_index = lyric_index
            self._pending_scroll_targets = []
            self._pending_highlight_index = lyric_index
            self._agent.reset()
            self._last_success_normalized = ""
            self._last_success_at = 0.0
            self._buffer_start_at = 0.0

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

    def process_recognized_text(self, recognized_text: str) -> dict[str, Any] | None:
        """Match recognized Japanese text against upcoming lyrics and queue updates."""
        recognized_text = (recognized_text or "").strip()
        if not recognized_text:
            return None

        normalized_recognized = normalize_match_text(
            normalize_to_hiragana(recognized_text)
        )
        if not normalized_recognized:
            return None

        match_payload: dict[str, Any] | None = None
        with self._lock:
            if not self._lyrics:
                return None

            now = time.monotonic()
            duplicate_ttl = float(config.ASR_BLOCK_SECONDS)
            if self._last_success_normalized:
                within_ttl = now - self._last_success_at <= duplicate_ttl
                is_duplicate = normalized_recognized in self._last_success_normalized
                if within_ttl and is_duplicate:
                    return None

            start_index = self._current_index + 1
            if start_index >= len(self._lyrics):
                return None

            if not self._agent.asr_buffer:
                self._buffer_start_at = now

            self._agent.append(normalized_recognized)
            target_length = len(self._lyrics[start_index]["normalized"])
            self._agent.trim_if_needed(target_length)

            elapsed_since_success = 0.0
            if self._last_success_at > 0.0:
                elapsed_since_success = max(0.0, now - self._last_success_at)
            elif self._buffer_start_at > 0.0:
                elapsed_since_success = max(0.0, now - self._buffer_start_at)

            phase1_lookahead_lines = max(
                1,
                min(
                    config.FUZZY_LOOKAHEAD_LINES,
                    int(elapsed_since_success / self._lookahead_seconds_per_line),
                ),
            )

            # 階段一：常規推進 (時間越久，搜尋範圍越大)
            end_index_phase1 = min(
                start_index + phase1_lookahead_lines, len(self._lyrics) - 1
            )
            best_index = None
            best_score = 0.0

            for idx in range(start_index, end_index_phase1 + 1):
                target_normalized = self._lyrics[idx]["normalized"]
                score = self._agent.score(target_normalized)
                if score > best_score:
                    best_score = score
                    best_index = idx

            # 階段二：迷失恢復機制 (階段一達到上限後才啟用)
            is_recovery = False
            if (
                best_score < config.FUZZY_NEXT_LINE_THRESHOLD
                and phase1_lookahead_lines >= config.FUZZY_LOOKAHEAD_LINES
            ):
                start_index_phase2 = end_index_phase1 + 1
                end_index_phase2 = min(
                    self._current_index + config.RECOVERY_LOOKAHEAD_LINES,
                    len(self._lyrics) - 1,
                )

                if start_index_phase2 <= end_index_phase2:
                    for idx in range(start_index_phase2, end_index_phase2 + 1):
                        target_normalized = self._lyrics[idx]["normalized"]
                        score = self._agent.ratio_score(target_normalized)
                        if score > best_score and score >= config.RECOVERY_THRESHOLD:
                            best_score = score
                            best_index = idx
                            is_recovery = True

            threshold_to_pass = (
                config.RECOVERY_THRESHOLD
                if is_recovery
                else config.FUZZY_NEXT_LINE_THRESHOLD
            )

            if best_index is None or best_score < threshold_to_pass:
                return None

            target_original = self._lyrics[best_index]["original"]
            target_normalized = self._lyrics[best_index]["normalized"]
            buffer_length = len(self._agent.asr_buffer)

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
            self._last_success_at = time.monotonic()
            self._buffer_start_at = 0.0
            self._agent.reset()

            match_payload = {
                "matched_index": best_index,
                "display_index": next_display_index,
                "score": best_score,
                "recognized_text": recognized_text,
                "target_original": target_original,
                "target_normalized": target_normalized,
                "buffer_length": buffer_length,
                "is_recovery": is_recovery,
            }

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
