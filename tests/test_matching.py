"""Unit tests for RapidFuzz matching, tail retention, short-line guard, smart duplicate TTL, and display timing."""

from pathlib import Path
import sys
import tempfile
import unittest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = str(_REPO_ROOT / "src")
_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import eval_matching  # noqa: E402
from utaten_finder.lyrics.matching import (  # noqa: E402
    LyricsAgent,
    LyricSyncEngine,
    calculate_similarity_normalized,
    normalize_match_text,
    normalize_to_hiragana,
    score_similarity_detailed,
)
from utaten_finder.lyrics.trace import MatchTraceRecorder, load_trace  # noqa: E402


class _FakeClock:
    def __init__(self, initial: float = 100.0) -> None:
        self.now = float(initial)

    def __call__(self) -> float:
        return self.now


def _line(index: int, text: str, ruby_html: str = "") -> dict:
    return {
        "index": index,
        "original": text,
        "normalized": normalize_match_text(normalize_to_hiragana(text)),
        "ruby_html": ruby_html or text,
        "stanza_break": False,
    }


class TestRapidFuzzAndTailRetention(unittest.TestCase):
    def test_score_similarity_detailed_alignment_and_parity(self) -> None:
        empty = score_similarity_detailed("", "abc")
        self.assertEqual(empty["final_score"], 0.0)
        self.assertEqual(empty["src_end"], 0)
        self.assertEqual(empty["dest_end"], 0)

        buf = "やさしいうそをついてくれよげんじつは"
        target = "やさしいうそをついてくれよ"
        detailed = score_similarity_detailed(buf, target, min_coverage_ratio=0.4)
        self.assertEqual(
            detailed["final_score"],
            calculate_similarity_normalized(buf, target, min_coverage_ratio=0.4),
        )
        self.assertEqual(detailed["src_start"], 0)
        self.assertEqual(detailed["src_end"], len(target))
        self.assertEqual(detailed["dest_start"], 0)
        self.assertEqual(detailed["dest_end"], len(target))
        self.assertEqual(buf[detailed["src_end"] :], "げんじつは")

        agent = LyricsAgent()
        agent.asr_buffer = buf
        self.assertEqual(agent.consume_prefix(detailed["src_end"]), "げんじつは")
        self.assertEqual(agent.consume_prefix(0), "")
        agent.asr_buffer = "abc"
        self.assertEqual(agent.consume_prefix(3), "")

    def test_retain_tail_carries_next_line_prefix_across_match_boundary(self) -> None:
        lyrics = [
            _line(0, "やさしいうそをついてくれよ"),
            _line(1, "げんじつはざんこくだものだね"),
        ]

        clock_keep = _FakeClock(0.0)
        eng_keep = LyricSyncEngine(
            clock=clock_keep,
            emit_events=False,
            retain_tail=True,
            short_line_guard=True,
            smart_duplicate_ttl=True,
            display_mode="adaptive",
        )
        eng_keep.reset_with_lyrics(lyrics)

        clock_keep.now = 2.0
        m0_keep, t0_keep = eng_keep.evaluate_recognized_text("やさしいうそをついてくれよげんじつは")
        self.assertIsNotNone(m0_keep)
        assert m0_keep is not None
        self.assertEqual(m0_keep["matched_index"], 0)
        self.assertEqual(t0_keep["retained_tail"], "げんじつは")
        self.assertEqual(eng_keep.get_buffer_length(), len("げんじつは"))

        clock_keep.now = 4.2
        m1_keep, _ = eng_keep.evaluate_recognized_text("ざんこ")
        self.assertIsNotNone(m1_keep)
        assert m1_keep is not None
        self.assertEqual(m1_keep["matched_index"], 1)

        # Without retain_tail, "げんじつは" is wiped and short "ざんこ" falls below coverage/threshold.
        clock_drop = _FakeClock(0.0)
        eng_drop = LyricSyncEngine(
            clock=clock_drop,
            emit_events=False,
            retain_tail=False,
            short_line_guard=True,
            smart_duplicate_ttl=True,
            display_mode="next",
        )
        eng_drop.reset_with_lyrics(lyrics)

        clock_drop.now = 2.0
        m0_drop, t0_drop = eng_drop.evaluate_recognized_text("やさしいうそをついてくれよげんじつは")
        self.assertIsNotNone(m0_drop)
        self.assertEqual(t0_drop["retained_tail"], "")
        self.assertEqual(eng_drop.get_buffer_length(), 0)

        clock_drop.now = 4.2
        m1_drop, _ = eng_drop.evaluate_recognized_text("ざんこ")
        self.assertIsNone(m1_drop)


