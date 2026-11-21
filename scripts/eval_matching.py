"""Offline match trace replay and multi-strategy benchmark CLI.

Examples:
    python scripts/eval_matching.py data/traces/song.jsonl --timeline
    python scripts/eval_matching.py data/traces/ --strategy baseline --strategy baseline:threshold=0.68 --diff
    python scripts/eval_matching.py data/traces/ --strategy baseline --strategy baseline:threshold=0.70 --export-md report.md
"""

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import sys
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = str(_REPO_ROOT / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from utaten_finder.core import config  # noqa: E402
from utaten_finder.lyrics.matching import (  # noqa: E402
    UNCONDITIONAL_HALLUCINATIONS,
    LyricSyncEngine,
)
from utaten_finder.lyrics.parser import extract_ruby_reading  # noqa: E402
from utaten_finder.lyrics.trace import load_trace  # noqa: E402

def _parse_bool(value: str) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"布林參數必須為 true/false，收到: {value!r}")


def _parse_display_mode(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in {"adaptive", "next", "current"}:
        raise ValueError(
            f"display_mode 必須為 adaptive/next/current，收到: {value!r}"
        )
    return normalized


def _parse_alignment_mode(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in {"line", "beam"}:
        raise ValueError(
            f"align_mode 必須為 line/beam，收到: {value!r}"
        )
    return normalized


_BASELINE_PARAM_CASTERS: dict[str, tuple[str, Any]] = {
    "threshold": ("fuzzy_next_line_threshold", float),
    "recovery": ("recovery_threshold", float),
    "lookahead": ("fuzzy_lookahead_lines", int),
    "recovery_lookahead": ("recovery_lookahead_lines", int),
    "min_coverage": ("min_coverage_ratio", float),
    "max_buffer_ratio": ("max_buffer_ratio", float),
    "duplicate_ttl": ("duplicate_ttl", float),
    "lookahead_sec": ("lookahead_seconds_per_line", float),
    "retain_tail": ("retain_tail", _parse_bool),
    "short_guard": ("short_line_guard", _parse_bool),
    "short_len": ("short_line_max_len", int),
    "smart_dup": ("smart_duplicate_ttl", _parse_bool),
    "display_mode": ("display_mode", _parse_display_mode),
    "early_progress": ("early_display_progress_threshold", float),
    "align_mode": ("alignment_mode", _parse_alignment_mode),
    "phono": ("phonological_norm", _parse_bool),
    "phono_particles": ("phono_particles", _parse_bool),
    "phono_long": ("phono_long_vowels", _parse_bool),
    "phono_sokuon": ("phono_drop_sokuon", _parse_bool),
    "weighted_dakuten": ("weighted_dakuten", _parse_bool),
    "dakuten_cost": ("dakuten_cost", float),
    "pair": ("adjacent_pair", _parse_bool),
    "pair_threshold": ("pair_threshold", float),
    "beam_width": ("beam_width", int),
    "beam_min_sim": ("beam_min_sim", float),
    "beam_dakuten": ("beam_dakuten_score", float),
    "beam_forward": ("beam_forward_bonus", float),
    "beam_jump_pen": ("beam_jump_penalty_per_char", float),
    "beam_line_pen": ("beam_jump_penalty_per_line", float),
    "beam_max_jump_pen": ("beam_max_jump_penalty", float),
    "beam_rec_pen": ("beam_recovery_penalty", float),
    "beam_decay": ("beam_decay", float),
    "beam_back_slack": ("beam_back_slack", int),
    "beam_buf_chars": ("beam_max_buffer_chars", int),
    "beam_stale_sec": ("beam_buffer_stale_seconds", float),
    "asr_filter": ("asr_segment_filter", _parse_bool),
    "min_logprob": ("asr_min_avg_logprob", float),
    "max_no_speech": ("asr_max_no_speech_prob", float),
    "max_comp_ratio": ("asr_max_compression_ratio", float),
    "pos_prior": ("position_prior_enabled", _parse_bool),
    "pos_tol": ("position_prior_tolerance", float),
    "pos_weight": ("position_prior_weight", float),
    "pos_floor": ("position_prior_floor", float),
    "seek_detect": ("seek_detection_enabled", _parse_bool),
    "seek_rewind_ms": ("seek_rewind_threshold_ms", int),
    "seek_forward_ms": ("seek_forward_threshold_ms", int),
    "seek_radius": ("seek_window_ratio_radius", float),
}


class VirtualClock:
    """Deterministic monotonic clock controlled by recorded trace timestamps."""

    def __init__(self, initial: float = 0.0) -> None:
        self.now = float(initial)

    def __call__(self) -> float:
        return self.now


@dataclass(frozen=True)
class StrategySpec:
    """Parsed CLI strategy specification (e.g. ``baseline:threshold=0.7``)."""

    raw: str
    name: str
    params: dict[str, Any] = field(default_factory=dict)


class ReplayStrategy:
    """Base interface for offline lyric matching strategies."""

    def __init__(self, spec: StrategySpec) -> None:
        self.spec = spec

    def reset(
        self,
        lyrics: list[dict[str, Any]],
        session_start: dict[str, Any],
        clock: VirtualClock,
    ) -> None:
        raise NotImplementedError

    def step(
        self,
        recognized_text: str,
        asr_meta: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        raise NotImplementedError

    def apply_manual_jump(self, line_index: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def update_playback_progress(self, playback: dict[str, Any]) -> None:
        return None


class BaselineStrategy(ReplayStrategy):
    """Wraps production ``LyricSyncEngine`` with virtual time and optional overrides."""

    _DEFAULT_OVERRIDES: dict[str, Any] = {}

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self._engine: LyricSyncEngine | None = None
        self._asr_filter_active = False
        self._reconstruct_unfiltered = False
        self._last_poll_tuple: tuple[Any, Any, bool] | None = None
        self._last_est_ms: int | None = None

    def _resolve_param(
        self, key: str, matcher_cfg: dict[str, Any], legacy_default: Any
    ) -> Any:
        if key in self.spec.params:
            return self.spec.params[key]
        if key in self._DEFAULT_OVERRIDES:
            return self._DEFAULT_OVERRIDES[key]
        return matcher_cfg.get(key, legacy_default)

    def reset(
        self,
        lyrics: list[dict[str, Any]],
        session_start: dict[str, Any],
        clock: VirtualClock,
    ) -> None:
        matcher_cfg = session_start.get("matcher_config") or {}
        asr_cfg = session_start.get("asr_config") or {}

        alignment_mode = str(
            self._resolve_param("alignment_mode", matcher_cfg, "line")
        )
        if "beam_min_sim" in self.spec.params:
            beam_min_sim = float(self.spec.params["beam_min_sim"])
        elif (
            alignment_mode == "beam"
            and "fuzzy_next_line_threshold" in self.spec.params
        ):
            beam_min_sim = float(self.spec.params["fuzzy_next_line_threshold"])
        else:
            beam_min_sim = float(
                self._resolve_param("beam_min_sim", matcher_cfg, 0.73)
            )
        pair_threshold_raw = self._resolve_param(
            "pair_threshold", matcher_cfg, None
        )

        engine_kwargs: dict[str, Any] = {
            "lookahead_seconds_per_line": float(
                self.spec.params.get(
                    "lookahead_seconds_per_line",
                    matcher_cfg.get("lookahead_seconds_per_line", 1.5),
                )
            ),
            "clock": clock,
            "fuzzy_lookahead_lines": int(
                self.spec.params.get(
                    "fuzzy_lookahead_lines",
                    matcher_cfg.get(
                        "fuzzy_lookahead_lines", config.FUZZY_LOOKAHEAD_LINES
                    ),
                )
            ),
            "fuzzy_next_line_threshold": float(
                self.spec.params.get(
                    "fuzzy_next_line_threshold",
                    matcher_cfg.get(
                        "fuzzy_next_line_threshold",
                        config.FUZZY_NEXT_LINE_THRESHOLD,
                    ),
                )
            ),
            "recovery_lookahead_lines": int(
                self.spec.params.get(
                    "recovery_lookahead_lines",
                    matcher_cfg.get(
                        "recovery_lookahead_lines",
                        config.RECOVERY_LOOKAHEAD_LINES,
                    ),
                )
            ),
            "recovery_threshold": float(
                self.spec.params.get(
                    "recovery_threshold",
                    matcher_cfg.get(
                        "recovery_threshold", config.RECOVERY_THRESHOLD
                    ),
                )
            ),
            "duplicate_ttl": float(
                self.spec.params.get(
                    "duplicate_ttl",
                    matcher_cfg.get(
                        "duplicate_ttl",
                        asr_cfg.get("block_seconds", config.ASR_BLOCK_SECONDS),
                    ),
                )
            ),
            "min_coverage_ratio": float(
                self.spec.params.get(
                    "min_coverage_ratio",
                    matcher_cfg.get("min_coverage_ratio", 0.4),
                )
            ),
            "max_buffer_ratio": float(
                self.spec.params.get(
                    "max_buffer_ratio",
                    matcher_cfg.get("max_buffer_ratio", 1.8),
                )
            ),
            "retain_tail": bool(
                self._resolve_param("retain_tail", matcher_cfg, False)
            ),
            "short_line_guard": bool(
                self._resolve_param("short_line_guard", matcher_cfg, False)
            ),
            "short_line_max_len": int(
                self._resolve_param("short_line_max_len", matcher_cfg, 4)
            ),
            "smart_duplicate_ttl": bool(
                self._resolve_param("smart_duplicate_ttl", matcher_cfg, False)
            ),
            "display_mode": str(
                self._resolve_param("display_mode", matcher_cfg, "next")
            ),
            "early_display_progress_threshold": float(
                self._resolve_param(
                    "early_display_progress_threshold", matcher_cfg, 0.75
                )
            ),
            "alignment_mode": alignment_mode,
            "phonological_norm": bool(
                self._resolve_param("phonological_norm", matcher_cfg, False)
            ),
            "phono_particles": bool(
                self._resolve_param("phono_particles", matcher_cfg, True)
            ),
            "phono_long_vowels": bool(
                self._resolve_param("phono_long_vowels", matcher_cfg, True)
            ),
            "phono_drop_sokuon": bool(
                self._resolve_param("phono_drop_sokuon", matcher_cfg, True)
            ),
            "weighted_dakuten": bool(
                self._resolve_param("weighted_dakuten", matcher_cfg, False)
            ),
            "dakuten_cost": float(
                self._resolve_param("dakuten_cost", matcher_cfg, 0.35)
            ),
            "adjacent_pair": bool(
                self._resolve_param("adjacent_pair", matcher_cfg, False)
            ),
            "pair_threshold": (
                float(pair_threshold_raw)
                if pair_threshold_raw is not None
                else None
            ),
            "beam_width": int(
                self._resolve_param("beam_width", matcher_cfg, 4)
            ),
            "beam_min_sim": beam_min_sim,
            "beam_dakuten_score": float(
                self._resolve_param("beam_dakuten_score", matcher_cfg, 1.2)
            ),
            "beam_forward_bonus": float(
                self._resolve_param("beam_forward_bonus", matcher_cfg, 3.5)
            ),
            "beam_jump_penalty_per_char": float(
                self._resolve_param(
                    "beam_jump_penalty_per_char", matcher_cfg, 0.10
                )
            ),
            "beam_jump_penalty_per_line": float(
                self._resolve_param(
                    "beam_jump_penalty_per_line", matcher_cfg, 2.0
                )
            ),
            "beam_max_jump_penalty": float(
                self._resolve_param("beam_max_jump_penalty", matcher_cfg, 8.0)
            ),
            "beam_recovery_penalty": float(
                self._resolve_param("beam_recovery_penalty", matcher_cfg, 6.5)
            ),
            "beam_decay": float(
                self._resolve_param("beam_decay", matcher_cfg, 0.78)
            ),
            "beam_back_slack": int(
                self._resolve_param("beam_back_slack", matcher_cfg, 16)
            ),
            "beam_max_buffer_chars": int(
                self._resolve_param("beam_max_buffer_chars", matcher_cfg, 32)
            ),
            "beam_buffer_stale_seconds": float(
                self._resolve_param(
                    "beam_buffer_stale_seconds", matcher_cfg, 6.5
                )
            ),
            "asr_segment_filter": bool(
                self._resolve_param("asr_segment_filter", matcher_cfg, False)
            ),
            "asr_min_avg_logprob": float(
                self._resolve_param("asr_min_avg_logprob", matcher_cfg, -0.80)
            ),
            "asr_max_no_speech_prob": float(
                self._resolve_param("asr_max_no_speech_prob", matcher_cfg, 0.60)
            ),
            "asr_max_compression_ratio": float(
                self._resolve_param(
                    "asr_max_compression_ratio", matcher_cfg, 1.35
                )
            ),
            "position_prior_enabled": bool(
                self._resolve_param("position_prior_enabled", matcher_cfg, False)
            ),
            "position_prior_tolerance": float(
                self._resolve_param(
                    "position_prior_tolerance", matcher_cfg, 0.15
                )
            ),
            "position_prior_weight": float(
                self._resolve_param("position_prior_weight", matcher_cfg, 0.45)
            ),
            "position_prior_floor": float(
                self._resolve_param("position_prior_floor", matcher_cfg, 0.60)
            ),
            "seek_detection_enabled": bool(
                self._resolve_param("seek_detection_enabled", matcher_cfg, False)
            ),
            "seek_rewind_threshold_ms": int(
                self._resolve_param(
                    "seek_rewind_threshold_ms", matcher_cfg, 3500
                )
            ),
            "seek_forward_threshold_ms": int(
                self._resolve_param(
                    "seek_forward_threshold_ms", matcher_cfg, 12000
                )
            ),
            "seek_window_ratio_radius": float(
                self._resolve_param(
                    "seek_window_ratio_radius", matcher_cfg, 0.35
                )
            ),
            "emit_events": False,
        }
        self._asr_filter_active = bool(engine_kwargs["asr_segment_filter"])
        self._reconstruct_unfiltered = not self._asr_filter_active and (
            self.spec.name != "baseline"
            or "asr_segment_filter" in self.spec.params
        )
        self._last_poll_tuple = None
        self._last_est_ms = None
        self._engine = LyricSyncEngine(**engine_kwargs)
        self._engine.reset_with_lyrics(lyrics)

        init_state = session_start.get("initial_state") or {}
        if init_state and (
            init_state.get("lazy_opened")
            or int(init_state.get("current_index", -1)) >= 0
            or init_state.get("buffer")
            or init_state.get("last_success_normalized")
            or init_state.get("last_success_recognized")
            or init_state.get("seek_reanchor_active")
        ):
            self._engine.restore_state(
                current_index=int(init_state.get("current_index", -1)),
                buffer=str(init_state.get("buffer") or ""),
                last_success_normalized=str(
                    init_state.get("last_success_normalized") or ""
                ),
                last_success_recognized=str(
                    init_state.get("last_success_recognized") or ""
                ),
                elapsed_since_last_success=init_state.get(
                    "elapsed_since_last_success"
                ),
                elapsed_since_buffer_start=init_state.get(
                    "elapsed_since_buffer_start"
                ),
                beam_pos=init_state.get("beam_pos"),
                elapsed_since_beam_buffer=init_state.get(
                    "elapsed_since_beam_buffer"
                ),
                beam=init_state.get("beam"),
                seek_reanchor_active=bool(
                    init_state.get("seek_reanchor_active", False)
                ),
                seek_type=str(init_state.get("seek_type") or ""),
                seek_ref_index=int(init_state.get("seek_ref_index", -1)),
                pending_seek_event=init_state.get("pending_seek_event"),
            )

    def step(
        self,
        recognized_text: str,
        asr_meta: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        assert self._engine is not None
        meta_for_step = dict(asr_meta)
        text_for_step = recognized_text
        if (
            self._asr_filter_active
            and isinstance(meta_for_step.get("segments"), list)
            and meta_for_step["segments"]
        ):
            meta_for_step["segment_filtered"] = False
        elif (
            self._reconstruct_unfiltered
            and meta_for_step.get("segment_filtered")
            and "raw_text_before_filter" in meta_for_step
        ):
            reconstructed = str(meta_for_step.get("raw_text_before_filter") or "")
            for phrase in UNCONDITIONAL_HALLUCINATIONS:
                reconstructed = reconstructed.replace(phrase, "")
            text_for_step = reconstructed.strip()
            meta_for_step["text"] = text_for_step
        return self._engine.evaluate_recognized_text(
            text_for_step, asr_meta=meta_for_step
        )

    def apply_manual_jump(self, line_index: int) -> dict[str, Any] | None:
        assert self._engine is not None
        return self._engine.apply_manual_jump(line_index)

    def update_playback_progress(self, playback: dict[str, Any]) -> None:
        assert self._engine is not None
        if not playback:
            return
        sp_ms = playback.get("spotify_progress_ms")
        est_ms = playback.get("estimated_progress_ms")
        dur_ms = playback.get("duration_ms")
        if sp_ms is None and est_ms is None and dur_ms is None:
            self._last_poll_tuple = None
            self._last_est_ms = None
            self._engine.update_playback_progress(
                None,
                None,
                is_playing=bool(playback.get("is_playing", False)),
                estimated_progress_ms=None,
            )
            return

        is_playing = bool(playback.get("is_playing", True))
        poll_tuple = (sp_ms, dur_ms, is_playing)
        est_int = int(est_ms) if est_ms is not None else None
        if (
            sp_ms is not None
            and dur_ms is not None
            and self._last_poll_tuple == poll_tuple
            and est_int is not None
            and self._last_est_ms is not None
            and abs(est_int - int(sp_ms)) <= 7500
            and est_int >= (self._last_est_ms - 500)
        ):
            self._last_est_ms = est_int
            return

        poll_changed = poll_tuple != self._last_poll_tuple
        self._last_poll_tuple = poll_tuple
        self._last_est_ms = est_int
        self._engine.update_playback_progress(
            sp_ms,
            dur_ms,
            is_playing=is_playing,
            estimated_progress_ms=(
                est_int if (sp_ms is None or not poll_changed) else None
            ),
        )


class RtReadingStrategy(BaselineStrategy):
    """Recompute line ``normalized`` targets using UtaTen ``<rt>`` furigana readings."""

    def reset(
        self,
        lyrics: list[dict[str, Any]],
        session_start: dict[str, Any],
        clock: VirtualClock,
    ) -> None:
        updated_lyrics: list[dict[str, Any]] = []
        for line in lyrics:
            line_copy = dict(line)
            ruby_html = str(line_copy.get("ruby_html") or "")
            if ruby_html:
                reading, norm = extract_ruby_reading(ruby_html)
                if norm:
                    line_copy["reading"] = reading
                    line_copy["normalized"] = norm
            updated_lyrics.append(line_copy)

        updated_session = session_start
        init_state = session_start.get("initial_state")
        if isinstance(init_state, dict):
            try:
                cur_idx = int(init_state.get("current_index", -1))
            except (TypeError, ValueError):
                cur_idx = -1
            last_norm = str(init_state.get("last_success_normalized") or "")
            if 0 <= cur_idx < len(lyrics) and last_norm:
                old_norm = str(lyrics[cur_idx].get("normalized") or "")
                new_norm = str(updated_lyrics[cur_idx].get("normalized") or "")
                if last_norm == old_norm and new_norm and new_norm != old_norm:
                    init_copy = dict(init_state)
                    init_copy["last_success_normalized"] = new_norm
                    updated_session = dict(session_start)
                    updated_session["initial_state"] = init_copy

        super().reset(updated_lyrics, updated_session, clock)


_ISSUE3_LINE_OVERRIDES: dict[str, Any] = {
    "retain_tail": True,
    "short_line_guard": True,
    "smart_duplicate_ttl": True,
    "display_mode": "adaptive",
    "alignment_mode": "line",
    "phonological_norm": False,
    "weighted_dakuten": False,
    "adjacent_pair": False,
    "position_prior_enabled": False,
    "seek_detection_enabled": False,
}


class SmartSyncStrategy(BaselineStrategy):
    """Preset enabling tail retention, short-line guard, smart duplicate TTL, and adaptive display."""

    _DEFAULT_OVERRIDES = dict(_ISSUE3_LINE_OVERRIDES)


class RtSmartSyncStrategy(RtReadingStrategy):
    """Preset combining UtaTen ``<rt>`` readings with all Issue #3 matcher improvements."""

    _DEFAULT_OVERRIDES = dict(_ISSUE3_LINE_OVERRIDES)


class PhonoSyncStrategy(RtReadingStrategy):
    """Preset enabling Japanese singing phonological normalization on top of ``rt_smart_sync``."""

    _DEFAULT_OVERRIDES = {
        **_ISSUE3_LINE_OVERRIDES,
        "phonological_norm": True,
    }


class PhonoWeightedStrategy(RtReadingStrategy):
    """Preset combining singing phonological normalization with weighted dakuten edit distance."""

    _DEFAULT_OVERRIDES = {
        **_ISSUE3_LINE_OVERRIDES,
        "phonological_norm": True,
        "weighted_dakuten": True,
        "dakuten_cost": 0.35,
    }


class PairSyncStrategy(RtReadingStrategy):
    """Preset enabling cross-line adjacent pair candidates on top of ``rt_smart_sync``."""

    _DEFAULT_OVERRIDES = {
        **_ISSUE3_LINE_OVERRIDES,
        "adjacent_pair": True,
    }


class PhonoPairSyncStrategy(RtReadingStrategy):
    """Preset combining phonological norm, weighted dakuten distance, and adjacent line pairs."""

    _DEFAULT_OVERRIDES = {
        **_ISSUE3_LINE_OVERRIDES,
        "phonological_norm": True,
        "weighted_dakuten": True,
        "dakuten_cost": 0.35,
        "adjacent_pair": True,
    }


class BeamAlignStrategy(RtReadingStrategy):
    """Preset using continuous character timeline Smith-Waterman alignment and Top-K beam tracking."""

    _DEFAULT_OVERRIDES = {
        **_ISSUE3_LINE_OVERRIDES,
        "alignment_mode": "beam",
        "phonological_norm": True,
    }


class BeamAsrOptStrategy(BeamAlignStrategy):
    """Preset adding ASR segment confidence and interlude hallucination filtering on ``beam_align``."""

    _DEFAULT_OVERRIDES = {
        **BeamAlignStrategy._DEFAULT_OVERRIDES,
        "asr_segment_filter": True,
    }


class BeamPosPriorStrategy(BeamAsrOptStrategy):
    """Preset combining beam_asr_opt with Spotify soft position prior and seek/rewind detection."""

    _DEFAULT_OVERRIDES = {
        **BeamAsrOptStrategy._DEFAULT_OVERRIDES,
        "position_prior_enabled": True,
        "seek_detection_enabled": True,
    }


STRATEGIES: dict[str, type[ReplayStrategy]] = {
    "baseline": BaselineStrategy,
    "rt_reading": RtReadingStrategy,
    "smart_sync": SmartSyncStrategy,
    "rt_smart_sync": RtSmartSyncStrategy,
    "phono_sync": PhonoSyncStrategy,
    "phono_weighted": PhonoWeightedStrategy,
    "pair_sync": PairSyncStrategy,
    "phono_pair_sync": PhonoPairSyncStrategy,
    "beam_align": BeamAlignStrategy,
    "beam_asr_opt": BeamAsrOptStrategy,
    "beam_pos_prior": BeamPosPriorStrategy,
}


def parse_strategy_spec(raw_spec: str) -> StrategySpec:
    """Parse ``name[:key=value,...]`` into a validated ``StrategySpec``."""
    spec_text = (raw_spec or "").strip()
    if not spec_text:
        raise ValueError("策略名稱不可為空")

    if ":" in spec_text:
        name, param_str = spec_text.split(":", 1)
    else:
        name, param_str = spec_text, ""

    name = name.strip().lower()
    if name not in STRATEGIES:
        available = ", ".join(sorted(STRATEGIES))
        raise ValueError(f"不支援的策略 '{name}'，可用策略: {available}")

    params: dict[str, Any] = {}
    if param_str.strip():
        for pair in param_str.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                raise ValueError(f"策略參數格式錯誤 '{pair}'，應為 key=value")
            key, value_str = pair.split("=", 1)
            key = key.strip()
            value_str = value_str.strip()
            if key not in _BASELINE_PARAM_CASTERS:
                valid_keys = ", ".join(sorted(_BASELINE_PARAM_CASTERS))
                raise ValueError(
                    f"策略 '{name}' 不支援參數 '{key}'，可用參數: {valid_keys}"
                )
            target_field, caster = _BASELINE_PARAM_CASTERS[key]
            try:
                params[target_field] = caster(value_str)
            except ValueError as exc:
                raise ValueError(
                    f"參數 '{key}' 數值無效 '{value_str}': {exc}"
                ) from exc

    return StrategySpec(raw=spec_text, name=name, params=params)


def resolve_trace_paths(raw_paths: list[str]) -> list[Path]:
    """Expand files/directories into a deduplicated, sorted list of trace files."""
    resolved: list[Path] = []
    seen: set[Path] = set()

    for raw in raw_paths:
        path = Path(raw)
        if not path.exists():
            raise FileNotFoundError(f"找不到 Trace 路徑: {path}")
        if path.is_dir():
            for child in sorted(path.rglob("*.jsonl")):
                norm = child.resolve()
                if norm not in seen:
                    seen.add(norm)
                    resolved.append(child)
        else:
            norm = path.resolve()
            if norm not in seen:
                seen.add(norm)
                resolved.append(path)

    if not resolved:
        raise FileNotFoundError("指定的路徑下找不到任何 .jsonl Trace 檔案")
    return resolved


def format_ms(ms: int | float | None) -> str:
    """Format milliseconds into ``m:ss`` or ``-``."""
    if ms is None:
        return "-"
    total_sec = max(0, int(round(float(ms) / 1000.0)))
    minutes, seconds = divmod(total_sec, 60)
    return f"{minutes}:{seconds:02d}"


def replay_trace(
    trace_data: dict[str, Any],
    spec: StrategySpec,
    *,
    anchor_mode: str = "eval",
) -> dict[str, Any]:
    """Replay one parsed trace through a strategy and compute evaluation metrics."""
    if anchor_mode not in {"eval", "apply"}:
        raise ValueError("anchor_mode 必須為 'eval' 或 'apply'")

    session_start = trace_data["session_start"]
    lyrics = list(session_start.get("lyrics") or [])
    events_list = list(trace_data.get("events") or [])
    asr_cfg = session_start.get("asr_config") or {}
    step_budget_ms = float(
        asr_cfg.get("step_seconds") or (config.ASR_BLOCK_SECONDS / 2)
    ) * 1000.0

    base_time = 1000.0
    clock = VirtualClock(base_time)
    strategy = STRATEGIES[spec.name](spec)
    strategy.reset(lyrics, session_start, clock)

    init_state = session_start.get("initial_state") or {}
    init_idx = int(init_state.get("current_index", -1))
    lazy_opened = bool(init_state.get("lazy_opened", False))
    if lyrics:
        if init_idx >= 0:
            clamped_idx = min(init_idx, len(lyrics) - 1)
            pred_display_index = min(clamped_idx + 1, len(lyrics) - 1)
            prev_matched_index: int | None = clamped_idx
        else:
            pred_display_index = 0
            prev_matched_index = None
    else:
        pred_display_index = -1
        prev_matched_index = None
    matched_unique_lines: set[int] = set()
    match_scores: list[float] = []
    infer_ms_values: list[float] = []
    infer_over_budget_count = 0

    asr_steps = 0
    non_empty_steps = 0
    matches_p1 = 0
    matches_p2 = 0
    sequential_advances = 0
    jump_advances = 0
    max_jump = 0
    parity_matches = 0
    tails_retained = 0
    early_display_holds = 0
    seek_reanchors = 0
    rewind_advances = 0

    anchor_evals: list[dict[str, Any]] = []
    timeline_rows: list[dict[str, Any]] = []

    for event in events_list:
        elapsed_sec = float(event.get("elapsed_sec") or 0.0)
        clock.now = base_time + elapsed_sec
        ev_type = event.get("type")

        if ev_type == "asr_step":
            asr_steps += 1
            asr_info = dict(event.get("asr") or {})
            if "text" in asr_info:
                raw_text = str(asr_info.get("text") or "")
            else:
                raw_text = str(asr_info.get("raw_text") or "")
            if raw_text.strip():
                non_empty_steps += 1

            infer_ms = asr_info.get("infer_ms")
            if infer_ms is not None:
                infer_val = float(infer_ms)
                infer_ms_values.append(infer_val)
                if infer_val > step_budget_ms:
                    infer_over_budget_count += 1

            strategy.update_playback_progress(event.get("playback") or {})
            match_payload, step_trace = strategy.step(raw_text, asr_info)
            decision = str(step_trace.get("decision") or "")
            matched_idx = step_trace.get("matched_index")
            display_idx = step_trace.get("display_index")

            if (
                decision == event.get("decision")
                and matched_idx == event.get("matched_index")
            ):
                parity_matches += 1

            if match_payload is not None and matched_idx is not None:
                matched_idx = int(matched_idx)
                display_idx = int(display_idx)
                pred_display_index = display_idx
                matched_unique_lines.add(matched_idx)
                match_scores.append(float(match_payload["score"]))

                if decision == "matched_phase2_recovery":
                    matches_p2 += 1
                else:
                    matches_p1 += 1

                if step_trace.get("retained_tail"):
                    tails_retained += 1
                if step_trace.get("early_display_hold"):
                    early_display_holds += 1
                if step_trace.get("seek_reanchored"):
                    seek_reanchors += 1

                if prev_matched_index is not None:
                    delta = matched_idx - prev_matched_index
                    if delta == 1:
                        sequential_advances += 1
                    elif delta >= 2:
                        jump_advances += 1
                        max_jump = max(max_jump, delta)
                    elif delta < 0:
                        rewind_advances += 1
                prev_matched_index = matched_idx

            timeline_rows.append(
                {
                    "type": "asr_step",
                    "seq": event.get("seq"),
                    "elapsed_sec": elapsed_sec,
                    "playback": event.get("playback") or {},
                    "asr": asr_info,
                    "raw_text": raw_text,
                    "buffer_after_trim": step_trace.get(
                        "buffer_after_trim", ""
                    ),
                    "candidates": list(step_trace.get("candidates") or []),
                    "decision": decision,
                    "matched_index": matched_idx,
                    "display_index": display_idx,
                    "best_score": step_trace.get("best_score"),
                    "threshold": step_trace.get("threshold"),
                    "seek_detected": step_trace.get("seek_detected"),
                    "seek_reanchored": bool(
                        step_trace.get("seek_reanchored", False)
                    ),
                    "pred_display_index": pred_display_index,
                    "recorded_decision": event.get("decision"),
                    "recorded_matched_index": event.get("matched_index"),
                }
            )

        elif ev_type == "manual_jump":
            strategy.update_playback_progress(event.get("playback") or {})
            to_index = int(event.get("to_index", -1))
            error = (
                pred_display_index - to_index
                if pred_display_index >= 0 and to_index >= 0
                else None
            )
            anchor_record = {
                "seq": event.get("seq"),
                "elapsed_sec": elapsed_sec,
                "to_index": to_index,
                "pred_display_index": pred_display_index,
                "error": error,
                "abs_error": abs(error) if error is not None else None,
                "from_index_recorded": event.get("from_index"),
                "current_line_text": event.get("current_line_text", ""),
                "playback": event.get("playback") or {},
            }
            anchor_evals.append(anchor_record)

            if anchor_mode == "apply" and to_index >= 0:
                jump_res = strategy.apply_manual_jump(to_index)
                if jump_res is not None:
                    pred_display_index = to_index
                    prev_matched_index = to_index

            timeline_rows.append(
                {
                    "type": "manual_jump",
                    "seq": event.get("seq"),
                    "elapsed_sec": elapsed_sec,
                    "playback": event.get("playback") or {},
                    "to_index": to_index,
                    "pred_display_index": anchor_record["pred_display_index"],
                    "error": error,
                    "current_line_text": event.get("current_line_text", ""),
                    "applied": anchor_mode == "apply",
                }
            )

    total_transitions = sequential_advances + jump_advances
    abs_errors = [
        a["abs_error"] for a in anchor_evals if a["abs_error"] is not None
    ]
    anchor_exact = sum(1 for err in abs_errors if err == 0)
    anchor_within_1 = sum(1 for err in abs_errors if err <= 1)

    total_matches = matches_p1 + matches_p2
    total_lines = len(lyrics)
    parity_applicable = not (anchor_mode == "eval" and len(abs_errors) > 0)
    live_parity_rate = (
        round(parity_matches / asr_steps, 4)
        if (parity_applicable and asr_steps > 0)
        else (1.0 if parity_applicable else None)
    )

    summary = {
        "trace_path": trace_data["path"],
        "track": session_start.get("track") or {},
        "strategy": spec.raw,
        "anchor_mode": anchor_mode,
        "lazy_opened": lazy_opened,
        "total_lines": total_lines,
        "asr_steps": asr_steps,
        "non_empty_steps": non_empty_steps,
        "matches_p1": matches_p1,
        "matches_p2": matches_p2,
        "total_matches": total_matches,
        "unique_lines_matched": len(matched_unique_lines),
        "line_coverage": round(len(matched_unique_lines) / total_lines, 4)
        if total_lines > 0
        else 0.0,
        "sequential_advances": sequential_advances,
        "jump_advances": jump_advances,
        "sequential_rate": round(sequential_advances / total_transitions, 4)
        if total_transitions > 0
        else None,
        "max_jump": max_jump,
        "mean_match_score": round(sum(match_scores) / len(match_scores), 4)
        if match_scores
        else None,
        "anchors_total": len(abs_errors),
        "anchor_exact": anchor_exact,
        "anchor_within_1": anchor_within_1,
        "anchor_mae": round(sum(abs_errors) / len(abs_errors), 4)
        if abs_errors
        else None,
        "avg_infer_ms": round(sum(infer_ms_values) / len(infer_ms_values), 1)
        if infer_ms_values
        else None,
        "max_infer_ms": round(max(infer_ms_values), 1)
        if infer_ms_values
        else None,
        "infer_over_budget_count": infer_over_budget_count,
        "step_budget_ms": round(step_budget_ms, 1),
        "live_parity_rate": live_parity_rate,
        "parity_matches": parity_matches,
        "tails_retained": tails_retained,
        "early_display_holds": early_display_holds,
        "seek_reanchors": seek_reanchors,
        "rewind_advances": rewind_advances,
        "_parity_steps": asr_steps if parity_applicable else 0,
        "_parity_matches": parity_matches if parity_applicable else 0,
        "_score_sum": sum(match_scores),
        "_score_count": len(match_scores),
        "_anchor_err_sum": sum(abs_errors),
        "_infer_sum": sum(infer_ms_values),
        "_infer_count": len(infer_ms_values),
    }

    return {
        "summary": summary,
        "anchors": anchor_evals,
        "timeline": timeline_rows,
    }


def aggregate_summaries(
    runs: list[dict[str, Any]], strategy_raw: str, anchor_mode: str
) -> dict[str, Any]:
    """Aggregate per-trace summaries for a single strategy across multiple traces."""
    summaries = [run["summary"] for run in runs]
    total_lines = sum(s["total_lines"] for s in summaries)
    unique_matched = sum(s["unique_lines_matched"] for s in summaries)
    asr_steps = sum(s["asr_steps"] for s in summaries)
    non_empty_steps = sum(s["non_empty_steps"] for s in summaries)
    matches_p1 = sum(s["matches_p1"] for s in summaries)
    matches_p2 = sum(s["matches_p2"] for s in summaries)
    seq_adv = sum(s["sequential_advances"] for s in summaries)
    jmp_adv = sum(s["jump_advances"] for s in summaries)
    total_transitions = seq_adv + jmp_adv
    score_sum = sum(s["_score_sum"] for s in summaries)
    score_count = sum(s["_score_count"] for s in summaries)
    anchors_total = sum(s["anchors_total"] for s in summaries)
    anchor_exact = sum(s["anchor_exact"] for s in summaries)
    anchor_within_1 = sum(s["anchor_within_1"] for s in summaries)
    anchor_err_sum = sum(s["_anchor_err_sum"] for s in summaries)
    infer_sum = sum(s["_infer_sum"] for s in summaries)
    infer_count = sum(s["_infer_count"] for s in summaries)
    max_infers = [s["max_infer_ms"] for s in summaries if s["max_infer_ms"] is not None]
    parity_steps = sum(s["_parity_steps"] for s in summaries)
    parity_matches = sum(s["_parity_matches"] for s in summaries)
    has_any_parity_trace = any(s["live_parity_rate"] is not None for s in summaries)

    return {
        "traces_count": len(summaries),
        "strategy": strategy_raw,
        "anchor_mode": anchor_mode,
        "total_lines": total_lines,
        "asr_steps": asr_steps,
        "non_empty_steps": non_empty_steps,
        "matches_p1": matches_p1,
        "matches_p2": matches_p2,
        "total_matches": matches_p1 + matches_p2,
        "unique_lines_matched": unique_matched,
        "line_coverage": round(unique_matched / total_lines, 4)
        if total_lines > 0
        else 0.0,
        "sequential_advances": seq_adv,
        "jump_advances": jmp_adv,
        "sequential_rate": round(seq_adv / total_transitions, 4)
        if total_transitions > 0
        else None,
        "max_jump": max((s["max_jump"] for s in summaries), default=0),
        "mean_match_score": round(score_sum / score_count, 4)
        if score_count > 0
        else None,
        "anchors_total": anchors_total,
        "anchor_exact": anchor_exact,
        "anchor_within_1": anchor_within_1,
        "anchor_mae": round(anchor_err_sum / anchors_total, 4)
        if anchors_total > 0
        else None,
        "avg_infer_ms": round(infer_sum / infer_count, 1)
        if infer_count > 0
        else None,
        "max_infer_ms": round(max(max_infers), 1) if max_infers else None,
        "infer_over_budget_count": sum(
            s["infer_over_budget_count"] for s in summaries
        ),
        "live_parity_rate": round(parity_matches / parity_steps, 4)
        if parity_steps > 0
        else (1.0 if has_any_parity_trace else None),
        "tails_retained": sum(s.get("tails_retained", 0) for s in summaries),
        "early_display_holds": sum(
            s.get("early_display_holds", 0) for s in summaries
        ),
        "seek_reanchors": sum(s.get("seek_reanchors", 0) for s in summaries),
        "rewind_advances": sum(s.get("rewind_advances", 0) for s in summaries),
    }


def _fmt_pct(val: float | None, *, none_text: str = "-") -> str:
    return f"{val * 100:.1f}%" if val is not None else none_text


def _fmt_num(val: float | None, digits: int = 2) -> str:
    return f"{val:.{digits}f}" if val is not None else "-"


def format_comparison_markdown(rows: list[dict[str, Any]]) -> str:
    """Render strategy summary rows as a GitHub-flavored Markdown table."""
    headers = [
        "Strategy",
        "Steps (NonEmpty)",
        "Matches (P1+P2)",
        "Line Cov",
        "Seq (+1)",
        "Jumps (>=2)",
        "Max Jump",
        "Mean Score",
        "Tail/Hold",
        "Anchor Exact / ±1 / MAE",
        "Infer Avg/Max (Over)",
        "Live Parity",
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join([":---"] + ["---:"] * (len(headers) - 1)) + " |",
    ]
    for row in rows:
        anchor_str = (
            f"{row['anchor_exact']}/{row['anchor_within_1']}/{row['anchors_total']} "
            f"(MAE {_fmt_num(row['anchor_mae'], 2)})"
            if row["anchors_total"] > 0
            else "-"
        )
        infer_str = (
            f"{_fmt_num(row['avg_infer_ms'], 0)}/{_fmt_num(row['max_infer_ms'], 0)}ms "
            f"({row['infer_over_budget_count']})"
            if row["avg_infer_ms"] is not None
            else "-"
        )
        tail_hold_str = (
            f"{row.get('tails_retained', 0)}/{row.get('early_display_holds', 0)}"
        )
        cols = [
            f"`{row['strategy']}`",
            f"{row['asr_steps']} ({row['non_empty_steps']})",
            f"{row['total_matches']} ({row['matches_p1']}+{row['matches_p2']})",
            f"{row['unique_lines_matched']}/{row['total_lines']} ({_fmt_pct(row['line_coverage'])})",
            _fmt_pct(row["sequential_rate"]),
            str(row["jump_advances"]),
            str(row["max_jump"]),
            _fmt_num(row["mean_match_score"], 3),
            tail_hold_str,
            anchor_str,
            infer_str,
            _fmt_pct(row["live_parity_rate"], none_text="n/a"),
        ]
        lines.append("| " + " | ".join(cols) + " |")
    return "\n".join(lines)


def format_comparison_table(rows: list[dict[str, Any]]) -> str:
    """Render an aligned plain-text table for terminal display."""
    headers = [
        "Strategy",
        "Steps",
        "P1+P2",
        "LineCov",
        "Seq%",
        "Jump",
        "MaxJ",
        "Score",
        "Tail/Hold",
        "Anchor(Exact/±1/MAE)",
        "Infer(Avg/Max/Over)",
        "Parity",
    ]
    table_rows: list[list[str]] = []
    for row in rows:
        anchor_str = (
            f"{row['anchor_exact']}/{row['anchor_within_1']}/{row['anchors_total']}"
            f" ({_fmt_num(row['anchor_mae'], 2)})"
            if row["anchors_total"] > 0
            else "-"
        )
        infer_str = (
            f"{_fmt_num(row['avg_infer_ms'], 0)}/{_fmt_num(row['max_infer_ms'], 0)}ms"
            f" ({row['infer_over_budget_count']})"
            if row["avg_infer_ms"] is not None
            else "-"
        )
        tail_hold_str = (
            f"{row.get('tails_retained', 0)}/{row.get('early_display_holds', 0)}"
        )
        table_rows.append(
            [
                str(row["strategy"]),
                f"{row['asr_steps']}({row['non_empty_steps']})",
                f"{row['matches_p1']}+{row['matches_p2']}",
                f"{row['unique_lines_matched']}/{row['total_lines']} ({_fmt_pct(row['line_coverage'])})",
                _fmt_pct(row["sequential_rate"]),
                str(row["jump_advances"]),
                str(row["max_jump"]),
                _fmt_num(row["mean_match_score"], 3),
                tail_hold_str,
                anchor_str,
                infer_str,
                _fmt_pct(row["live_parity_rate"], none_text="n/a"),
            ]
        )

    widths = [
        max(len(headers[i]), *(len(r[i]) for r in table_rows))
        for i in range(len(headers))
    ]
    sep = " | "
    out = [
        sep.join(headers[i].ljust(widths[i]) for i in range(len(headers))),
        "-+-".join("-" * widths[i] for i in range(len(headers))),
    ]
    for r in table_rows:
        out.append(sep.join(r[i].ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(out)


def format_timeline(run: dict[str, Any]) -> str:
    """Format a step-by-step timeline for one trace and strategy."""
    summary = run["summary"]
    track = summary["track"]
    title = track.get("title") or track.get("song_name") or Path(summary["trace_path"]).name
    artist = track.get("artist") or track.get("artist_name") or ""
    header = f"--- Timeline: {artist} - {title} [{summary['strategy']}] ---"
    lines = [header]

    for item in run["timeline"]:
        t_str = f"{item['elapsed_sec']:6.2f}s"
        pb = item.get("playback") or {}
        sp_str = format_ms(pb.get("estimated_progress_ms"))
        if item["type"] == "manual_jump":
            err = item.get("error")
            err_txt = f"{err:+d}" if err is not None else "-"
            tag = "APPLIED" if item.get("applied") else "EVAL"
            lines.append(
                f"  #{item['seq']:<3} {t_str} [SP {sp_str}] [MANUAL_JUMP:{tag}] "
                f"to_line={item['to_index'] + 1} (pred={item['pred_display_index'] + 1}, err={err_txt}) "
                f"'{item.get('current_line_text', '')}'"
            )
            continue

        asr = item.get("asr") or {}
        infer = asr.get("infer_ms")
        nsp = asr.get("no_speech_prob_max")
        logp = asr.get("avg_logprob_mean")
        meta_parts = []
        if infer is not None:
            meta_parts.append(f"infer={infer:.0f}ms")
        if nsp is not None:
            meta_parts.append(f"nsp={nsp:.2f}")
        if logp is not None:
            meta_parts.append(f"logp={logp:.2f}")
        meta_str = " ".join(meta_parts)

        top_cands = sorted(
            item.get("candidates") or [],
            key=lambda c: float(c.get("score", 0.0)),
            reverse=True,
        )[:3]
        cand_str = ", ".join(
            f"L{int(c['index']) + 1}:{float(c['score']):.2f}"
            for c in top_cands
        )

        decision = item["decision"]
        if item["matched_index"] is not None:
            dec_str = (
                f"{decision}->L{int(item['matched_index']) + 1} "
                f"(disp=L{int(item['display_index']) + 1}, "
                f"score={item['best_score']:.2f})"
            )
        else:
            best = (
                f"{item['best_score']:.2f}"
                if item.get("best_score") is not None
                else "-"
            )
            dec_str = f"{decision} (best={best})"

        seek_parts: list[str] = []
        seek_det = item.get("seek_detected")
        if isinstance(seek_det, dict):
            seek_parts.append(
                f"{seek_det.get('type', '?')} "
                f"{format_ms(seek_det.get('from_ms'))}->{format_ms(seek_det.get('to_ms'))}"
            )
        if item.get("seek_reanchored"):
            seek_parts.append("reanchored")
        seek_tag = f" [SEEK:{','.join(seek_parts)}]" if seek_parts else ""

        lines.append(
            f"  #{item['seq']:<3} {t_str} [SP {sp_str}]{seek_tag} "
            f"ASR='{item['raw_text']}' | buf='{item['buffer_after_trim']}' | "
            f"{meta_str} | top=[{cand_str}] | {dec_str}"
        )

    return "\n".join(lines)


def format_strategy_diffs(
    trace_path: str, runs_by_strategy: list[dict[str, Any]]
) -> str:
    """Format steps where two or more strategies produced different decisions."""
    if len(runs_by_strategy) < 2:
        return ""

    timelines = [run["timeline"] for run in runs_by_strategy]
    names = [run["summary"]["strategy"] for run in runs_by_strategy]
    step_count = min(len(tl) for tl in timelines)

    diff_blocks: list[str] = []
    for idx in range(step_count):
        items = [tl[idx] for tl in timelines]
        if items[0]["type"] != "asr_step":
            continue
        signatures = {
            (it["decision"], it["matched_index"], it["display_index"])
            for it in items
        }
        if len(signatures) <= 1:
            continue

        sample = items[0]
        block = [
            f"  Step #{sample['seq']} @ {sample['elapsed_sec']:.2f}s | ASR='{sample['raw_text']}'"
        ]
        for strat_name, it in zip(names, items):
            matched_lbl = (
                f"L{int(it['matched_index']) + 1}"
                if it["matched_index"] is not None
                else "-"
            )
            score_lbl = (
                f"{it['best_score']:.3f}"
                if it.get("best_score") is not None
                else "-"
            )
            block.append(
                f"    - {strat_name}: {it['decision']} matched={matched_lbl} "
                f"score={score_lbl} buf='{it['buffer_after_trim']}'"
            )
        diff_blocks.append("\n".join(block))

    header = f"--- Strategy Diffs ({Path(trace_path).name}) ---"
    if not diff_blocks:
        return f"{header}\n  (無決策差異)"
    return header + "\n" + "\n".join(diff_blocks)


def _strip_internal_fields(summary: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in summary.items() if not k.startswith("_")}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay match trace JSONL files and benchmark lyric matching strategies."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Trace .jsonl files or directories containing trace files.",
    )
    parser.add_argument(
        "--strategy",
        action="append",
        dest="strategies",
        default=[],
        help=(
            "Strategy spec to evaluate (repeatable), e.g. 'baseline', 'rt_reading', "
            "'smart_sync', 'rt_smart_sync', 'phono_sync', 'phono_weighted', "
            "'pair_sync', 'phono_pair_sync', 'beam_align', 'beam_asr_opt', "
            "'beam_pos_prior', or 'beam_pos_prior:pos_weight=0.45,seek_rewind_ms=3500'. "
            "Defaults to 'baseline'."
        ),
    )
    parser.add_argument(
        "--anchor-mode",
        choices=("eval", "apply"),
        default="eval",
        help=(
            "'eval' treats manual_jump events as ground-truth error checkpoints; "
            "'apply' replays manual jumps into the engine state."
        ),
    )
    parser.add_argument(
        "--timeline",
        action="store_true",
        help="Print per-step timeline details for each trace and strategy.",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="When comparing >= 2 strategies, print steps where decisions differ.",
    )
    parser.add_argument(
        "--export-md",
        type=str,
        default="",
        help="Write Markdown comparison report to the specified file path.",
    )
    parser.add_argument(
        "--export-json",
        type=str,
        default="",
        help="Write structured evaluation results JSON to the specified file path.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    raw_strategies = args.strategies or ["baseline"]
    try:
        strategy_specs = [parse_strategy_spec(s) for s in raw_strategies]
        trace_paths = resolve_trace_paths(args.paths)
    except (ValueError, FileNotFoundError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    traces: list[dict[str, Any]] = []
    for path in trace_paths:
        try:
            traces.append(load_trace(path))
        except (ValueError, OSError) as exc:
            print(
                f"[WARN] 略過損毀或無法讀取的 Trace 檔案 ({path}): {exc}",
                file=sys.stderr,
            )

    if not traces:
        print("[ERROR] 沒有可重播的有效 Trace 檔案", file=sys.stderr)
        return 2

    runs_by_trace: list[list[dict[str, Any]]] = []
    runs_by_strategy: dict[str, list[dict[str, Any]]] = {
        spec.raw: [] for spec in strategy_specs
    }

    for trace in traces:
        per_trace_runs: list[dict[str, Any]] = []
        for spec in strategy_specs:
            run = replay_trace(trace, spec, anchor_mode=args.anchor_mode)
            per_trace_runs.append(run)
            runs_by_strategy[spec.raw].append(run)
        runs_by_trace.append(per_trace_runs)

    for trace, per_trace_runs in zip(traces, runs_by_trace):
        track = trace["session_start"].get("track") or {}
        title = track.get("title") or track.get("song_name") or Path(trace["path"]).name
        artist = track.get("artist") or track.get("artist_name") or "Unknown"
        flags = []
        if (trace["session_start"].get("initial_state") or {}).get("lazy_opened"):
            flags.append("lazy")
        if trace.get("truncated"):
            flags.append("truncated")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        print(
            f"\n=== Trace: {artist} - {title} ({Path(trace['path']).name}) "
            f"[anchor_mode={args.anchor_mode}]{flag_str} ==="
        )
        print(
            format_comparison_table(
                [_strip_internal_fields(r["summary"]) for r in per_trace_runs]
            )
        )
        if args.timeline:
            for run in per_trace_runs:
                print()
                print(format_timeline(run))
        if args.diff and len(per_trace_runs) >= 2:
            print()
            print(format_strategy_diffs(trace["path"], per_trace_runs))

    aggregated_rows = [
        aggregate_summaries(
            runs_by_strategy[spec.raw], spec.raw, args.anchor_mode
        )
        for spec in strategy_specs
    ]
    if len(traces) > 1:
        print(
            f"\n=== Overall Summary ({len(traces)} traces, anchor_mode={args.anchor_mode}) ==="
        )
        print(format_comparison_table(aggregated_rows))

    if args.export_md:
        md_sections = [
            f"# Match Evaluation Report (`anchor_mode={args.anchor_mode}`)",
            "",
            f"- Traces evaluated: {len(traces)}",
            "",
            "## Overall Comparison",
            "",
            format_comparison_markdown(aggregated_rows),
        ]
        if len(traces) > 1:
            md_sections.extend(["", "## Per-Trace Results"])
            for trace, per_trace_runs in zip(traces, runs_by_trace):
                track = trace["session_start"].get("track") or {}
                title = (
                    track.get("title")
                    or track.get("song_name")
                    or Path(trace["path"]).name
                )
                artist = track.get("artist") or track.get("artist_name") or ""
                md_sections.extend(
                    [
                        "",
                        f"### {artist} - {title} (`{Path(trace['path']).name}`)",
                        "",
                        format_comparison_markdown(
                            [
                                _strip_internal_fields(r["summary"])
                                for r in per_trace_runs
                            ]
                        ),
                    ]
                )
        md_path = Path(args.export_md)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("\n".join(md_sections) + "\n", encoding="utf-8")
        print(f"\n[OK] 已匯出 Markdown 報表: {md_path}")

    if args.export_json:
        json_payload = {
            "anchor_mode": args.anchor_mode,
            "overall": aggregated_rows,
            "traces": [
                {
                    "path": trace["path"],
                    "track": trace["session_start"].get("track") or {},
                    "results": [
                        {
                            "summary": _strip_internal_fields(run["summary"]),
                            "anchors": run["anchors"],
                        }
                        for run in per_trace_runs
                    ],
                }
                for trace, per_trace_runs in zip(traces, runs_by_trace)
            ],
        }
        json_path = Path(args.export_json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(json_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[OK] 已匯出 JSON 報表: {json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
