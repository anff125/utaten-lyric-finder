"""Japanese text normalization and multi-mode fuzzy lyric synchronization."""

from dataclasses import dataclass
import math
import re
import threading
import time
from typing import Any, Callable

from rapidfuzz import fuzz

from utaten_finder.core import config, events
from utaten_finder.lyrics.phonology import (
    ContinuousLyricsTimeline,
    normalize_singing_phonology,
    score_similarity_weighted_detailed,
    smith_waterman_local_peaks,
    weighted_indel_ratio,
)
from utaten_finder.lyrics.trace import MatchTraceRecorder, sanitize_trace_slug

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


UNCONDITIONAL_HALLUCINATIONS: tuple[str, ...] = (
    "ご視聴ありがとうございました",
    "視聴ありがとうございました",
    "チャンネル登録",
    "高評価",
    "よろしくお願いします",
    "字幕作成",
    "サブタイトル",
)

INTERLUDE_FILLER_HALLUCINATIONS: frozenset[str] = frozenset(
    {
        "ごめん",
        "ごめんなさい",
        "ありがとう",
        "ありがとうございました",
        "おやすみ",
        "おやすみなさい",
        "お疲れ様でした",
        "おつかれさまでした",
        "字幕",
        "拍手",
        "音楽",
        "歌詞",
        "me",
        "you",
        "お",
        "おー",
        "うん",
        "うー",
        "あ",
        "あー",
        "え",
        "えー",
        "ん",
        "はい",
        "ええ",
    }
)

_REPETITIVE_LOOP_RE = re.compile(r"(.{1,6}?)\1{2,}")
_PUNCT_STRIP_KEEP_LONG_RE = re.compile(r"[\s\u3000、。,.!！?？『』「」\\〜~…-]")


def _opt_finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def has_repetitive_token_loop(text: str) -> bool:
    """Detect 3+ consecutive repetitions of a 1-6 char token dominating the text."""
    norm = normalize_match_text(text)
    if len(norm) < 4:
        return False
    covered = sum(m.end() - m.start() for m in _REPETITIVE_LOOP_RE.finditer(norm))
    return (covered / len(norm)) >= 0.60


def _is_token_in_lyrics_corpus(token: str, corpus_lower: str) -> bool:
    if not token or not corpus_lower:
        return False
    if len(token) <= 1:
        return f"\n{token}\n" in f"\n{corpus_lower}\n"
    if token.isascii() and any(ch.isalpha() for ch in token):
        if re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", corpus_lower):
            return True
        return len(token) >= 6 and token in corpus_lower
    return token in corpus_lower


def filter_asr_segments(
    segments: list[dict[str, Any]],
    *,
    lyrics_corpus: str | None = None,
    min_avg_logprob: float = -0.90,
    max_no_speech_prob: float = 0.60,
    max_compression_ratio: float = 1.35,
    filter_interlude_fillers: bool = True,
) -> dict[str, Any]:
    """Filter low-confidence Whisper segments and instrumental hallucinations."""
    raw_text_before_filter = "".join(
        str(seg.get("text") or "")
        for seg in segments
        if isinstance(seg, dict)
    ).strip()
    corpus_lower = (lyrics_corpus or "").lower()

    hallucinations_removed: list[str] = []
    kept_segments: list[dict[str, Any]] = []
    filtered_segments: list[dict[str, Any]] = []

    def _record_removed_phrase(phrase: str) -> None:
        if phrase and phrase not in hallucinations_removed:
            hallucinations_removed.append(phrase)

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_text = str(seg.get("text") or "")
        cleaned = seg_text
        for phrase in UNCONDITIONAL_HALLUCINATIONS:
            if phrase in cleaned:
                _record_removed_phrase(phrase)
                cleaned = cleaned.replace(phrase, "")

        cleaned_stripped = cleaned.strip()
        if not cleaned_stripped:
            if seg_text.strip():
                filtered_segments.append({**seg, "reason": "hallucination_phrase"})
            continue

        avg_logprob = _opt_finite_float(seg.get("avg_logprob"))
        no_speech_prob = _opt_finite_float(seg.get("no_speech_prob"))
        compression_ratio = _opt_finite_float(seg.get("compression_ratio"))

        drop_reason = ""
        if no_speech_prob is not None and (
            no_speech_prob > max_no_speech_prob
            or (
                no_speech_prob > 0.45
                and avg_logprob is not None
                and avg_logprob < -0.50
            )
        ):
            drop_reason = "high_no_speech_prob"
        elif avg_logprob is not None and avg_logprob < min_avg_logprob:
            drop_reason = "low_avg_logprob"
        elif (
            compression_ratio is not None
            and compression_ratio > max_compression_ratio
        ):
            drop_reason = "high_compression_ratio"
        else:
            punct_stripped = _PUNCT_STRIP_KEEP_LONG_RE.sub(
                "", cleaned_stripped
            ).lower()
            norm_seg = normalize_match_text(cleaned_stripped)
            hira_seg = normalize_match_text(
                normalize_to_hiragana(cleaned_stripped)
            )
            cand_keys = [
                key
                for key in (
                    cleaned_stripped.lower(),
                    punct_stripped,
                    norm_seg,
                    hira_seg,
                )
                if key
            ]
            present_in_lyrics = any(
                _is_token_in_lyrics_corpus(key, corpus_lower)
                for key in cand_keys
            )
            if (
                has_repetitive_token_loop(cleaned_stripped)
                and not present_in_lyrics
            ):
                drop_reason = "repetitive_loop"
                _record_removed_phrase(cleaned_stripped)
            elif filter_interlude_fillers and not present_in_lyrics:
                is_filler = any(
                    key in INTERLUDE_FILLER_HALLUCINATIONS
                    for key in cand_keys
                )
                is_low_conf_single = (
                    len(norm_seg) == 1
                    and avg_logprob is not None
                    and avg_logprob < -0.45
                )
                if is_filler or is_low_conf_single:
                    drop_reason = "interlude_filler"
                    _record_removed_phrase(cleaned_stripped)

        if drop_reason:
            filtered_segments.append(
                {**seg, "text": cleaned_stripped, "reason": drop_reason}
            )
            continue

        kept_segments.append({**seg, "text": cleaned})

    result_text = "".join(str(seg.get("text") or "") for seg in kept_segments)
    for phrase in UNCONDITIONAL_HALLUCINATIONS:
        if phrase in result_text:
            _record_removed_phrase(phrase)
            result_text = result_text.replace(phrase, "")
    result_text = result_text.strip()

    return {
        "text": result_text,
        "raw_text_before_filter": raw_text_before_filter,
        "hallucinations_removed": hallucinations_removed,
        "kept_segments": kept_segments,
        "filtered_segments": filtered_segments,
    }


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
) -> dict[str, Any]:
    """Return detailed similarity breakdown matching calculate_similarity_normalized."""
    if not na or not nb:
        return {
            "coverage": 0.0,
            "raw_partial_score": 0.0,
            "partial_score": 0.0,
            "ratio_score": 0.0,
            "final_score": 0.0,
            "src_start": 0,
            "src_end": 0,
            "dest_start": 0,
            "dest_end": 0,
        }

    coverage = _length_coverage_ratio(na, nb)

    # 1. partial_ratio: 解決 Whisper 只聽到一半的問題，並以 alignment 定位已匹配片段
    alignment = fuzz.partial_ratio_alignment(na, nb)
    if alignment is not None:
        raw_partial_score = alignment.score / 100.0
        src_start = int(alignment.src_start)
        src_end = int(alignment.src_end)
        dest_start = int(alignment.dest_start)
        dest_end = int(alignment.dest_end)
    else:
        raw_partial_score = 0.0
        src_start = 0
        src_end = 0
        dest_start = 0
        dest_end = 0

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
        "src_start": src_start,
        "src_end": src_end,
        "dest_start": dest_start,
        "dest_end": dest_end,
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

    def consume_prefix(self, end_index: int) -> str:
        """Drop consumed prefix up to ``end_index`` and keep any unconsumed tail."""
        if end_index <= 0 or end_index >= len(self.asr_buffer):
            self.asr_buffer = ""
            return ""
        self.asr_buffer = self.asr_buffer[end_index:]
        return self.asr_buffer

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


_VALID_DISPLAY_MODES = frozenset({"adaptive", "next", "current"})
_VALID_ALIGNMENT_MODES = frozenset({"line", "beam"})


@dataclass(frozen=True)
class _BeamHypothesis:
    pos: int
    score: float
    last_adv_time: float | None
    last_line: int