class TestShortLineGuard(unittest.TestCase):
    def test_two_char_target_blocked_in_unrelated_buffer_but_allowed_when_exact(self) -> None:
        lyrics = [
            _line(0, "あいうえおかきくけこ"),
            _line(1, "ほしぞらをみあげてる"),
            _line(2, "あか"),
        ]

        # Guard off: random "あか" inside longer unrelated sentence false-matches via partial_ratio=1.0.
        clock_unprot = _FakeClock(0.0)
        eng_unprotected = LyricSyncEngine(
            clock=clock_unprot,
            emit_events=False,
            short_line_guard=False,
        )
        eng_unprotected.reset_with_lyrics(lyrics)
        eng_unprotected.apply_manual_jump(0)
        eng_unprotected.process_recognized_text("お")
        clock_unprot.now = 4.0  # expands phase1 lookahead to cover L2 ("あか")
        self.assertIsNotNone(
            eng_unprotected.process_recognized_text("きょうもあかるいそら")
        )

        # Guard on: rejected in unrelated long buffer, accepted when spoken cleanly.
        clock_guard = _FakeClock(0.0)
        eng_guarded = LyricSyncEngine(
            clock=clock_guard,
            emit_events=False,
            short_line_guard=True,
        )
        eng_guarded.reset_with_lyrics(lyrics)
        eng_guarded.apply_manual_jump(0)
        eng_guarded.process_recognized_text("お")
        clock_guard.now = 4.0
        self.assertIsNone(
            eng_guarded.process_recognized_text("きょうもあかるいそら")
        )

        eng_guarded.apply_manual_jump(1)
        exact_match = eng_guarded.process_recognized_text("あか")
        self.assertIsNotNone(exact_match)
        assert exact_match is not None
        self.assertEqual(exact_match["matched_index"], 2)

    def test_three_to_four_char_guard_blocks_noise_and_keeps_valid_tail_and_longer_winner(
        self,
    ) -> None:
        # 1. 4-char 1-char substitution (0.75 < 0.90) is rejected while exact 4-char passes.
        clock = _FakeClock(0.0)
        eng = LyricSyncEngine(clock=clock, emit_events=False, short_line_guard=True)
        eng.reset_with_lyrics([_line(0, "さようなら"), _line(1, "かわくよ")])
        eng.apply_manual_jump(0)
        self.assertIsNone(eng.process_recognized_text("かわるよ"))
        eng.apply_manual_jump(0)
        ok = eng.process_recognized_text("かわくよ")
        self.assertIsNotNone(ok)

        # 2. Skipping ahead: fresh tail match ("かぜなびく") passes, stale early match blocked.
        lyrics_skip = [
            _line(0, "くさをぬけ"),
            _line(1, "だれもいないみちをずっとあるきつづけるよ"),
            _line(2, "だれもいないみち"),
            _line(3, "なびく"),
        ]
        clock2 = _FakeClock(0.0)
        eng2 = LyricSyncEngine(clock=clock2, emit_events=False, short_line_guard=True)
        eng2.reset_with_lyrics(lyrics_skip)
        eng2.apply_manual_jump(0)

        # Prime buffer timestamp at t=0.0, then advance to t=10.0 so lookahead covers L3.
        eng2.process_recognized_text("あ")
        clock2.now = 10.0
        # Stale "なびく" buried early before unrelated tail -> blocked on skip.
        self.assertIsNone(eng2.process_recognized_text("なびくぜんぜんちがうことば"))

        eng2.apply_manual_jump(0)
        clock2.now = 10.0
        eng2.process_recognized_text("お")
        clock2.now = 20.0
        fresh_tail = eng2.process_recognized_text("わたしをおいかぜなびく")
        self.assertIsNotNone(fresh_tail)
        assert fresh_tail is not None
        self.assertEqual(fresh_tail["matched_index"], 3)

        # 3. Longer passing candidate wins even if short candidate has raw score 0.75 > 0.0 (fails 0.90).
        lyrics_comp = [
            _line(0, "いちぎょうめ"),
            _line(1, "かわくよ"),
            _line(2, "きょうもはれわたるあおぞら"),
        ]
        clock3 = _FakeClock(0.0)
        eng3 = LyricSyncEngine(clock=clock3, emit_events=False, short_line_guard=True)
        eng3.reset_with_lyrics(lyrics_comp)
        eng3.apply_manual_jump(0)
        clock3.now = 2.0
        # Buffer contains 1-char typo "かわるよ" (0.75 vs L1, fails 0.90) + L2 match (~0.80, passes 0.75).
        comp_match = eng3.process_recognized_text("かわるよきょうもはれわたるあおぞ")
        self.assertIsNotNone(comp_match)
        assert comp_match is not None
        self.assertEqual(comp_match["matched_index"], 2)

        # 4. Phase 2 recovery also enforces short-line guard.
        lyrics_p2 = [_line(i, f"ながいおなじ歌詞の行その{i}だよ") for i in range(6)] + [
            _line(6, "かわくよ")
        ]
        clock4 = _FakeClock(0.0)
        eng4 = LyricSyncEngine(clock=clock4, emit_events=False, short_line_guard=True)
        eng4.reset_with_lyrics(lyrics_p2)
        eng4.apply_manual_jump(0)
        clock4.now = 0.0
        eng4.process_recognized_text("あ")
        clock4.now = 10.0
        # 3/4 = 0.75 >= recovery_threshold (0.60), but short-line guard requires 0.90.
        self.assertIsNone(eng4.process_recognized_text("かわるよ"))


class TestSmartDuplicateTTL(unittest.TestCase):
    def test_consecutive_identical_and_prefix_repeated_lines_not_swallowed(self) -> None:
        lyrics = [
            _line(0, "間違ってないよな"),
            _line(1, "間違ってないよな"),
            _line(2, "だから僕は音楽を辞めた"),
            _line(3, "だから僕は"),
        ]

        # Legacy duplicate_ttl swallows L1 ("まちがってないよな") at dt=2.2s.
        clock_legacy = _FakeClock(0.0)
        eng_legacy = LyricSyncEngine(
            clock=clock_legacy,
            emit_events=False,
            smart_duplicate_ttl=False,
            duplicate_ttl=4.0,
        )
        eng_legacy.reset_with_lyrics(lyrics)
        clock_legacy.now = 2.0
        self.assertEqual(eng_legacy.process_recognized_text("まちがってないよな")["matched_index"], 0)
        clock_legacy.now = 4.2
        _, trace_leg = eng_legacy.evaluate_recognized_text("まちがってないよな")
        self.assertEqual(trace_leg["decision"], "skipped_duplicate_ttl")

        # Smart duplicate_ttl debounces rapid (<1.5s) echoes but allows consecutive repeat at 2.2s.
        clock_smart = _FakeClock(0.0)
        eng_smart = LyricSyncEngine(
            clock=clock_smart,
            emit_events=False,
            smart_duplicate_ttl=True,
            duplicate_ttl=4.0,
        )
        eng_smart.reset_with_lyrics(lyrics)
        clock_smart.now = 2.0
        self.assertEqual(eng_smart.process_recognized_text("まちがってないよな")["matched_index"], 0)
        clock_smart.now = 2.8  # dt = 0.8s <= 1.5s -> rapid echo still suppressed
        _, rapid = eng_smart.evaluate_recognized_text("まちがってないよな")
        self.assertEqual(rapid["decision"], "skipped_duplicate_ttl")

        clock_smart.now = 4.2  # dt = 2.2s > 1.5s -> second identical line matches L1!
        m1, _ = eng_smart.evaluate_recognized_text("まちがってないよな")
        self.assertIsNotNone(m1)
        assert m1 is not None
        self.assertEqual(m1["matched_index"], 1)

        # Prefix-repeated pair: L2 ("だから僕は音楽を辞めた") -> L3 ("だから僕は") at dt=2.2s.
        clock_smart.now = 7.0
        m2 = eng_smart.process_recognized_text("だから僕は音楽を辞めた")
        self.assertIsNotNone(m2)
        assert m2 is not None
        self.assertEqual(m2["matched_index"], 2)

        clock_smart.now = 9.2
        m3 = eng_smart.process_recognized_text("だから僕は")
        self.assertIsNotNone(m3)
        assert m3 is not None
        self.assertEqual(m3["matched_index"], 3)

    def test_asr_echo_and_typo_remnant_suppressed_on_non_repeated_progression(self) -> None:
        lyrics = [
            _line(0, "どうでもいいんだ"),
            _line(1, "間違ってないだろ"),
            _line(2, "忘れないように色褪せないように"),
            _line(3, "形に残るものが全てじゃないように"),
        ]
        clock = _FakeClock(0.0)
        eng = LyricSyncEngine(
            clock=clock,
            emit_events=False,
            smart_duplicate_ttl=True,
            duplicate_ttl=4.0,
        )
        eng.reset_with_lyrics(lyrics)

        clock.now = 2.0
        # Recognized text has extra trailing "う" so it is not a substring of target "まちがってないだろ".
        m1 = eng.process_recognized_text("間違ってないだろう")
        self.assertIsNotNone(m1)
        assert m1 is not None
        self.assertEqual(m1["matched_index"], 1)

        # Exact ASR echo at dt=2.2s -> suppressed by _last_success_recognized!
        clock.now = 4.2
        m_echo, t_echo = eng.evaluate_recognized_text("間違ってないだろう")
        self.assertIsNone(m_echo)
        self.assertEqual(t_echo["decision"], "skipped_duplicate_ttl")

        # Match L2, then feed 1-char-typo remnant ("いろあせないように" vs "いろせないように").
        clock.now = 9.0
        m2 = eng.process_recognized_text("忘れないように色褪せないように")
        self.assertIsNotNone(m2)
        assert m2 is not None
        self.assertEqual(m2["matched_index"], 2)

        clock.now = 11.2
        m_remnant, t_remnant = eng.evaluate_recognized_text("いろせないように")
        self.assertIsNone(m_remnant)
        self.assertEqual(t_remnant["decision"], "skipped_duplicate_ttl")