class LyricSyncEngine:
    """Thread-safe multi-mode lyric synchronization state machine."""

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
        retain_tail: bool = True,
        short_line_guard: bool = True,
        short_line_max_len: int = 4,
        smart_duplicate_ttl: bool = True,
        display_mode: str = "adaptive",
        early_display_progress_threshold: float = 0.75,
        alignment_mode: str = "beam",
        phonological_norm: bool = True,
        phono_particles: bool = True,
        phono_long_vowels: bool = True,
        phono_drop_sokuon: bool = True,
        weighted_dakuten: bool = False,
        dakuten_cost: float = 0.35,
        adjacent_pair: bool = False,
        pair_threshold: float | None = None,
        beam_width: int = 4,
        beam_min_sim: float = 0.73,
        beam_dakuten_score: float = 1.2,
        beam_forward_bonus: float = 3.5,
        beam_jump_penalty_per_char: float = 0.10,
        beam_jump_penalty_per_line: float = 2.0,
        beam_max_jump_penalty: float = 8.0,
        beam_recovery_penalty: float = 6.5,
        beam_decay: float = 0.78,
        beam_back_slack: int = 16,
        beam_max_buffer_chars: int = 32,
        beam_buffer_stale_seconds: float = 6.5,
        asr_segment_filter: bool = False,
        asr_min_avg_logprob: float = -0.90,
        asr_max_no_speech_prob: float = 0.60,
        asr_max_compression_ratio: float = 1.35,
        position_prior_enabled: bool = True,
        position_prior_tolerance: float = 0.15,
        position_prior_weight: float = 0.45,
        position_prior_floor: float = 0.60,
        seek_detection_enabled: bool = True,
        seek_rewind_threshold_ms: int = 3500,
        seek_forward_threshold_ms: int = 12000,
        seek_window_ratio_radius: float = 0.35,
        emit_events: bool = True,
    ) -> None:
        if display_mode not in _VALID_DISPLAY_MODES:
            raise ValueError(
                f"display_mode 必須為 {sorted(_VALID_DISPLAY_MODES)} 之一，收到: {display_mode!r}"
            )
        if alignment_mode not in _VALID_ALIGNMENT_MODES:
            raise ValueError(
                f"alignment_mode 必須為 {sorted(_VALID_ALIGNMENT_MODES)} 之一，收到: {alignment_mode!r}"
            )
        self._lock = threading.Lock()
        self._lyrics: list[dict[str, Any]] = []
        self._match_keys: list[str] = []
        self._timeline = ContinuousLyricsTimeline([])
        self._current_index = -1
        self._pending_scroll_targets: list[int] = []
        self._pending_highlight_index = -1
        self._agent = LyricsAgent(
            min_coverage_ratio=min_coverage_ratio,
            max_buffer_ratio=max_buffer_ratio,
        )
        self._last_success_normalized = ""
        self._last_success_match_key = ""
        self._last_success_recognized = ""
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
        self._retain_tail = bool(retain_tail)
        self._short_line_guard = bool(short_line_guard)
        self._short_line_max_len = int(short_line_max_len)
        self._smart_duplicate_ttl = bool(smart_duplicate_ttl)
        self._display_mode = display_mode
        self._early_display_progress_threshold = float(
            early_display_progress_threshold
        )
        self._alignment_mode = alignment_mode
        self._phonological_norm = bool(phonological_norm)
        self._phono_particles = bool(phono_particles)
        self._phono_long_vowels = bool(phono_long_vowels)
        self._phono_drop_sokuon = bool(phono_drop_sokuon)
        self._weighted_dakuten = bool(weighted_dakuten)
        self._dakuten_cost = float(dakuten_cost)
        self._adjacent_pair = bool(adjacent_pair)
        self._pair_threshold = (
            float(pair_threshold) if pair_threshold is not None else None
        )
        self._beam_width = max(1, int(beam_width))
        self._beam_min_sim = float(beam_min_sim)
        self._beam_dakuten_score = float(beam_dakuten_score)
        self._beam_forward_bonus = float(beam_forward_bonus)
        self._beam_jump_penalty_per_char = float(beam_jump_penalty_per_char)
        self._beam_jump_penalty_per_line = float(beam_jump_penalty_per_line)
        self._beam_max_jump_penalty = float(beam_max_jump_penalty)
        self._beam_recovery_penalty = float(beam_recovery_penalty)
        self._beam_decay = float(beam_decay)
        self._beam_back_slack = max(0, int(beam_back_slack))
        self._beam_max_buffer_chars = max(8, int(beam_max_buffer_chars))
        self._beam_buffer_stale_seconds = float(beam_buffer_stale_seconds)
        self._beam: list[_BeamHypothesis] = [
            _BeamHypothesis(pos=0, score=0.0, last_adv_time=None, last_line=-1)
        ]
        self._beam_buffer_at: float | None = None
        self._asr_segment_filter = bool(asr_segment_filter)
        self._asr_min_avg_logprob = float(asr_min_avg_logprob)
        self._asr_max_no_speech_prob = float(asr_max_no_speech_prob)
        self._asr_max_compression_ratio = float(asr_max_compression_ratio)
        self._position_prior_enabled = bool(position_prior_enabled)
        self._position_prior_tolerance = max(0.0, float(position_prior_tolerance))
        self._position_prior_weight = max(0.0, float(position_prior_weight))
        self._position_prior_floor = max(0.0, min(1.0, float(position_prior_floor)))
        self._seek_detection_enabled = bool(seek_detection_enabled)
        self._seek_rewind_threshold_ms = max(500, int(seek_rewind_threshold_ms))
        self._seek_forward_threshold_ms = max(1000, int(seek_forward_threshold_ms))
        self._seek_window_ratio_radius = max(
            0.05, min(1.0, float(seek_window_ratio_radius))
        )
        self._lyrics_corpus = ""
        self._track_slug = ""
        self._track_song_id = ""
        self._emit_events = bool(emit_events)

        self._playback_song_id: str | None = None
        self._playback_progress_ms: int | None = None
        self._playback_estimated_ms_at_poll: int | None = None
        self._playback_duration_ms: int | None = None
        self._playback_polled_at: float | None = None
        self._playback_is_playing = False
        self._repeated_line_flags: list[bool] = []
        self._seek_reanchor_active = False
        self._seek_type = ""
        self._seek_ref_index = -1
        self._pending_seek_event: dict[str, Any] | None = None

    def _to_match_key(self, normalized: str) -> str:
        if not normalized or not self._phonological_norm:
            return normalized
        return normalize_singing_phonology(
            normalized,
            normalize_particles=self._phono_particles,
            collapse_long_vowels=self._phono_long_vowels,
            drop_sokuon=self._phono_drop_sokuon,
        )

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
            "retain_tail": self._retain_tail,
            "short_line_guard": self._short_line_guard,
            "short_line_max_len": self._short_line_max_len,
            "smart_duplicate_ttl": self._smart_duplicate_ttl,
            "display_mode": self._display_mode,
            "early_display_progress_threshold": self._early_display_progress_threshold,
            "alignment_mode": self._alignment_mode,
            "phonological_norm": self._phonological_norm,
            "phono_particles": self._phono_particles,
            "phono_long_vowels": self._phono_long_vowels,
            "phono_drop_sokuon": self._phono_drop_sokuon,
            "weighted_dakuten": self._weighted_dakuten,
            "dakuten_cost": self._dakuten_cost,
            "adjacent_pair": self._adjacent_pair,
            "pair_threshold": self._pair_threshold,
            "beam_width": self._beam_width,
            "beam_min_sim": self._beam_min_sim,
            "beam_dakuten_score": self._beam_dakuten_score,
            "beam_forward_bonus": self._beam_forward_bonus,
            "beam_jump_penalty_per_char": self._beam_jump_penalty_per_char,
            "beam_jump_penalty_per_line": self._beam_jump_penalty_per_line,
            "beam_max_jump_penalty": self._beam_max_jump_penalty,
            "beam_recovery_penalty": self._beam_recovery_penalty,
            "beam_decay": self._beam_decay,
            "beam_back_slack": self._beam_back_slack,
            "beam_max_buffer_chars": self._beam_max_buffer_chars,
            "beam_buffer_stale_seconds": self._beam_buffer_stale_seconds,
            "asr_segment_filter": self._asr_segment_filter,
            "asr_min_avg_logprob": self._asr_min_avg_logprob,
            "asr_max_no_speech_prob": self._asr_max_no_speech_prob,
            "asr_max_compression_ratio": self._asr_max_compression_ratio,
            "position_prior_enabled": self._position_prior_enabled,
            "position_prior_tolerance": self._position_prior_tolerance,
            "position_prior_weight": self._position_prior_weight,
            "position_prior_floor": self._position_prior_floor,
            "seek_detection_enabled": self._seek_detection_enabled,
            "seek_rewind_threshold_ms": self._seek_rewind_threshold_ms,
            "seek_forward_threshold_ms": self._seek_forward_threshold_ms,
            "seek_window_ratio_radius": self._seek_window_ratio_radius,
        }

    def _clear_seek_state_locked(self) -> None:
        self._seek_reanchor_active = False
        self._seek_type = ""
        self._seek_ref_index = -1
        self._pending_seek_event = None

    @staticmethod
    def _build_repeated_line_flags(match_keys: list[str]) -> list[bool]:
        """Flag lyric lines whose text repeats or strongly overlaps another line."""
        n = len(match_keys)
        flags = [False] * n
        for i in range(n):
            ki = match_keys[i]
            if len(ki) < 3:
                continue
            for j in range(i + 1, n):
                if flags[i] and flags[j]:
                    continue
                kj = match_keys[j]
                if len(kj) < 3:
                    continue
                is_dup = ki == kj
                if not is_dup and len(ki) >= 4 and len(kj) >= 4:
                    is_dup = (
                        ki in kj
                        or kj in ki
                        or (fuzz.ratio(ki, kj) / 100.0) >= 0.78
                    )
                if not is_dup and min(len(ki), len(kj)) >= 6:
                    is_dup = (fuzz.partial_ratio(ki, kj) / 100.0) >= 0.88
                if is_dup:
                    flags[i] = True
                    flags[j] = True
        return flags

    def _is_repeated_candidate_locked(
        self, line_idx: int, matched_span_text: str = ""
    ) -> bool:
        if (
            0 <= line_idx < len(self._repeated_line_flags)
            and self._repeated_line_flags[line_idx]
        ):
            return True
        return (
            len(matched_span_text) >= 4
            and self._timeline.full_text.count(matched_span_text) > 1
        )

    @staticmethod
    def _current_playback_ratio_locked(playback: dict[str, Any]) -> float | None:
        est_ms = playback.get("estimated_progress_ms")
        if est_ms is None:
            est_ms = playback.get("spotify_progress_ms")
        dur_ms = playback.get("duration_ms")
        est_val = _opt_finite_float(est_ms)
        dur_val = _opt_finite_float(dur_ms)
        if est_val is None or dur_val is None or dur_val <= 0.0:
            return None
        return min(1.0, max(0.0, est_val / dur_val))

    def _compute_position_prior_locked(
        self,
        line_idx: int,
        playback_ratio: float | None,
        *,
        cand_ratio: float | None = None,
        is_repeated: bool = False,
        is_immediate_next: bool = False,
        in_recovery: bool = False,
        seek_active: bool = False,
    ) -> float:
        """Compute multiplicative soft position prior in [floor, 1.0] from playback ratio."""
        if not self._position_prior_enabled or playback_ratio is None:
            return 1.0
        if not is_repeated and not seek_active:
            return 1.0

        c_ratio = (
            cand_ratio
            if cand_ratio is not None
            else self._timeline.line_center_ratio(line_idx)
        )
        drift = abs(c_ratio - playback_ratio)
        eff_tol = (
            max(self._position_prior_tolerance, 0.22)
            if (is_immediate_next and not seek_active)
            else self._position_prior_tolerance
        )
        tie_slope = 0.08 if seek_active else 0.0
        if drift <= eff_tol:
            return max(self._position_prior_floor, 1.0 - tie_slope * drift)

        excess = drift - eff_tol
        base_weight = (
            self._position_prior_weight
            if is_repeated
            else (self._position_prior_weight * 0.35)
        )
        if is_repeated and (in_recovery or seek_active or not is_immediate_next):
            base_weight = max(base_weight, self._position_prior_weight * 1.2)
        return max(
            self._position_prior_floor,
            1.0 - (tie_slope * eff_tol) - base_weight * excess,
        )

    def _compute_seek_window_locked(
        self, playback_ratio: float | None, seek_type: str, ref_index: int
    ) -> tuple[int, int]:
        n_lines = len(self._lyrics)
        if n_lines <= 0:
            return (0, 0)
        if playback_ratio is None:
            return (0, n_lines - 1)

        est_line = self._timeline.line_for_ratio(playback_ratio)
        low_r = max(0.0, playback_ratio - self._seek_window_ratio_radius)
        high_r = min(1.0, playback_ratio + self._seek_window_ratio_radius)
        start_line = min(
            self._timeline.line_for_ratio(low_r), max(0, est_line - 12)
        )
        end_line = max(
            self._timeline.line_for_ratio(high_r), min(n_lines - 1, est_line + 12)
        )
        if seek_type == "rewind":
            if playback_ratio <= 0.25:
                start_line = 0
            if ref_index >= 0:
                start_line = min(start_line, ref_index)
        elif seek_type == "fast_forward" and ref_index >= 0:
            start_line = max(start_line, min(n_lines - 1, ref_index + 1))
            end_line = max(end_line, start_line)

        start_line = max(0, min(n_lines - 1, start_line))
        end_line = max(start_line, min(n_lines - 1, end_line))
        return (start_line, end_line)

    def _apply_playback_seek_locked(
        self,
        now: float,
        seek_type: str,
        *,
        from_ms: int,
        to_ms: int,
        duration_ms: int,
    ) -> dict[str, Any]:
        ratio = (
            min(1.0, max(0.0, float(to_ms) / float(duration_ms)))
            if duration_ms > 0
            else 0.0
        )
        from_index = (
            self._current_index
            if not self._seek_reanchor_active
            else self._seek_ref_index
        )
        seek_start, seek_end = self._compute_seek_window_locked(
            ratio, seek_type, from_index
        )
        self._agent.reset()
        self._last_success_normalized = ""
        self._last_success_match_key = ""
        self._last_success_recognized = ""
        self._last_success_at = None
        self._buffer_start_at = None
        self._beam_buffer_at = None

        self._seek_reanchor_active = True
        self._seek_type = seek_type
        self._seek_ref_index = from_index
        self._current_index = seek_start - 1
        start_pos = (
            self._timeline.line_starts[seek_start]
            if 0 <= seek_start < self._timeline.line_count
            else 0
        )
        self._beam = [
            _BeamHypothesis(
                pos=start_pos,
                score=0.0,
                last_adv_time=now,
                last_line=self._current_index,
            )
        ]
        seek_info: dict[str, Any] = {
            "type": seek_type,
            "from_ms": int(from_ms),
            "to_ms": int(to_ms),
            "from_index": int(from_index),
            "estimated_line": self._timeline.line_for_ratio(ratio),
            "window": [seek_start, seek_end],
        }
        self._pending_seek_event = seek_info
        return dict(seek_info)

    def update_playback_progress(
        self,
        progress_ms: int | None,
        duration_ms: int | None,
        *,
        song_id: str | None = None,
        is_playing: bool = True,
        estimated_progress_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """Store latest playback progress snapshot and detect manual seek/rewind."""
        with self._lock:
            now = self._clock()
            new_sp = int(progress_ms) if progress_ms is not None else None
            new_dur = int(duration_ms) if duration_ms is not None else None
            new_est = (
                int(estimated_progress_ms)
                if estimated_progress_ms is not None
                else new_sp
            )
            seek_event: dict[str, Any] | None = None

            same_duration = (
                new_dur is not None
                and self._playback_duration_ms is not None
                and new_dur > 0
                and new_dur == self._playback_duration_ms
            )
            same_song = not (
                song_id and self._track_song_id and song_id != self._track_song_id
            ) and not (
                song_id
                and self._playback_song_id
                and song_id != self._playback_song_id
            )
            if (
                self._seek_detection_enabled
                and self._lyrics
                and same_duration
                and same_song
            ):
                prev_sp = self._playback_progress_ms
                prev_base = (
                    self._playback_estimated_ms_at_poll
                    if self._playback_estimated_ms_at_poll is not None
                    else prev_sp
                )
                wall_dt_ms = (
                    max(0.0, now - self._playback_polled_at) * 1000.0
                    if self._playback_polled_at is not None
                    else 0.0
                )
                playing_dt_ms = wall_dt_ms if self._playback_is_playing else 0.0
                expected_est = (
                    (prev_base + playing_dt_ms) if prev_base is not None else None
                )
                max_normal_est = (
                    (prev_base + wall_dt_ms) if prev_base is not None else None
                )

                seek_type = ""
                if (
                    (
                        new_sp is not None
                        and prev_sp is not None
                        and (prev_sp - new_sp) >= self._seek_rewind_threshold_ms
                    )
                    or (
                        new_est is not None
                        and prev_base is not None
                        and (prev_base - new_est) >= self._seek_rewind_threshold_ms
                    )
                    or (
                        new_est is not None
                        and expected_est is not None
                        and prev_base is not None
                        and (
                            (new_sp <= (prev_sp - 250))
                            if (new_sp is not None and prev_sp is not None)
                            else (new_est <= (prev_base - 250))
                        )
                        and (expected_est - new_est) >= self._seek_rewind_threshold_ms
                    )
                ):
                    seek_type = "rewind"
                elif (
                    new_est is not None
                    and max_normal_est is not None
                    and (new_est - max_normal_est) >= self._seek_forward_threshold_ms
                ):
                    seek_type = "fast_forward"

                if seek_type:
                    from_ms = int(
                        prev_base
                        if prev_base is not None
                        else (prev_sp if prev_sp is not None else 0)
                    )
                    to_ms = int(
                        new_est
                        if new_est is not None
                        else (new_sp if new_sp is not None else 0)
                    )
                    seek_event = self._apply_playback_seek_locked(
                        now,
                        seek_type,
                        from_ms=from_ms,
                        to_ms=to_ms,
                        duration_ms=int(new_dur),
                    )

            self._playback_song_id = song_id
            self._playback_progress_ms = new_sp
            self._playback_estimated_ms_at_poll = new_est
            self._playback_duration_ms = new_dur
            self._playback_polled_at = now
            self._playback_is_playing = bool(is_playing)
            return seek_event

    def _snapshot_playback_progress_locked(self, now: float) -> dict[str, Any]:
        spotify_ms = self._playback_progress_ms
        duration_ms = self._playback_duration_ms
        base_ms = (
            self._playback_estimated_ms_at_poll
            if self._playback_estimated_ms_at_poll is not None
            else spotify_ms
        )
        estimated_ms = base_ms
        if (
            base_ms is not None
            and self._playback_is_playing
            and self._playback_polled_at is not None
        ):
            elapsed_ms = max(0.0, now - self._playback_polled_at) * 1000.0
            estimated_ms = int(base_ms + elapsed_ms)
            if duration_ms is not None and duration_ms >= 0:
                estimated_ms = min(duration_ms, estimated_ms)
        return {
            "spotify_progress_ms": spotify_ms,
            "estimated_progress_ms": estimated_ms,
            "duration_ms": duration_ms,
            "is_playing": self._playback_is_playing,
        }

    def get_playback_progress(self) -> dict[str, Any]:
        """Return a thread-safe snapshot of current playback progress state."""
        with self._lock:
            return self._snapshot_playback_progress_locked(self._clock())

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
        snapshot: dict[str, Any] = {
            "lazy_opened": True,
            "current_index": self._current_index,
            "buffer": self._agent.asr_buffer,
            "last_success_normalized": self._last_success_normalized,
            "last_success_recognized": self._last_success_recognized,
            "elapsed_since_last_success": elapsed_last,
            "elapsed_since_buffer_start": elapsed_buf,
        }
        if self._seek_reanchor_active:
            snapshot["seek_reanchor_active"] = True
            snapshot["seek_type"] = self._seek_type
            snapshot["seek_ref_index"] = int(self._seek_ref_index)
            if self._pending_seek_event is not None:
                snapshot["pending_seek_event"] = dict(self._pending_seek_event)
        if self._alignment_mode == "beam":
            snapshot["beam_pos"] = int(self._beam[0].pos) if self._beam else 0
            snapshot["elapsed_since_beam_buffer"] = (
                round(max(0.0, now - self._beam_buffer_at), 4)
                if self._beam_buffer_at is not None
                else None
            )
            snapshot["beam"] = [
                {
                    "pos": int(h.pos),
                    "score": round(float(h.score), 6),
                    "elapsed_since_adv": (
                        round(max(0.0, now - h.last_adv_time), 4)
                        if h.last_adv_time is not None
                        else None
                    ),
                    "last_line": int(h.last_line),
                }
                for h in self._beam
            ]
        return snapshot

    def restore_state(
        self,
        *,
        current_index: int = -1,
        buffer: str = "",
        last_success_normalized: str = "",
        last_success_recognized: str = "",
        elapsed_since_last_success: float | None = None,
        elapsed_since_buffer_start: float | None = None,
        beam_pos: int | None = None,
        elapsed_since_beam_buffer: float | None = None,
        beam: list[dict[str, Any]] | None = None,
        seek_reanchor_active: bool = False,
        seek_type: str = "",
        seek_ref_index: int = -1,
        pending_seek_event: dict[str, Any] | None = None,
    ) -> None:
        """Restore mid-song synchronization state (e.g. from a lazy-opened trace)."""
        with self._lock:
            now = self._clock()
            self._clear_seek_state_locked()
            if seek_reanchor_active:
                self._seek_reanchor_active = True
                self._seek_type = str(seek_type or "")
                self._seek_ref_index = int(seek_ref_index)
                self._pending_seek_event = (
                    dict(pending_seek_event)
                    if isinstance(pending_seek_event, dict)
                    else None
                )
            idx = int(current_index)
            if idx < -1:
                idx = -1
            elif self._lyrics and idx >= len(self._lyrics):
                idx = len(self._lyrics) - 1
            self._current_index = idx
            self._agent.asr_buffer = str(buffer or "")
            self._last_success_normalized = str(last_success_normalized or "")
            self._last_success_match_key = (
                self._to_match_key(self._last_success_normalized)
                if self._last_success_normalized
                else ""
            )
            self._last_success_recognized = str(last_success_recognized or "")
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
            self._beam_buffer_at = (
                now - float(elapsed_since_beam_buffer)
                if elapsed_since_beam_buffer is not None
                else (now if self._agent.asr_buffer else None)
            )
            max_pos = len(self._timeline.full_text)
            if beam and isinstance(beam, list):
                restored_beam: list[_BeamHypothesis] = []
                for item in beam:
                    if not isinstance(item, dict):
                        continue
                    pos_val = max(0, min(max_pos, int(item.get("pos", 0))))
                    adv_el = item.get("elapsed_since_adv")
                    adv_at = (
                        now - float(adv_el) if adv_el is not None else None
                    )
                    restored_beam.append(
                        _BeamHypothesis(
                            pos=pos_val,
                            score=float(item.get("score", 0.0)),
                            last_adv_time=adv_at,
                            last_line=int(item.get("last_line", idx)),
                        )
                    )
                if restored_beam:
                    self._beam = restored_beam[: self._beam_width]
                    return

            if beam_pos is not None:
                pos_val = max(0, min(max_pos, int(beam_pos)))
            elif 0 <= idx < self._timeline.line_count:
                pos_val = self._timeline.line_ends[idx]
            else:
                pos_val = 0
            self._beam = [
                _BeamHypothesis(
                    pos=pos_val,
                    score=0.0,
                    last_adv_time=self._last_success_at,
                    last_line=idx,
                )
            ]

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
            self._match_keys = [
                self._to_match_key(str(line.get("normalized") or ""))
                for line in self._lyrics
            ]
            self._timeline = ContinuousLyricsTimeline(self._match_keys)
            self._repeated_line_flags = self._build_repeated_line_flags(
                self._match_keys
            )
            self._clear_seek_state_locked()
            self._current_index = -1
            self._pending_scroll_targets = []
            self._pending_highlight_index = -1
            self._agent.reset()
            self._last_success_normalized = ""
            self._last_success_match_key = ""
            self._last_success_recognized = ""
            self._last_success_at = None
            self._buffer_start_at = None
            self._beam = [
                _BeamHypothesis(pos=0, score=0.0, last_adv_time=None, last_line=-1)
            ]
            self._beam_buffer_at = None

            corpus_parts: list[str] = []
            for line in self._lyrics:
                for key in ("original", "normalized", "reading"):
                    val = str(line.get(key) or "").strip()
                    if val:
                        corpus_parts.append(val.lower())
                        norm_val = normalize_match_text(val)
                        if norm_val:
                            corpus_parts.append(norm_val)
            self._lyrics_corpus = "\n".join(corpus_parts)

            if self._lyrics and track_meta:
                artist = str(
                    track_meta.get("artist_name")
                    or track_meta.get("artist")
                    or ""
                ).strip()
                title = str(
                    track_meta.get("song_name")
                    or track_meta.get("title")
                    or ""
                ).strip()
                if artist and title:
                    raw_slug = f"{artist}_{title}"
                elif title or artist:
                    raw_slug = title or artist
                else:
                    raw_slug = str(track_meta.get("song_id") or "unknown_track")
                self._track_slug = sanitize_trace_slug(raw_slug)
            elif self._lyrics:
                self._track_slug = "unknown_track"
            else:
                self._track_slug = ""

            new_song_id = (track_meta or {}).get("song_id")
            self._track_song_id = str(new_song_id or "") if self._lyrics else ""
            if not self._lyrics or (
                new_song_id
                and self._playback_song_id
                and new_song_id != self._playback_song_id
            ):
                self._playback_song_id = new_song_id if self._lyrics else None
                self._playback_progress_ms = None
                self._playback_estimated_ms_at_poll = None
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
                        "beam_size": config.ASR_BEAM_SIZE,
                        "best_of": config.ASR_BEST_OF,
                        "condition_on_previous_text": config.ASR_CONDITION_ON_PREVIOUS_TEXT,
                        "initial_prompt_enabled": config.is_initial_prompt_active(
                            config.ASR_MODEL_SIZE
                        ),
                        "vocal_bandpass_enabled": config.ASR_VOCAL_BANDPASS_ENABLED,
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

    def _build_upcoming_prompt_locked(
        self, max_lines: int | None = None, max_chars: int | None = None
    ) -> str:
        if not self._lyrics:
            return ""
        limit_lines = (
            int(max_lines)
            if max_lines is not None
            else int(getattr(config, "ASR_INITIAL_PROMPT_LINES", 4))
        )
        limit_chars = (
            int(max_chars)
            if max_chars is not None
            else int(getattr(config, "ASR_INITIAL_PROMPT_MAX_CHARS", 120))
        )
        if limit_lines <= 0 or limit_chars <= 0:
            return ""

        start_idx = max(0, self._current_index + 1)
        if self._seek_reanchor_active:
            playback = self._snapshot_playback_progress_locked(self._clock())
            ratio = self._current_playback_ratio_locked(playback)
            if ratio is not None:
                start_idx = max(0, self._timeline.line_for_ratio(ratio) - 1)
        if start_idx >= len(self._lyrics):
            return ""

        parts: list[str] = []
        total_chars = 0
        for line in self._lyrics[start_idx:]:
            orig = str(line.get("original") or "").strip()
            if not orig:
                continue
            sep_len = 1 if parts else 0
            if parts and (total_chars + sep_len + len(orig)) > limit_chars:
                break
            if not parts and len(orig) > limit_chars:
                parts.append(orig[:limit_chars])
                break
            parts.append(orig)
            total_chars += sep_len + len(orig)
            if len(parts) >= limit_lines:
                break
        return "、".join(parts)

    def get_upcoming_prompt(
        self, max_lines: int | None = None, max_chars: int | None = None
    ) -> str:
        """Return a compact prompt from the next upcoming lyric lines."""
        with self._lock:
            return self._build_upcoming_prompt_locked(
                max_lines=max_lines, max_chars=max_chars
            )

    def get_asr_context(
        self, max_lines: int | None = None, max_chars: int | None = None
    ) -> dict[str, Any]:
        """Return current lyric context (initial prompt, corpus, track slug) for ASR."""
        with self._lock:
            return {
                "initial_prompt": self._build_upcoming_prompt_locked(
                    max_lines=max_lines, max_chars=max_chars
                ),
                "lyrics_corpus": self._lyrics_corpus,
                "track_slug": self._track_slug,
                "current_index": self._current_index,
            }

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
            self._clear_seek_state_locked()
            self._pending_scroll_targets = []
            self._pending_highlight_index = lyric_index
            self._agent.reset()
            self._last_success_normalized = ""
            self._last_success_match_key = ""
            self._last_success_recognized = ""
            self._last_success_at = None
            self._buffer_start_at = None
            jump_pos = (
                self._timeline.line_ends[lyric_index]
                if lyric_index < self._timeline.line_count
                else 0
            )
            self._beam = [
                _BeamHypothesis(
                    pos=jump_pos,
                    score=0.0,
                    last_adv_time=None,
                    last_line=lyric_index,
                )
            ]
            self._beam_buffer_at = None

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
            "retained_tail": "",
            "early_display_hold": False,
            "alignment": None,
            "seek_detected": None,
            "seek_reanchored": False,
            "current_index_after": current_index,
        }

    def _effective_phase1_threshold(
        self,
        target_key: str,
        base_threshold: float,
        buffer_text: str,
        details: dict[str, Any],
        *,
        is_immediate_next: bool,
    ) -> tuple[float, bool]:
        if (
            not self._short_line_guard
            or len(target_key) > self._short_line_max_len
        ):
            return base_threshold, True

        if len(target_key) <= 2:
            return max(base_threshold, 0.99), float(details["ratio_score"]) >= 0.50

        effective = max(base_threshold, 0.90)
        if (
            not is_immediate_next
            and (len(buffer_text) - int(details["src_end"]) > 2)
            and float(details["ratio_score"]) < 0.60
        ):
            return effective, False
        return effective, True

    def _is_duplicate_locked(
        self,
        now: float,
        match_recognized: str,
        start_index: int,
        duplicate_ttl: float,
    ) -> bool:
        if not self._last_success_match_key or self._last_success_at is None:
            return False

        elapsed = now - self._last_success_at
        if not self._smart_duplicate_ttl:
            return (
                elapsed <= duplicate_ttl
                and match_recognized in self._last_success_match_key
            )

        next_key = (
            self._match_keys[start_index]
            if start_index < len(self._match_keys)
            else ""
        )
        next_is_repeat = bool(next_key) and (
            next_key == self._last_success_match_key
            or (
                len(next_key) >= 3
                and (
                    next_key in self._last_success_match_key
                    or self._last_success_match_key in next_key
                )
            )
            or (
                fuzz.ratio(next_key, self._last_success_match_key) / 100.0
                >= 0.85
            )
        )
        effective_ttl = (
            min(duplicate_ttl * 0.4, 1.5) if next_is_repeat else duplicate_ttl
        )
        if elapsed > effective_ttl:
            return False

        if match_recognized in self._last_success_match_key:
            return True

        if self._last_success_recognized and (
            match_recognized == self._last_success_recognized
            or (
                len(match_recognized) >= 4
                and match_recognized in self._last_success_recognized
            )
        ):
            return True

        if (
            not next_is_repeat
            and 5 <= len(match_recognized) <= len(self._last_success_match_key)
        ):
            sim_last = (
                fuzz.partial_ratio(
                    match_recognized, self._last_success_match_key
                )
                / 100.0
            )
            if sim_last >= 0.85:
                sim_next = (
                    fuzz.partial_ratio(match_recognized, next_key) / 100.0
                    if next_key
                    else 0.0
                )
                if sim_last > sim_next:
                    return True

        return False

    def _score_line_detailed(
        self,
        buffer_text: str,
        target_key: str,
        *,
        min_coverage_ratio: float | None = None,
    ) -> dict[str, Any]:
        min_cov = (
            self._agent.min_coverage_ratio
            if min_coverage_ratio is None
            else min_coverage_ratio
        )
        if self._weighted_dakuten:
            return score_similarity_weighted_detailed(
                buffer_text,
                target_key,
                min_coverage_ratio=min_cov,
                dakuten_cost=self._dakuten_cost,
            )
        return score_similarity_detailed(buffer_text, target_key, min_cov)

    def _score_line_ratio(self, buffer_text: str, target_key: str) -> float:
        if self._weighted_dakuten:
            return weighted_indel_ratio(
                buffer_text, target_key, dakuten_cost=self._dakuten_cost
            )
        return calculate_ratio_similarity_normalized(buffer_text, target_key)

    def _score_adjacent_pairs_locked(
        self,
        start_index: int,
        end_index_phase1: int,
        buffer_text: str,
        phase1_threshold: float,
        build_trace: bool,
        candidates: list[dict[str, Any]],
        *,
        playback_ratio: float | None = None,
        seek_active: bool = False,
    ) -> tuple[int, float, float, int, float, dict[str, Any]] | None:
        pair_ranges: list[tuple[int, int, bool]] = []
        if not seek_active and self._current_index >= 0 and start_index < len(self._lyrics):
            pair_ranges.append((self._current_index, start_index, True))
        for idx in range(
            start_index, min(end_index_phase1, len(self._lyrics) - 2) + 1
        ):
            pair_ranges.append((idx, idx + 1, False))

        eff_pair_th = (
            self._pair_threshold
            if self._pair_threshold is not None
            else phase1_threshold
        )
        min_cov = min(self._agent.min_coverage_ratio, 0.35)
        best_win: tuple[int, float, float, int, float, dict[str, Any]] | None = (
            None
        )
        best_pair_score = 0.0

        for i1, i2, is_bridge in pair_ranges:
            t1 = self._match_keys[i1]
            t2 = self._match_keys[i2]
            if len(t1) < 3 or len(t2) < 3:
                continue

            details = self._score_line_detailed(
                buffer_text, t1 + t2, min_coverage_ratio=min_cov
            )
            ds = int(details["dest_start"])
            de = int(details["dest_end"])
            l1, l2 = len(t1), len(t2)
            cov1 = max(0, min(de, l1) - ds)
            cov2 = max(0, de - max(ds, l1))

            allowed = (
                cov1 >= 3
                and cov2 >= 3
                and (cov1 / l1 >= 0.20)
                and (cov2 / l2 >= 0.25)
            )
            if allowed and self._short_line_guard and l2 <= self._short_line_max_len:
                if cov2 < l2:
                    allowed = False

            raw_score = float(details["final_score"])
            is_repeated = self._is_repeated_candidate_locked(
                i1
            ) or self._is_repeated_candidate_locked(i2)
            cand_ratio = (
                self._timeline.char_ratio(
                    0.5 * (self._timeline.line_starts[i1] + self._timeline.line_ends[i2])
                )
                if self._timeline.total_chars > 0
                else None
            )
            prior_factor = self._compute_position_prior_locked(
                i2,
                playback_ratio,
                cand_ratio=cand_ratio,
                is_repeated=is_repeated,
                is_immediate_next=(i1 == start_index or i2 == start_index),
                in_recovery=False,
                seek_active=seek_active,
            )
            score = raw_score * prior_factor
            if build_trace:
                candidates.append(
                    {
                        "index": i2,
                        "pair": [i1, i2],
                        "is_bridge": is_bridge,
                        "phase": 1,
                        "target_normalized": (
                            f"{self._lyrics[i1]['normalized']}"
                            f"+{self._lyrics[i2]['normalized']}"
                        ),
                        "coverage": round(details["coverage"], 4),
                        "raw_partial_score": round(
                            details["raw_partial_score"], 4
                        ),
                        "partial_score": round(details["partial_score"], 4),
                        "ratio_score": round(details["ratio_score"], 4),
                        "raw_sim": round(raw_score, 4),
                        "position_prior": round(prior_factor, 4),
                        "score": round(score, 4),
                        "effective_threshold": round(eff_pair_th, 4),
                        "allowed": allowed,
                        "src_end": int(details["src_end"]),
                        "dest_end": de,
                    }
                )

            if allowed and score >= eff_pair_th and score > best_pair_score:
                best_pair_score = score
                prog2 = min(1.0, cov2 / l2)
                if is_bridge:
                    emit_idx = i2
                    src_end = int(details["src_end"])
                    dest_prog = prog2
                elif prog2 < 0.70:
                    emit_idx = i1
                    src_end = max(
                        int(details["src_start"]),
                        int(details["src_end"]) - cov2,
                    )
                    dest_prog = 1.0
                else:
                    emit_idx = i2
                    src_end = int(details["src_end"])
                    dest_prog = prog2
                best_win = (
                    emit_idx,
                    score,
                    eff_pair_th,
                    src_end,
                    dest_prog,
                    details,
                )

        return best_win

    def _commit_match_locked(
        self,
        now: float,
        cleaned_text: str,
        match_recognized: str,
        best_index: int,
        best_score: float,
        buffer_length: int,
        is_recovery: bool,
        retained_tail: str,
        dest_progress: float,
        ratio_for_display: float,
        phase1_threshold: float,
        alignment_info: dict[str, Any] | None,
        build_trace: bool,
        step_trace: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        target_original = self._lyrics[best_index]["original"]
        target_normalized = self._lyrics[best_index]["normalized"]
        seek_reanchored = bool(self._seek_reanchor_active)
        self._seek_reanchor_active = False
        self._seek_type = ""
        self._seek_ref_index = -1

        is_early_prefix_match = (
            not is_recovery
            and not retained_tail
            and dest_progress < self._early_display_progress_threshold
            and ratio_for_display < phase1_threshold
        )
        early_display_hold = bool(
            self._display_mode == "adaptive" and is_early_prefix_match
        )
        if self._display_mode == "current" or early_display_hold:
            next_display_index = best_index
        else:
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
        self._last_success_match_key = self._match_keys[best_index]
        self._last_success_recognized = match_recognized
        self._last_success_at = now
        self._buffer_start_at = now if retained_tail else None

        if build_trace:
            step_trace["decision"] = (
                "matched_phase2_recovery" if is_recovery else "matched_phase1"
            )
            step_trace["matched_index"] = best_index
            step_trace["display_index"] = next_display_index
            step_trace["retained_tail"] = retained_tail
            step_trace["early_display_hold"] = early_display_hold
            step_trace["alignment"] = alignment_info
            step_trace["seek_reanchored"] = seek_reanchored
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
            "retained_tail": retained_tail,
            "early_display_hold": early_display_hold,
            "seek_reanchored": seek_reanchored,
        }
        return match_payload, step_trace

    def _prepare_eval_context_locked(
        self, now: float, build_trace: bool
    ) -> tuple[dict[str, Any], float | None, dict[str, Any] | None]:
        need_playback = bool(
            build_trace
            or self._position_prior_enabled
            or self._seek_detection_enabled
            or self._seek_reanchor_active
        )
        playback = (
            self._snapshot_playback_progress_locked(now) if need_playback else {}
        )
        playback_ratio = (
            self._current_playback_ratio_locked(playback)
            if (self._position_prior_enabled or self._seek_reanchor_active)
            else None
        )
        seek_event = self._pending_seek_event
        self._pending_seek_event = None
        return playback, playback_ratio, seek_event

    def _refresh_seek_window_locked(
        self, now: float, playback_ratio: float | None, *, reset_beam: bool
    ) -> tuple[int, int] | None:
        if not self._seek_reanchor_active or not self._lyrics:
            return None
        seek_start, seek_end = self._compute_seek_window_locked(
            playback_ratio, self._seek_type, self._seek_ref_index
        )
        self._current_index = seek_start - 1
        if reset_beam:
            start_pos = (
                self._timeline.line_starts[seek_start]
                if 0 <= seek_start < self._timeline.line_count
                else 0
            )
            self._beam = [
                _BeamHypothesis(
                    pos=start_pos,
                    score=0.0,
                    last_adv_time=now,
                    last_line=self._current_index,
                )
            ]
        return (seek_start, seek_end)

    def _evaluate_line_locked(
        self,
        now: float,
        cleaned_text: str,
        canonical_recognized: str,
        match_recognized: str,
        asr_payload: dict[str, Any],
        build_trace: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        playback, playback_ratio, seek_event = self._prepare_eval_context_locked(
            now, build_trace
        )
        cur_idx_before = self._current_index
        buffer_before = self._agent.asr_buffer
        elapsed_before = self._elapsed_since_success_locked(now)

        def _empty_result(
            decision: str,
        ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
            if not build_trace:
                return None, {}
            tr = self._make_step_trace(
                playback,
                asr_payload,
                canonical_recognized,
                cur_idx_before,
                buffer_before,
                elapsed_before,
                decision=decision,
            )
            if seek_event is not None:
                tr["seek_detected"] = seek_event
            return None, tr

        if not self._lyrics:
            return _empty_result("ignored_no_lyrics")

        seek_bounds = self._refresh_seek_window_locked(
            now, playback_ratio, reset_beam=False
        )
        seek_active = seek_bounds is not None
        start_index = (
            seek_bounds[0] if seek_bounds is not None else (self._current_index + 1)
        )
        duplicate_ttl = self._effective_duplicate_ttl()
        if not seek_active and self._is_duplicate_locked(
            now, match_recognized, start_index, duplicate_ttl
        ):
            return _empty_result("skipped_duplicate_ttl")

        if start_index >= len(self._lyrics):
            return _empty_result("end_of_lyrics")

        if not self._agent.asr_buffer:
            self._buffer_start_at = now

        self._agent.append(match_recognized)
        buffer_after_append = self._agent.asr_buffer
        if seek_active and seek_bounds is not None:
            target_length = max(
                [
                    len(self._match_keys[i])
                    for i in range(seek_bounds[0], seek_bounds[1] + 1)
                ]
                or [len(self._match_keys[start_index])]
            )
        else:
            target_length = len(self._match_keys[start_index])
        trimmed = self._agent.trim_if_needed(target_length)
        buffer_after_trim = self._agent.asr_buffer

        elapsed_since_success = self._elapsed_since_success_locked(now)
        max_lookahead = self._effective_lookahead_lines()
        phase1_threshold = self._effective_next_line_threshold()
        recovery_lookahead = self._effective_recovery_lookahead_lines()
        recovery_threshold = self._effective_recovery_threshold()

        if seek_active and seek_bounds is not None:
            phase1_lookahead_lines = max(1, seek_bounds[1] - seek_bounds[0] + 1)
            end_index_phase1 = seek_bounds[1]
        else:
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
        best_details: dict[str, Any] | None = None
        best_threshold = phase1_threshold

        best_passed_index: int | None = None
        best_passed_score = 0.0
        best_passed_details: dict[str, Any] | None = None
        best_passed_threshold = phase1_threshold

        candidates: list[dict[str, Any]] = []

        for idx in range(start_index, end_index_phase1 + 1):
            target_key = self._match_keys[idx]
            details = self._score_line_detailed(buffer_after_trim, target_key)
            raw_score = float(details["final_score"])
            is_repeated = self._is_repeated_candidate_locked(idx, target_key)
            prior_factor = self._compute_position_prior_locked(
                idx,
                playback_ratio,
                is_repeated=is_repeated,
                is_immediate_next=(idx == start_index),
                in_recovery=False,
                seek_active=seek_active,
            )
            score = raw_score * prior_factor
            eff_threshold, allowed = self._effective_phase1_threshold(
                target_key,
                phase1_threshold,
                buffer_after_trim,
                details,
                is_immediate_next=(not seek_active and idx == start_index),
            )
            if build_trace:
                candidates.append(
                    {
                        "index": idx,
                        "phase": 1,
                        "target_normalized": self._lyrics[idx]["normalized"],
                        "coverage": round(details["coverage"], 4),
                        "raw_partial_score": round(
                            details["raw_partial_score"], 4
                        ),
                        "partial_score": round(details["partial_score"], 4),
                        "ratio_score": round(details["ratio_score"], 4),
                        "raw_sim": round(raw_score, 4),
                        "position_prior": round(prior_factor, 4),
                        "score": round(score, 4),
                        "effective_threshold": round(eff_threshold, 4),
                        "allowed": allowed,
                        "src_end": int(details["src_end"]),
                        "dest_end": int(details["dest_end"]),
                    }
                )
            if allowed:
                if score > best_score:
                    best_score = score
                    best_index = idx
                    best_details = details
                    best_threshold = eff_threshold
                if score >= eff_threshold and score > best_passed_score:
                    best_passed_score = score
                    best_passed_index = idx
                    best_passed_details = details
                    best_passed_threshold = eff_threshold

        pair_src_end_override: int | None = None
        pair_dest_progress_override: float | None = None

        if best_passed_index is not None:
            best_index = best_passed_index
            best_score = best_passed_score
            best_details = best_passed_details
            best_threshold = best_passed_threshold
        elif self._adjacent_pair:
            pair_win = self._score_adjacent_pairs_locked(
                start_index,
                end_index_phase1,
                buffer_after_trim,
                phase1_threshold,
                build_trace,
                candidates,
                playback_ratio=playback_ratio,
                seek_active=seek_active,
            )
            if pair_win is not None:
                (
                    best_index,
                    best_score,
                    best_threshold,
                    pair_src_end_override,
                    pair_dest_progress_override,
                    best_details,
                ) = pair_win
                best_passed_index = best_index

        # 階段二：迷失恢復機制 (階段一達到上限後才啟用)
        is_recovery = False
        phase2_range: list[int] | None = None
        if (
            not seek_active
            and best_passed_index is None
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
                    target_key = self._match_keys[idx]
                    raw_score = self._score_line_ratio(
                        buffer_after_trim, target_key
                    )
                    is_repeated = self._is_repeated_candidate_locked(
                        idx, target_key
                    )
                    prior_factor = self._compute_position_prior_locked(
                        idx,
                        playback_ratio,
                        is_repeated=is_repeated,
                        is_immediate_next=False,
                        in_recovery=True,
                        seek_active=False,
                    )
                    score = raw_score * prior_factor
                    req_threshold = (
                        max(
                            recovery_threshold,
                            0.99 if len(target_key) <= 2 else 0.90,
                        )
                        if (
                            self._short_line_guard
                            and len(target_key) <= self._short_line_max_len
                        )
                        else recovery_threshold
                    )
                    if build_trace:
                        candidates.append(
                            {
                                "index": idx,
                                "phase": 2,
                                "target_normalized": self._lyrics[idx][
                                    "normalized"
                                ],
                                "ratio_score": round(raw_score, 4),
                                "raw_sim": round(raw_score, 4),
                                "position_prior": round(prior_factor, 4),
                                "score": round(score, 4),
                                "effective_threshold": round(req_threshold, 4),
                            }
                        )
                    if score > best_score and score >= req_threshold:
                        best_score = score
                        best_index = idx
                        best_threshold = req_threshold
                        best_details = None
                        is_recovery = True

        threshold_to_pass = (
            best_threshold if best_index is not None else phase1_threshold
        )

        step_trace: dict[str, Any] = {}
        if build_trace:
            step_trace = self._make_step_trace(
                playback,
                asr_payload,
                canonical_recognized,
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
            if seek_event is not None:
                step_trace["seek_detected"] = seek_event

        if best_index is None or best_score < threshold_to_pass:
            if build_trace:
                step_trace["decision"] = "no_match"
                step_trace["current_index_after"] = self._current_index
            return None, step_trace

        buffer_length = len(buffer_after_trim)
        consume_end = (
            pair_src_end_override
            if pair_src_end_override is not None
            else (int(best_details["src_end"]) if best_details is not None else 0)
        )
        if self._retain_tail and not is_recovery and best_details is not None:
            retained_tail = self._agent.consume_prefix(consume_end)
        else:
            self._agent.reset()
            retained_tail = ""

        target_key = self._match_keys[best_index]
        if pair_dest_progress_override is not None:
            dest_progress = pair_dest_progress_override
        elif best_details is not None and target_key:
            dest_progress = min(1.0, int(best_details["dest_end"]) / len(target_key))
        else:
            dest_progress = 1.0

        ratio_for_display = (
            float(best_details["ratio_score"])
            if best_details is not None
            else best_score
        )
        alignment_info = (
            {
                "src_start": int(best_details["src_start"]),
                "src_end": int(best_details["src_end"]),
                "dest_start": int(best_details["dest_start"]),
                "dest_end": int(best_details["dest_end"]),
                "dest_progress": round(dest_progress, 4),
            }
            if best_details is not None
            else None
        )
        return self._commit_match_locked(
            now=now,
            cleaned_text=cleaned_text,
            match_recognized=match_recognized,
            best_index=best_index,
            best_score=best_score,
            buffer_length=buffer_length,
            is_recovery=is_recovery,
            retained_tail=retained_tail,
            dest_progress=dest_progress,
            ratio_for_display=ratio_for_display,
            phase1_threshold=phase1_threshold,
            alignment_info=alignment_info,
            build_trace=build_trace,
            step_trace=step_trace,
        )

    def _evaluate_beam_locked(
        self,
        now: float,
        cleaned_text: str,
        canonical_recognized: str,
        match_recognized: str,
        asr_payload: dict[str, Any],
        build_trace: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        playback, playback_ratio, seek_event = self._prepare_eval_context_locked(
            now, build_trace
        )
        cur_idx_before = self._current_index
        buffer_before = self._agent.asr_buffer
        elapsed_before = self._elapsed_since_success_locked(now)

        def _empty_result(
            decision: str,
        ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
            if not build_trace:
                return None, {}
            tr = self._make_step_trace(
                playback,
                asr_payload,
                canonical_recognized,
                cur_idx_before,
                buffer_before,
                elapsed_before,
                decision=decision,
            )
            if seek_event is not None:
                tr["seek_detected"] = seek_event
            return None, tr

        if not self._lyrics:
            return _empty_result("ignored_no_lyrics")

        seek_bounds = self._refresh_seek_window_locked(
            now, playback_ratio, reset_beam=True
        )
        seek_active = seek_bounds is not None
        start_index = (
            seek_bounds[0] if seek_bounds is not None else (self._current_index + 1)
        )
        duplicate_ttl = self._effective_duplicate_ttl()
        if not seek_active and self._is_duplicate_locked(
            now, match_recognized, start_index, duplicate_ttl
        ):
            return _empty_result("skipped_duplicate_ttl")

        n_lines = len(self._lyrics)
        if start_index >= n_lines:
            return _empty_result("end_of_lyrics")

        if self._buffer_start_at is None:
            self._buffer_start_at = now
        if (
            self._beam_buffer_at is not None
            and (now - self._beam_buffer_at) > self._beam_buffer_stale_seconds
        ):
            self._agent.reset()

        prev_buf_len = len(self._agent.asr_buffer)
        self._agent.append(match_recognized)
        buffer_after_append = self._agent.asr_buffer
        added_tail = len(buffer_after_append) - prev_buf_len
        trimmed = False
        if len(buffer_after_append) > self._beam_max_buffer_chars:
            self._agent.asr_buffer = buffer_after_append[
                -self._beam_max_buffer_chars :
            ]
            trimmed = True
        buffer_after_trim = self._agent.asr_buffer
        self._beam_buffer_at = now

        fresh_start_in_buf = max(
            0, len(buffer_after_trim) - max(len(match_recognized), added_tail)
        )
        queries: list[tuple[str, bool, int]] = (
            [(match_recognized, True, 0)]
            if buffer_after_trim == match_recognized
            else [
                (match_recognized, True, 0),
                (buffer_after_trim, False, fresh_start_in_buf),
            ]
        )

        elapsed_since_success = self._elapsed_since_success_locked(now)
        max_lookahead = self._effective_lookahead_lines()
        phase1_threshold = self._effective_next_line_threshold()
        recovery_lookahead = self._effective_recovery_lookahead_lines()
        recovery_threshold = self._effective_recovery_threshold()

        if seek_active and seek_bounds is not None:
            phase1_lookahead_lines = max(1, seek_bounds[1] - seek_bounds[0] + 1)
            end_index_phase1 = seek_bounds[1]
            phase2_range: list[int] | None = None
        else:
            phase1_lookahead_lines = max(
                1,
                min(
                    max_lookahead,
                    int(elapsed_since_success / self._lookahead_seconds_per_line),
                ),
            )
            end_index_phase1 = min(
                start_index + phase1_lookahead_lines, n_lines - 1
            )
            phase2_range = None
            if phase1_lookahead_lines >= max_lookahead:
                p2_start = end_index_phase1 + 1
                p2_end = min(
                    self._current_index + recovery_lookahead, n_lines - 1
                )
                if p2_start <= p2_end:
                    phase2_range = [p2_start, p2_end]

        timeline = self._timeline
        full_text = timeline.full_text
        line_starts = timeline.line_starts
        line_ends = timeline.line_ends
        char_to_line = timeline.char_to_line
        decay = self._beam_decay

        next_hyps: list[tuple[float, _BeamHypothesis, tuple[Any, ...] | None]] = [
            (h.score * decay, _BeamHypothesis(h.pos, h.score * decay, h.last_adv_time, h.last_line), None)
            for h in self._beam
        ]
        candidates: list[dict[str, Any]] = []
        seen_cand_keys: set[tuple[int, int, bool]] = set()
        best_seen_sim: float | None = None

        for h in self._beam:
            h_line = h.last_line
            if seek_active and seek_bounds is not None:
                first_target_line = seek_bounds[0]
                max_p1_line = seek_bounds[1]
                max_target_line = seek_bounds[1]
                win_start = line_starts[seek_bounds[0]] if n_lines > 0 else 0
                win_end = line_ends[seek_bounds[1]] if n_lines > 0 else 0
            else:
                ref_t = (
                    h.last_adv_time
                    if h.last_adv_time is not None
                    else self._buffer_start_at
                )
                dt = max(0.0, now - ref_t) if ref_t is not None else 0.0
                h_p1_la = max(
                    1,
                    min(max_lookahead, int(dt / self._lookahead_seconds_per_line)),
                )
                if h_line < 0:
                    max_p1_line = min(n_lines - 1, h_p1_la)
                    max_target_line = (
                        min(n_lines - 1, recovery_lookahead - 1)
                        if h_p1_la >= max_lookahead
                        else max_p1_line
                    )
                else:
                    max_p1_line = min(n_lines - 1, h_line + 1 + h_p1_la)
                    max_target_line = (
                        min(n_lines - 1, h_line + recovery_lookahead)
                        if h_p1_la >= max_lookahead
                        else max_p1_line
                    )

                win_start = max(0, h.pos - self._beam_back_slack)
                win_end = line_ends[max_target_line] if n_lines > 0 else 0
                first_target_line = max(0, h_line + 1)

            if win_end <= win_start:
                continue
            window = full_text[win_start:win_end]

            min_len_in_win = min(
                (
                    timeline.line_length(li)
                    for li in range(first_target_line, max_target_line + 1)
                    if timeline.line_length(li) > 0
                ),
                default=self._short_line_max_len + 1,
            )
            min_raw_score = (
                4.0
                if (
                    not self._short_line_guard
                    or min_len_in_win <= self._short_line_max_len
                )
                else 6.0
            )
            peaks_top_k = (
                12 if (seek_active or max_target_line > max_p1_line) else 6
            )

            best_echo_raw = 0.0
            adv_cands: list[tuple[Any, ...]] = []

            for q_text, is_fresh_q, fresh_start in queries:
                if len(q_text) < 2:
                    continue
                peaks = smith_waterman_local_peaks(
                    q_text,
                    window,
                    dakuten_score=self._beam_dakuten_score,
                    min_raw_score=min_raw_score,
                    top_k=peaks_top_k,
                )
                for peak in peaks:
                    g_s = win_start + peak.ref_start
                    g_e = win_start + peak.ref_end
                    if g_e <= g_s:
                        continue
                    matched_r_len = g_e - g_s
                    matched_q_len = peak.q_end - peak.q_start
                    end_line = char_to_line[g_e - 1]
                    start_line = char_to_line[g_s]
                    line_len = timeline.line_length(end_line)

                    if not is_fresh_q:
                        fresh_used = peak.q_end - max(peak.q_start, fresh_start)
                        if fresh_used < min(2, len(match_recognized)) or (
                            len(q_text) - peak.q_end
                            > max(3, len(match_recognized) // 2)
                        ):
                            continue

                    if g_e <= h.pos:
                        if peak.local_sim >= self._beam_min_sim:
                            best_echo_raw = max(best_echo_raw, peak.raw_score)
                        continue

                    in_recovery = end_line > max_p1_line
                    eff_req_sim = (
                        max(recovery_threshold, 0.80)
                        if in_recovery
                        else self._beam_min_sim
                    )
                    is_exact_short = (
                        line_len <= self._short_line_max_len
                        and g_s == line_starts[end_line]
                        and g_e == line_ends[end_line]
                        and peak.local_sim >= 0.95
                    )

                    allowed = True
                    if self._short_line_guard:
                        if matched_r_len < 4 and not is_exact_short:
                            allowed = False
                        elif line_len <= self._short_line_max_len:
                            eff_req_sim = max(
                                eff_req_sim,
                                0.99 if line_len <= 2 else 0.90,
                            )
                            if line_len <= 2 and len(q_text) > line_len + 2:
                                allowed = False
                            elif (
                                not seek_active
                                and end_line > first_target_line
                                and (len(q_text) - peak.q_end > 2)
                            ):
                                allowed = False
                        elif matched_r_len <= 5:
                            eff_req_sim = max(eff_req_sim, 0.85)
                            if (
                                not seek_active
                                and start_line > first_target_line
                                and (len(q_text) - peak.q_end > 2)
                                and (matched_r_len / max(1, line_len) < 0.35)
                            ):
                                allowed = False
                        if (
                            matched_q_len < 6
                            and len(q_text) >= 14
                            and (len(q_text) - peak.q_end > 3)
                            and peak.local_sim < 0.90
                        ):
                            allowed = False
                    elif matched_r_len < 2:
                        allowed = False

                    if in_recovery and matched_r_len < (
                        6 if self._short_line_guard else 4
                    ):
                        allowed = False

                    is_repeated = self._is_repeated_candidate_locked(
                        end_line, full_text[g_s:g_e]
                    )
                    prior_factor = self._compute_position_prior_locked(
                        end_line,
                        playback_ratio,
                        cand_ratio=timeline.char_ratio(0.5 * (g_s + g_e)),
                        is_repeated=is_repeated,
                        is_immediate_next=(end_line <= first_target_line),
                        in_recovery=in_recovery,
                        seek_active=seek_active,
                    )
                    weighted_sim = peak.local_sim * prior_factor

                    if build_trace:
                        cand_key = (end_line, 2 if in_recovery else 1, is_fresh_q)
                        if cand_key not in seen_cand_keys:
                            seen_cand_keys.add(cand_key)
                            candidates.append(
                                {
                                    "index": end_line,
                                    "start_index": start_line,
                                    "phase": 2 if in_recovery else 1,
                                    "target_normalized": self._lyrics[end_line][
                                        "normalized"
                                    ],
                                    "coverage": round(
                                        timeline.line_progress(end_line, g_e), 4
                                    ),
                                    "raw_score": round(peak.raw_score, 4),
                                    "raw_sim": round(peak.local_sim, 4),
                                    "position_prior": round(prior_factor, 4),
                                    "score": round(weighted_sim, 4),
                                    "effective_threshold": round(
                                        eff_req_sim, 4
                                    ),
                                    "allowed": allowed,
                                    "span": [g_s, g_e],
                                    "q_span": [peak.q_start, peak.q_end],
                                }
                            )

                    if allowed:
                        if best_seen_sim is None or weighted_sim > best_seen_sim:
                            best_seen_sim = weighted_sim
                        if weighted_sim >= eff_req_sim:
                            adv_cands.append(
                                (
                                    peak.raw_score,
                                    weighted_sim,
                                    prior_factor,
                                    peak.q_start,
                                    peak.q_end,
                                    g_s,
                                    g_e,
                                    start_line,
                                    end_line,
                                    q_text,
                                    is_fresh_q,
                                    in_recovery,
                                    eff_req_sim,
                                    is_exact_short,
                                )
                            )

            if best_echo_raw > 0.0:
                conf_sc = h.score * decay + best_echo_raw * 0.45
                next_hyps.append(
                    (
                        conf_sc,
                        _BeamHypothesis(
                            h.pos, conf_sc, h.last_adv_time, h.last_line
                        ),
                        None,
                    )
                )

            for (
                raw_sc,
                local_sim,
                prior_factor,
                qs,
                qe,
                g_s,
                g_e,
                start_line,
                end_line,
                q_text,
                is_fresh_q,
                in_recovery,
                eff_req_sim,
                is_exact_short,
            ) in adv_cands:
                if seek_active:
                    trans = (
                        self._beam_forward_bonus * prior_factor
                        - self._beam_max_jump_penalty * (1.0 - prior_factor)
                    )
                else:
                    gap = g_s - h.pos
                    line_skip = max(0, start_line - (h_line + 1))
                    if in_recovery:
                        trans = -self._beam_recovery_penalty
                    elif -self._beam_back_slack <= gap <= 8 and line_skip == 0:
                        trans = self._beam_forward_bonus
                    elif gap > 8 or line_skip > 0:
                        trans = -min(
                            self._beam_max_jump_penalty,
                            self._beam_jump_penalty_per_char * max(0, gap - 6)
                            + self._beam_jump_penalty_per_line * line_skip,
                        )
                    else:
                        trans = -1.5
                    if prior_factor < 1.0:
                        trans -= self._beam_max_jump_penalty * (1.0 - prior_factor)

                step_gain = raw_sc * local_sim + trans
                min_gain = 3.0 if is_exact_short else 5.0
                if step_gain < min_gain:
                    continue
                new_sc = h.score * decay + step_gain
                nh = _BeamHypothesis(
                    pos=g_e, score=new_sc, last_adv_time=now, last_line=end_line
                )
                next_hyps.append(
                    (
                        new_sc,
                        nh,
                        (
                            nh,
                            local_sim,
                            raw_sc,
                            g_s,
                            g_e,
                            start_line,
                            end_line,
                            q_text,
                            qs,
                            qe,
                            is_fresh_q,
                            in_recovery,
                            eff_req_sim,
                        ),
                    )
                )

        next_hyps.sort(key=lambda item: item[0], reverse=True)
        pruned: list[_BeamHypothesis] = []
        best_ev: tuple[Any, ...] | None = None
        for _, hyp, ev_info in next_hyps:
            if any(abs(hyp.pos - kept.pos) <= 5 for kept in pruned):
                continue
            if not pruned:
                best_ev = ev_info
            pruned.append(hyp)
            if len(pruned) >= self._beam_width:
                break
        if pruned:
            self._beam = pruned

        matched_emit = False
        emit_line = self._current_index
        win_sim = best_seen_sim
        win_thresh = self._beam_min_sim
        win_recovery = False
        win_gs = 0
        win_ge = 0
        win_qs = 0
        win_qe = 0
        win_q_text = ""
        src_consume = 0

        if best_ev is not None:
            (
                nh,
                local_sim,
                _raw_sc,
                g_s,
                g_e,
                start_line,
                end_line,
                q_text,
                qs,
                qe,
                _is_fresh_q,
                in_recovery,
                eff_req_sim,
            ) = best_ev
            eff_end_line = end_line
            entered_chars = g_e - line_starts[eff_end_line]
            end_line_len = timeline.line_length(eff_end_line)
            if eff_end_line > start_line and entered_chars < min(3, end_line_len):
                eff_end_line -= 1
                g_e = line_ends[eff_end_line]
                self._beam[0] = _BeamHypothesis(
                    pos=g_e,
                    score=nh.score,
                    last_adv_time=now,
                    last_line=eff_end_line,
                )

            if eff_end_line > self._current_index:
                end_prog = timeline.line_progress(eff_end_line, g_e)
                if (
                    start_line > self._current_index
                    and start_line < eff_end_line
                    and end_prog < 0.35
                ):
                    emit_line = start_line
                    spill = g_e - line_starts[eff_end_line]
                    src_consume = max(qs, qe - spill)
                    win_ge = line_ends[start_line]
                    self._beam[0] = _BeamHypothesis(
                        pos=win_ge,
                        score=nh.score,
                        last_adv_time=now,
                        last_line=emit_line,
                    )
                else:
                    emit_line = eff_end_line
                    src_consume = qe
                    win_ge = g_e

                if emit_line > self._current_index:
                    matched_emit = True
                    win_sim = local_sim
                    win_thresh = eff_req_sim
                    win_recovery = bool(in_recovery)
                    win_gs = g_s
                    win_qs = qs
                    win_qe = src_consume
                    win_q_text = q_text

        step_trace: dict[str, Any] = {}
        if build_trace:
            step_trace = self._make_step_trace(
                playback,
                asr_payload,
                canonical_recognized,
                cur_idx_before,
                buffer_before,
                elapsed_since_success,
            )
            step_trace["buffer_after_append"] = buffer_after_append
            step_trace["trimmed"] = bool(trimmed)
            step_trace["buffer_after_trim"] = buffer_after_trim
            step_trace["trim_target_length"] = len(self._match_keys[start_index])
            step_trace["phase1_lookahead_lines"] = phase1_lookahead_lines
            step_trace["phase1_range"] = [start_index, end_index_phase1]
            step_trace["phase2_range"] = phase2_range
            step_trace["candidates"] = candidates
            step_trace["threshold"] = round(win_thresh, 4)
            step_trace["best_score"] = (
                round(win_sim, 4) if win_sim is not None else None
            )
            step_trace["is_recovery"] = win_recovery
            if seek_event is not None:
                step_trace["seek_detected"] = seek_event

        if not matched_emit or win_sim is None:
            if build_trace:
                step_trace["decision"] = "no_match"
                step_trace["current_index_after"] = self._current_index
            return None, step_trace

        buffer_length = len(buffer_after_trim)
        if self._retain_tail and not win_recovery:
            self._agent.asr_buffer = win_q_text[src_consume:]
            retained_tail = self._agent.asr_buffer
        else:
            self._agent.reset()
            retained_tail = ""
        if not retained_tail:
            self._beam_buffer_at = None

        dest_progress = timeline.line_progress(emit_line, win_ge)
        line_len = timeline.line_length(emit_line)
        matched_in_line = max(
            0, min(win_ge, line_ends[emit_line]) - max(win_gs, line_starts[emit_line])
        )
        ratio_for_display = (
            (2.0 * matched_in_line) / (matched_in_line + line_len)
            if line_len > 0
            else win_sim
        )
        alignment_info = {
            "src_start": int(win_qs),
            "src_end": int(win_qe),
            "dest_start": int(max(0, win_gs - line_starts[emit_line])),
            "dest_end": int(max(0, min(win_ge, line_ends[emit_line]) - line_starts[emit_line])),
            "dest_progress": round(dest_progress, 4),
            "global_span": [int(win_gs), int(win_ge)],
        }
        return self._commit_match_locked(
            now=now,
            cleaned_text=cleaned_text,
            match_recognized=match_recognized,
            best_index=emit_line,
            best_score=win_sim,
            buffer_length=buffer_length,
            is_recovery=win_recovery,
            retained_tail=retained_tail,
            dest_progress=dest_progress,
            ratio_for_display=ratio_for_display,
            phase1_threshold=phase1_threshold,
            alignment_info=alignment_info,
            build_trace=build_trace,
            step_trace=step_trace,
        )

    def _evaluate_locked(
        self,
        now: float,
        cleaned_text: str,
        canonical_recognized: str,
        match_recognized: str,
        asr_payload: dict[str, Any],
        build_trace: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        if self._alignment_mode == "beam":
            return self._evaluate_beam_locked(
                now,
                cleaned_text,
                canonical_recognized,
                match_recognized,
                asr_payload,
                build_trace,
            )
        return self._evaluate_line_locked(
            now,
            cleaned_text,
            canonical_recognized,
            match_recognized,
            asr_payload,
            build_trace,
        )

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
        canonical_recognized = (
            normalize_match_text(normalize_to_hiragana(cleaned_text))
            if cleaned_text
            else ""
        )
        match_recognized = (
            self._to_match_key(canonical_recognized)
            if canonical_recognized
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

            if (
                self._asr_segment_filter
                and asr_meta is not None
                and not asr_payload.get("segment_filtered")
                and isinstance(asr_payload.get("segments"), list)
                and asr_payload["segments"]
            ):
                filtered_res = filter_asr_segments(
                    asr_payload["segments"],
                    lyrics_corpus=self._lyrics_corpus,
                    min_avg_logprob=self._asr_min_avg_logprob,
                    max_no_speech_prob=self._asr_max_no_speech_prob,
                    max_compression_ratio=self._asr_max_compression_ratio,
                )
                cleaned_text = filtered_res["text"]
                asr_payload["text"] = cleaned_text
                asr_payload["raw_text_before_filter"] = filtered_res[
                    "raw_text_before_filter"
                ]
                asr_payload["hallucinations_removed"] = filtered_res[
                    "hallucinations_removed"
                ]
                asr_payload["filtered_segments"] = filtered_res[
                    "filtered_segments"
                ]
                asr_payload["segment_filtered"] = True
                canonical_recognized = (
                    normalize_match_text(normalize_to_hiragana(cleaned_text))
                    if cleaned_text
                    else ""
                )
                match_recognized = (
                    self._to_match_key(canonical_recognized)
                    if canonical_recognized
                    else ""
                )

            if (
                not cleaned_text
                or not canonical_recognized
                or not match_recognized
            ):
                empty_decision = (
                    "asr_empty_or_filtered"
                    if (not cleaned_text and asr_meta is not None)
                    else "ignored_empty"
                )
                seek_event = self._pending_seek_event
                self._pending_seek_event = None
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
                    if seek_event is not None:
                        step_trace["seek_detected"] = seek_event
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
                canonical_recognized,
                match_recognized,
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