class TestAdaptiveDisplayModeAndAnchorMAE(unittest.TestCase):
    def test_adaptive_display_holds_on_early_prefix_and_minimizes_anchor_mae(self) -> None:
        lyrics = [
            _line(0, "なつのこかげにすわったままひょうかをくちにほうりこんでかぜをまっていた"),
            _line(1, "もうわすれてしまったかなよのなかのぜんぶうそだらけ"),
            _line(2, "ほんとうのかちをふたりでさがしにいこうとわらったこと"),
        ]

        # Record a synthetic trace with manual_jump ground-truth checkpoints:
        # - At t=2.5s (singer is still midway through line 0 after matching early prefix) -> ground truth L0 (index 0)
        # - At t=7.0s (singer finished line 1) -> ground truth L2 (index 2)
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = _FakeClock(0.0)
            recorder = MatchTraceRecorder(
                traces_dir=Path(tmpdir),
                is_enabled_fn=lambda: True,
                clock=clock,
            )
            eng = LyricSyncEngine(
                trace_recorder=recorder,
                clock=clock,
                emit_events=False,
                retain_tail=True,
                short_line_guard=True,
                smart_duplicate_ttl=True,
                display_mode="adaptive",
            )
            eng.reset_with_lyrics(lyrics, track_meta={"song_name": "AnchorTest"})

            clock.now = 2.0
            m0, t0 = eng.evaluate_recognized_text("夏の木陰に座ったまま")
            self.assertIsNotNone(m0)
            assert m0 is not None
            self.assertTrue(m0["early_display_hold"])
            self.assertEqual(m0["display_index"], 0)
            self.assertTrue(t0["early_display_hold"])

            clock.now = 2.5
            eng.apply_manual_jump(0)

            clock.now = 6.5
            m1, t1 = eng.evaluate_recognized_text("もう忘れてしまったかな世の中の全部嘘だらけ")
            self.assertIsNotNone(m1)
            assert m1 is not None
            self.assertFalse(m1["early_display_hold"])
            self.assertEqual(m1["display_index"], 2)
            self.assertFalse(t1["early_display_hold"])

            clock.now = 7.0
            eng.apply_manual_jump(2)

            trace_path = recorder.current_trace_path
            assert trace_path is not None
            eng.close_trace_session("done", clear_pending=True)

            loaded = load_trace(trace_path)
            run_baseline = eval_matching.replay_trace(
                loaded,
                eval_matching.parse_strategy_spec("baseline"),
                anchor_mode="eval",
            )
            run_next = eval_matching.replay_trace(
                loaded,
                eval_matching.parse_strategy_spec("smart_sync:display_mode=next"),
                anchor_mode="eval",
            )
            run_adaptive = eval_matching.replay_trace(
                loaded,
                eval_matching.parse_strategy_spec("smart_sync"),
                anchor_mode="eval",
            )
            run_current = eval_matching.replay_trace(
                loaded,
                eval_matching.parse_strategy_spec("smart_sync:display_mode=current"),
                anchor_mode="eval",
            )

            self.assertEqual(run_baseline["summary"]["live_parity_rate"], None)
            # In apply mode, baseline replays matcher_config from the recorded trace -> 100% live parity.
            run_apply = eval_matching.replay_trace(
                loaded,
                eval_matching.parse_strategy_spec("baseline"),
                anchor_mode="apply",
            )
            self.assertEqual(run_apply["summary"]["live_parity_rate"], 1.0)

            # Adaptive achieves exact 0.0 MAE on both mid-line and post-line checkpoints.
            self.assertEqual(run_adaptive["summary"]["anchor_mae"], 0.0)
            self.assertEqual(run_adaptive["summary"]["anchor_exact"], 2)
            self.assertEqual(run_next["summary"]["anchor_mae"], 0.5)
            self.assertEqual(run_current["summary"]["anchor_mae"], 0.5)

    def test_eval_matching_parses_issue3_strategies_and_rejects_invalid_params(self) -> None:
        spec = eval_matching.parse_strategy_spec(
            "rt_smart_sync:retain_tail=true,short_guard=true,smart_dup=true,display_mode=adaptive"
        )
        self.assertEqual(spec.name, "rt_smart_sync")
        self.assertTrue(spec.params["retain_tail"])
        self.assertTrue(spec.params["short_line_guard"])
        self.assertTrue(spec.params["smart_duplicate_ttl"])
        self.assertEqual(spec.params["display_mode"], "adaptive")

        spec_plain = eval_matching.parse_strategy_spec("smart_sync")
        self.assertEqual(spec_plain.name, "smart_sync")

        with self.assertRaises(ValueError):
            eval_matching.parse_strategy_spec("smart_sync:retain_tail=maybe")
        with self.assertRaises(ValueError):
            eval_matching.parse_strategy_spec("smart_sync:display_mode=invalid")
        with self.assertRaises(ValueError):
            LyricSyncEngine(display_mode="invalid")


if __name__ == "__main__":
    unittest.main()
