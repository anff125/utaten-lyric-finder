"""Unit tests for RapidFuzz matching, tail retention, short-line guard, smart duplicate TTL, and display timing."""

from pathlib import Path
import sys
import tempfile
import unittest

from rapidfuzz import fuzz

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
    filter_asr_segments,
    has_repetitive_token_loop,
    normalize_match_text,
    normalize_to_hiragana,
    score_similarity_detailed,
)
from utaten_finder.lyrics.phonology import (  # noqa: E402
    ContinuousLyricsTimeline,
    collapse_japanese_long_vowels,
    normalize_singing_phonology,
    score_similarity_weighted_detailed,
    weighted_indel_ratio,
    weighted_partial_ratio_alignment,
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
            alignment_mode="line",
            phonological_norm=False,
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
            alignment_mode="line",
            phonological_norm=False,
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
            alignment_mode="line",
            phonological_norm=False,
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
            alignment_mode="line",
            phonological_norm=False,
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
        eng = LyricSyncEngine(
            clock=clock,
            emit_events=False,
            alignment_mode="line",
            phonological_norm=False,
            short_line_guard=True,
        )
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
        eng2 = LyricSyncEngine(
            clock=clock2,
            emit_events=False,
            alignment_mode="line",
            phonological_norm=False,
            short_line_guard=True,
        )
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
        eng3 = LyricSyncEngine(
            clock=clock3,
            emit_events=False,
            alignment_mode="line",
            phonological_norm=False,
            short_line_guard=True,
        )
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
        eng4 = LyricSyncEngine(
            clock=clock4,
            emit_events=False,
            alignment_mode="line",
            phonological_norm=False,
            short_line_guard=True,
        )
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
            alignment_mode="line",
            phonological_norm=False,
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
            alignment_mode="line",
            phonological_norm=False,
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
            eval_matching.parse_strategy_spec("beam_align:align_mode=invalid")
        with self.assertRaises(ValueError):
            LyricSyncEngine(display_mode="invalid")
        with self.assertRaises(ValueError):
            LyricSyncEngine(alignment_mode="invalid")


class TestSingingPhonologyAndWeightedDistance(unittest.TestCase):
    def test_singing_phonology_normalization_rules_and_ascii_passthrough(self) -> None:
        # Particles は/わ, へ/え, を/お + yotsugana じ/ぢ, ず/づ
        self.assertEqual(
            normalize_singing_phonology("わたしはそらへゆめをぢづゐゑ"),
            "わたしわそらえゆめおじずいえ",
        )
        # Small vowels + long vowels + sokuon
        self.assertEqual(normalize_singing_phonology("ねぇさぁとうとうまちぼうけ"), "ねさととまちぼけ")
        self.assertEqual(normalize_singing_phonology("とっとがらんどう"), "ととがらんど")
        # Middle dots & decorative punctuation stripped, ASCII preserved
        self.assertEqual(
            normalize_singing_phonology(normalize_match_text("はいうぇい・すたー♪")),
            "わいうえいすた",
        )
        self.assertEqual(normalize_singing_phonology("hyper star"), "hyper star")
        # Optional ei collapse
        self.assertEqual(
            collapse_japanese_long_vowels("せいしゅん", include_ei=False),
            "せいしゅん",
        )
        self.assertEqual(
            collapse_japanese_long_vowels("せいしゅん", include_ei=True),
            "せしゅん",
        )
        # Standalone を symmetry in o-column long vowel collapsing
        self.assertEqual(collapse_japanese_long_vowels("をうおを"), "を")

    def test_weighted_indel_calibration_and_dakuten_tolerance(self) -> None:
        pairs = [
            ("やさしいうそをついてくれよ", "やさしいうそをついてくれよ"),
            ("げんじつわさんこくだもの", "げんじつはざんこくだもの"),
            ("しろくさをぬけとっとがらんどいく", "とうとうがらんどう"),
            ("いきもわすれてまたたきさえを", "またたきさえおっくう"),
        ]
        for a, b in pairs:
            self.assertAlmostEqual(
                weighted_indel_ratio(a, b, dakuten_cost=1.0),
                fuzz.ratio(a, b) / 100.0,
                places=6,
            )
            w_sc, _, _, _, _ = weighted_partial_ratio_alignment(
                a, b, dakuten_cost=1.0
            )
            rf_al = fuzz.partial_ratio_alignment(a, b)
            assert rf_al is not None
            self.assertAlmostEqual(w_sc, rf_al.score / 100.0, places=6)

        # Voiced/unvoiced pairs score significantly higher than unweighted ratio (0.30)
        # and unrelated substitutions, while exact match stays highest at 1.0.
        exact = weighted_indel_ratio("かぜがかたくはばたく", "かぜがかたくはばたく", dakuten_cost=0.35)
        dakuten_var = weighted_indel_ratio("かせかかたぐぱはだぐ", "かぜがかたくはばたく", dakuten_cost=0.35)
        unweighted = fuzz.ratio("かせかかたぐぱはだぐ", "かぜがかたくはばたく") / 100.0
        unrelated = weighted_indel_ratio("ませまなだぐらまらぐ", "かぜがかたくはばたく", dakuten_cost=0.35)
        self.assertEqual(exact, 1.0)
        self.assertGreater(dakuten_var, 0.70)
        self.assertGreater(dakuten_var, unweighted + 0.30)
        self.assertGreater(dakuten_var, unrelated)

        det = score_similarity_weighted_detailed("かせかかたぐ", "かぜがかたく", dakuten_cost=0.35)
        self.assertGreater(det["final_score"], 0.80)


class TestAdjacentPairMatching(unittest.TestCase):
    def test_cross_line_pair_advances_sequentially_and_blocks_single_line_jump(self) -> None:
        lyrics = [
            _line(0, "いきもわすれて"),
            _line(1, "またたきさえおっくう"),
            _line(2, "きょうさえあしたかこにかわる"),
        ]

        # Pure single-line fails when ASR only captures half of L0 + half of L1.
        clock_single = _FakeClock(0.0)
        eng_single = LyricSyncEngine(
            clock=clock_single,
            emit_events=False,
            alignment_mode="line",
            phonological_norm=False,
            adjacent_pair=False,
        )
        eng_single.reset_with_lyrics(lyrics)
        clock_single.now = 2.0
        self.assertIsNone(eng_single.process_recognized_text("わすれてまたたき"))

        # Adjacent pair matches L0+L1! Because L1 entry < 0.70, it emits L0 (display=L1)
        # and keeps L1 prefix "またたき" so the rest of L1 matches +1 sequentially.
        clock_pair = _FakeClock(0.0)
        eng_pair = LyricSyncEngine(
            clock=clock_pair,
            emit_events=False,
            alignment_mode="line",
            phonological_norm=False,
            adjacent_pair=True,
            retain_tail=True,
        )
        eng_pair.reset_with_lyrics(lyrics)
        clock_pair.now = 2.0
        m0 = eng_pair.process_recognized_text("わすれてまたたき")
        self.assertIsNotNone(m0)
        assert m0 is not None
        self.assertEqual(m0["matched_index"], 0)
        self.assertEqual(m0["display_index"], 1)

        clock_pair.now = 4.2
        m1 = eng_pair.process_recognized_text("きさえおっくう")
        self.assertIsNotNone(m1)
        assert m1 is not None
        self.assertEqual(m1["matched_index"], 1)

        # Single-line-only buffer ("いきもわすれて") must NOT falsely jump via pair to L1.
        clock_guard = _FakeClock(0.0)
        eng_guard = LyricSyncEngine(
            clock=clock_guard,
            emit_events=False,
            alignment_mode="line",
            phonological_norm=False,
            adjacent_pair=True,
        )
        eng_guard.reset_with_lyrics(lyrics)
        clock_guard.now = 2.0
        m_only0 = eng_guard.process_recognized_text("いきもわすれて")
        self.assertIsNotNone(m_only0)
        assert m_only0 is not None
        self.assertEqual(m_only0["matched_index"], 0)

    def test_bridge_pair_matches_prev_tail_plus_next_prefix(self) -> None:
        lyrics = [
            _line(0, "とおくのそらをながめながら"),
            _line(1, "あしたをまってゆめをみた"),
        ]
        clock = _FakeClock(0.0)
        eng = LyricSyncEngine(
            clock=clock,
            emit_events=False,
            alignment_mode="line",
            phonological_norm=False,
            adjacent_pair=True,
            fuzzy_next_line_threshold=0.75,
        )
        eng.reset_with_lyrics(lyrics)
        clock.now = 2.0
        self.assertEqual(eng.process_recognized_text("とおくのそらをながめ")["matched_index"], 0)

        # ASR carries tail of L0 + early prefix of L1 (only 5 chars of L1, < 0.75 alone).
        clock.now = 4.2
        m_bridge = eng.process_recognized_text("めながらあしたを")
        self.assertIsNotNone(m_bridge)
        assert m_bridge is not None
        self.assertEqual(m_bridge["matched_index"], 1)


class TestContinuousBeamAlignment(unittest.TestCase):
    def test_default_engine_uses_beam_and_phono_and_handles_cross_line_variations(self) -> None:
        eng = LyricSyncEngine(emit_events=False)
        cfg = eng.get_matcher_config()
        self.assertEqual(cfg["alignment_mode"], "beam")
        self.assertTrue(cfg["phonological_norm"])

        lyrics = [
            _line(0, "とうとうがらんどう"),
            _line(1, "行く風下、私だけ"),
            _line(2, "茶化してくれハイウェイ・スター"),
            _line(3, "瞬きさえ億劫"),
        ]
        clock = _FakeClock(0.0)
        eng = LyricSyncEngine(clock=clock, emit_events=False)
        eng.reset_with_lyrics(lyrics)

        # L0+L1 cross-line + long-vowel/sokuon/particle variations ("とっとがらんどいくわたしだけ")
        clock.now = 2.0
        m0 = eng.process_recognized_text("とっとがらんどいく")
        self.assertIsNotNone(m0)
        assert m0 is not None
        self.assertEqual(m0["matched_index"], 0)
        self.assertEqual(m0["target_normalized"], "とうとうがらんどう")  # canonical preserved!

        clock.now = 4.0
        m1 = eng.process_recognized_text("ゆくかざしもわたしだけ")
        self.assertIsNotNone(m1)
        assert m1 is not None
        self.assertEqual(m1["matched_index"], 1)

        # Middle-dot + dakuten variation ("ちゃかしてくれハイウェイスだ")
        clock.now = 6.2
        m2 = eng.process_recognized_text("ちゃかしてくれハイウェイスだ")
        self.assertIsNotNone(m2)
        assert m2 is not None
        self.assertEqual(m2["matched_index"], 2)

        # Long vowel + wo/o particle ("瞬きさえを")
        clock.now = 8.4
        m3 = eng.process_recognized_text("瞬きさえを")
        self.assertIsNotNone(m3)
        assert m3 is not None
        self.assertEqual(m3["matched_index"], 3)

    def test_beam_freshness_guard_blocks_noise_and_boundary_spill_clamp(self) -> None:
        lyrics = [
            _line(0, "風が吹いた正午"),
            _line(1, "これからどうなるんだろうね"),
            _line(2, "進め方教わらないんだよ"),
        ]
        clock = _FakeClock(0.0)
        eng = LyricSyncEngine(clock=clock, emit_events=False)
        eng.reset_with_lyrics(lyrics)

        # Match L0 cleanly, leaving 1 char spill of L1 ("こ") -> must NOT prematurely claim L1!
        clock.now = 2.0
        m0 = eng.process_recognized_text("かぜがふいたしょうごこ")
        self.assertIsNotNone(m0)
        assert m0 is not None
        self.assertEqual(m0["matched_index"], 0)
        self.assertEqual(eng.get_current_index(), 0)

        # Pure Whisper hallucinations must NEVER match even with non-empty rolling buffer.
        for idx, noise in enumerate(("ごめん", "me", "うん", "バラバラバラバラ")):
            clock.now = 4.5 + idx * 2.2
            self.assertIsNone(eng.process_recognized_text(noise))
        self.assertEqual(eng.get_current_index(), 0)

    def test_beam_short_line_guard_repeats_chorus_safety_and_recovery(self) -> None:
        # 1. Short-line guard (2-char & 4-char)
        clock = _FakeClock(0.0)
        eng = LyricSyncEngine(clock=clock, emit_events=False)
        eng.reset_with_lyrics(
            [
                _line(0, "ほしぞらをみあげてる"),
                _line(1, "あか"),
                _line(2, "かわくよ"),
            ]
        )
        eng.apply_manual_jump(0)
        clock.now = 2.0
        self.assertIsNone(eng.process_recognized_text("きょうもあかるいそら"))
        m1 = eng.process_recognized_text("あか")
        self.assertIsNotNone(m1)
        assert m1 is not None
        self.assertEqual(m1["matched_index"], 1)

        clock.now = 4.2
        self.assertIsNone(eng.process_recognized_text("かわるよ"))
        clock.now = 6.4
        eng.apply_manual_jump(1)
        m2 = eng.process_recognized_text("かわくよ")
        self.assertIsNotNone(m2)
        assert m2 is not None
        self.assertEqual(m2["matched_index"], 2)

        # 2. Consecutive repeats, echo suppression, & repeated-chorus non-jump safety
        chorus = [
            _line(0, "間違ってないよな"),
            _line(1, "間違ってないよな"),
            _line(2, "だから僕は音楽を辞めた"),
            _line(3, "だから僕は"),
            _line(4, "知らない道へ歩いていく"),
            _line(5, "間違ってないよな"),
            _line(6, "間違ってないよな"),
            _line(7, "忘れられない言葉を抱いて"),
        ]
        clock2 = _FakeClock(0.0)
        eng2 = LyricSyncEngine(clock=clock2, emit_events=False, duplicate_ttl=4.0)
        eng2.reset_with_lyrics(chorus)

        clock2.now = 2.0
        self.assertEqual(eng2.process_recognized_text("間違ってないよな")["matched_index"], 0)
        clock2.now = 4.2
        self.assertEqual(eng2.process_recognized_text("間違ってないよな")["matched_index"], 1)
        clock2.now = 6.5
        self.assertEqual(eng2.process_recognized_text("だから僕は音楽を辞めた")["matched_index"], 2)
        clock2.now = 8.7
        self.assertEqual(eng2.process_recognized_text("だから僕は")["matched_index"], 3)
        # Repeating exact ASR echo quickly after non-repeat is suppressed
        clock2.now = 10.0
        _, echo_tr = eng2.evaluate_recognized_text("だから僕は")
        self.assertEqual(echo_tr["decision"], "skipped_duplicate_ttl")

        # Repeated chorus safety: jumping to L4 and hearing L5 must stay at L5, not skip to L6.
        eng2.apply_manual_jump(4)
        clock2.now = 15.0
        m5 = eng2.process_recognized_text("間違ってないよな")
        self.assertIsNotNone(m5)
        assert m5 is not None
        self.assertEqual(m5["matched_index"], 5)

        # Phase 2 recovery in beam mode
        eng2.apply_manual_jump(0)
        clock2.now = 20.0
        eng2.process_recognized_text("あ")
        clock2.now = 30.0
        m_rec, tr_rec = eng2.evaluate_recognized_text("忘れられない言葉を抱いて")
        self.assertIsNotNone(m_rec)
        assert m_rec is not None
        self.assertEqual(m_rec["matched_index"], 7)
        self.assertEqual(tr_rec["decision"], "matched_phase2_recovery")

    def test_beam_adaptive_display_lazy_parity_and_eval_presets(self) -> None:
        lyrics = [
            _line(0, "なつのこかげにすわったままひょうかをくちにほうりこんでかぜをまっていた"),
            _line(1, "もうわすれてしまったかなよのなかのぜんぶうそだらけ"),
            _line(2, "ほんとうのかちをふたりでさがしにいこうとわらったこと"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            enabled = False
            clock = _FakeClock(0.0)
            recorder = MatchTraceRecorder(
                traces_dir=Path(tmpdir),
                is_enabled_fn=lambda: enabled,
                clock=clock,
            )
            eng = LyricSyncEngine(
                trace_recorder=recorder,
                clock=clock,
                emit_events=False,
                display_mode="adaptive",
            )
            try:
                eng.reset_with_lyrics(lyrics, track_meta={"song_name": "BeamAnchor"})

                # Pre-trace match + buffer before lazy open
                clock.now = 2.0
                m0 = eng.process_recognized_text("夏の木陰に座ったまま")
                self.assertIsNotNone(m0)
                assert m0 is not None
                self.assertTrue(m0["early_display_hold"])
                self.assertEqual(m0["display_index"], 0)

                clock.now = 2.5
                eng.apply_manual_jump(0)

                clock.now = 4.0
                self.assertIsNone(eng.process_recognized_text("もうわ"))

                # Enable trace mid-song -> lazy open captures beam & short_buf state!
                enabled = True
                clock.now = 6.5
                m1 = eng.process_recognized_text("すれてしまったかな世の中の全部嘘だらけ")
                self.assertIsNotNone(m1)
                assert m1 is not None
                self.assertEqual(m1["matched_index"], 1)
                self.assertEqual(m1["display_index"], 2)

                clock.now = 7.0
                eng.apply_manual_jump(2)

                trace_path = recorder.current_trace_path
            finally:
                eng.close_trace_session("done", clear_pending=True)

            assert trace_path is not None
            loaded = load_trace(trace_path)
            run_apply = eval_matching.replay_trace(
                loaded,
                eval_matching.parse_strategy_spec("baseline"),
                anchor_mode="apply",
            )
            self.assertEqual(run_apply["summary"]["live_parity_rate"], 1.0)

            run_beam = eval_matching.replay_trace(
                loaded,
                eval_matching.parse_strategy_spec("beam_align"),
                anchor_mode="eval",
            )
            run_beam_next = eval_matching.replay_trace(
                loaded,
                eval_matching.parse_strategy_spec("beam_align:display_mode=next"),
                anchor_mode="eval",
            )
            self.assertEqual(run_beam["summary"]["anchor_mae"], 0.0)
            self.assertEqual(run_beam_next["summary"]["anchor_mae"], 0.0)

        for preset in (
            "phono_sync",
            "phono_weighted",
            "pair_sync",
            "phono_pair_sync",
            "beam_align",
        ):
            spec = eval_matching.parse_strategy_spec(preset)
            self.assertEqual(spec.name, preset)

        spec_custom = eval_matching.parse_strategy_spec(
            "beam_align:align_mode=beam,phono=true,beam_width=5,beam_min_sim=0.75,"
            "beam_line_pen=1.5,beam_max_jump_pen=7.0,beam_rec_pen=5.5,"
            "beam_decay=0.8,beam_back_slack=12,beam_buf_chars=40,beam_stale_sec=5.0"
        )
        self.assertEqual(spec_custom.params["alignment_mode"], "beam")
        self.assertTrue(spec_custom.params["phonological_norm"])
        self.assertEqual(spec_custom.params["beam_width"], 5)
        self.assertEqual(spec_custom.params["beam_min_sim"], 0.75)
        self.assertEqual(spec_custom.params["beam_jump_penalty_per_line"], 1.5)
        self.assertEqual(spec_custom.params["beam_max_jump_penalty"], 7.0)
        self.assertEqual(spec_custom.params["beam_recovery_penalty"], 5.5)
        self.assertEqual(spec_custom.params["beam_decay"], 0.8)
        self.assertEqual(spec_custom.params["beam_back_slack"], 12)
        self.assertEqual(spec_custom.params["beam_max_buffer_chars"], 40)
        self.assertEqual(spec_custom.params["beam_buffer_stale_seconds"], 5.0)

    def test_beam_partial_next_line_clamp_rewinds_beam_and_advances_sequentially(
        self,
    ) -> None:
        lyrics = [
            _line(0, "あいうえおかきくけこ"),
            _line(1, "さしすせそたちつてとなにぬねの"),
            _line(2, "まみむめもやゆよらりるれろ"),
        ]
        clock = _FakeClock(0.0)
        eng = LyricSyncEngine(clock=clock, emit_events=False)
        eng.reset_with_lyrics(lyrics)

        # Match L0 + 4 chars of L1 (4/15 = 0.267 < 0.35): must clamp to L0,
        # retain the 4-char spill, and rewind _beam[0] back to L0 end (pos=10, line=0).
        clock.now = 2.0
        m0, t0 = eng.evaluate_recognized_text("あいうえおかきくけこさしすせ")
        self.assertIsNotNone(m0)
        assert m0 is not None
        self.assertEqual(m0["matched_index"], 0)
        self.assertEqual(t0["retained_tail"], "さしすせ")
        self.assertEqual(eng._beam[0].pos, len("あいうえおかきくけこ"))
        self.assertEqual(eng._beam[0].last_line, 0)

        # Next ASR step brings only 2 more chars of L1 ("そた"): combined with retained spill
        # ("さしすせそた"), beam cleanly advances +1 to L1.
        clock.now = 4.0
        m1 = eng.process_recognized_text("そた")
        self.assertIsNotNone(m1)
        assert m1 is not None
        self.assertEqual(m1["matched_index"], 1)


class TestAsrSegmentFilterAndInitialPrompt(unittest.TestCase):
    def test_repetitive_loop_and_segment_confidence_hallucination_filter(
        self,
    ) -> None:
        self.assertTrue(has_repetitive_token_loop("バラバラバラバラ"))
        self.assertTrue(has_repetitive_token_loop("あいうあいうあいう"))
        self.assertFalse(has_repetitive_token_loop("とうとうがらんどう"))
        self.assertFalse(has_repetitive_token_loop("ここは"))

        corpus_without_fillers = "おわりのはじまり\nremember the night\nやさしいうそ"
        segments = [
            {"text": "ご視聴ありがとうございました", "avg_logprob": -0.20, "no_speech_prob": 0.05, "compression_ratio": 0.9},
            {"text": "ごめん", "avg_logprob": -0.45, "no_speech_prob": 0.08, "compression_ratio": 0.53},
            {"text": "me", "avg_logprob": -0.50, "no_speech_prob": 0.08, "compression_ratio": 0.20},
            {"text": "お", "avg_logprob": -0.42, "no_speech_prob": 0.06, "compression_ratio": 0.27},
            {"text": "おー", "avg_logprob": -0.60, "no_speech_prob": 0.09, "compression_ratio": 0.43},
            {"text": "バラバラバラバラ", "avg_logprob": -0.35, "no_speech_prob": 0.08, "compression_ratio": 1.41},
            {"text": "おうけ", "avg_logprob": -0.92, "no_speech_prob": 0.05, "compression_ratio": 0.53},
            {"text": "ねむい", "avg_logprob": -0.30, "no_speech_prob": 0.72, "compression_ratio": 0.60},
            {"text": "やさしい", "avg_logprob": -0.85, "no_speech_prob": 0.04, "compression_ratio": 0.72},
            {"text": "うそ", "avg_logprob": -0.22, "no_speech_prob": 0.04, "compression_ratio": 0.75},
        ]
        res = filter_asr_segments(segments, lyrics_corpus=corpus_without_fillers)
        self.assertEqual(res["text"], "やさしいうそ")
        reasons = {
            f.get("text"): f.get("reason") for f in res["filtered_segments"]
        }
        self.assertEqual(reasons.get("ごめん"), "interlude_filler")
        self.assertEqual(reasons.get("me"), "interlude_filler")
        self.assertEqual(reasons.get("お"), "interlude_filler")
        self.assertEqual(reasons.get("おー"), "interlude_filler")
        self.assertEqual(reasons.get("バラバラバラバラ"), "high_compression_ratio")
        self.assertEqual(reasons.get("おうけ"), "low_avg_logprob")
        self.assertEqual(reasons.get("ねむい"), "high_no_speech_prob")
        self.assertIn("ご視聴ありがとうございました", res["hallucinations_removed"])

        # Same segment text with lower compression_ratio triggers repetitive_loop.
        loop_res = filter_asr_segments(
            [{"text": "バラバラバラバラ", "avg_logprob": -0.30, "no_speech_prob": 0.05, "compression_ratio": 1.1}],
            lyrics_corpus=corpus_without_fillers,
        )
        self.assertEqual(loop_res["text"], "")
        self.assertEqual(loop_res["filtered_segments"][0]["reason"], "repetitive_loop")

        # Valid lyric occurrences of "ごめん" / "me" are preserved when present in lyrics_corpus!
        corpus_with_fillers = "ごめんねと言えなくて\nkiss me now"
        kept_res = filter_asr_segments(
            [
                {"text": "ごめん", "avg_logprob": -0.30, "no_speech_prob": 0.05, "compression_ratio": 0.53},
                {"text": "me", "avg_logprob": -0.30, "no_speech_prob": 0.05, "compression_ratio": 0.40},
            ],
            lyrics_corpus=corpus_with_fillers,
        )
        self.assertEqual(kept_res["text"], "ごめんme")
        self.assertEqual(kept_res["filtered_segments"], [])

        # Repetitive kana/ASCII choruses present in lyrics_corpus (even across kana scripts) are preserved.
        chorus_corpus = "らららら\noh la la la tonight"
        chorus_res = filter_asr_segments(
            [
                {"text": "ララララ", "avg_logprob": -0.25, "no_speech_prob": 0.04, "compression_ratio": 1.15},
                {"text": "la la la", "avg_logprob": -0.25, "no_speech_prob": 0.04, "compression_ratio": 1.10},
            ],
            lyrics_corpus=chorus_corpus,
        )
        self.assertEqual(chorus_res["text"], "ララララla la la")
        self.assertEqual(chorus_res["filtered_segments"], [])

    def test_upcoming_prompt_and_asr_context_follow_progress(self) -> None:
        eng = LyricSyncEngine(emit_events=False)
        self.assertEqual(eng.get_upcoming_prompt(), "")

        lyrics = [
            _line(0, "雲を抜け風下へ私だけ"),
            _line(1, "だから僕は音楽を辞めた"),
            _line(2, "花に亡霊の夏が来る"),
            _line(3, "春泥棒が風を運ぶ"),
            _line(4, "最後の行のメロディ"),
        ]
        eng.reset_with_lyrics(
            lyrics,
            track_meta={"artist_name": "ヨルシカ", "song_name": "テスト曲"},
        )

        prompt_start = eng.get_upcoming_prompt(max_lines=3, max_chars=120)
        self.assertEqual(
            prompt_start,
            "雲を抜け風下へ私だけ、だから僕は音楽を辞めた、花に亡霊の夏が来る",
        )
        # Respects max_chars truncation at line boundary.
        self.assertEqual(
            eng.get_upcoming_prompt(max_lines=4, max_chars=22),
            "雲を抜け風下へ私だけ、だから僕は音楽を辞めた",
        )

        eng.apply_manual_jump(2)
        ctx = eng.get_asr_context(max_lines=4, max_chars=120)
        self.assertEqual(
            ctx["initial_prompt"],
            "春泥棒が風を運ぶ、最後の行のメロディ",
        )
        self.assertEqual(ctx["track_slug"], "ヨルシカ_テスト曲")
        self.assertEqual(ctx["current_index"], 2)
        self.assertIn("くもをぬけ", ctx["lyrics_corpus"])

        eng.apply_manual_jump(4)
        self.assertEqual(eng.get_upcoming_prompt(), "")

    def test_engine_asr_segment_filter_and_eval_beam_asr_opt_strategy(
        self,
    ) -> None:
        lyrics = [
            _line(0, "雲を抜け風下へ私だけ"),
            _line(1, "だから僕は音楽を辞めた"),
        ]
        trace_data = {
            "path": "synthetic_asr_opt.jsonl",
            "session_start": {
                "type": "session_start",
                "schema_version": 1,
                "track": {"song_name": "OptTest", "artist_name": "Tester"},
                "asr_config": {"block_seconds": 4.0, "step_seconds": 2.0},
                "matcher_config": {},
                "initial_state": {"lazy_opened": False, "current_index": -1},
                "lyrics": lyrics,
            },
            "events": [
                {
                    "type": "asr_step",
                    "seq": 1,
                    "elapsed_sec": 2.0,
                    "asr": {
                        "text": "ごめん",
                        "segments": [
                            {"text": "ごめん", "avg_logprob": -0.48, "no_speech_prob": 0.08, "compression_ratio": 0.53}
                        ],
                    },
                    "playback": {},
                    "decision": "no_match",
                    "matched_index": None,
                },
                {
                    "type": "asr_step",
                    "seq": 2,
                    "elapsed_sec": 4.0,
                    "asr": {
                        "text": "雲を抜け風下へ私だけ",
                        "segments": [
                            {"text": "雲を抜け風下へ私だけ", "avg_logprob": -0.18, "no_speech_prob": 0.03, "compression_ratio": 0.80}
                        ],
                    },
                    "playback": {},
                    "decision": "no_match",
                    "matched_index": None,
                },
            ],
        }

        spec = eval_matching.parse_strategy_spec("beam_asr_opt")
        self.assertEqual(spec.name, "beam_asr_opt")
        custom_spec = eval_matching.parse_strategy_spec(
            "beam_align:asr_filter=true,min_logprob=-0.75,max_no_speech=0.5,max_comp_ratio=1.3"
        )
        self.assertTrue(custom_spec.params["asr_segment_filter"])
        self.assertEqual(custom_spec.params["asr_min_avg_logprob"], -0.75)
        self.assertEqual(custom_spec.params["asr_max_no_speech_prob"], 0.5)
        self.assertEqual(custom_spec.params["asr_max_compression_ratio"], 1.3)

        run_plain = eval_matching.replay_trace(
            trace_data, eval_matching.parse_strategy_spec("beam_align"), anchor_mode="eval"
        )
        run_opt = eval_matching.replay_trace(trace_data, spec, anchor_mode="eval")

        # Without filter, 'ごめん' pollutes buffer on step 1; with beam_asr_opt, step 1 is filtered cleanly.
        self.assertEqual(run_plain["timeline"][0]["buffer_after_trim"], "ごめん")
        self.assertEqual(run_opt["timeline"][0]["decision"], "asr_empty_or_filtered")
        self.assertEqual(run_opt["timeline"][0]["buffer_after_trim"], "")
        self.assertEqual(run_opt["timeline"][1]["matched_index"], 0)

    def test_post_pr11_trace_refilters_and_reconstructs_for_ab_eval(
        self,
    ) -> None:
        lyrics = [
            _line(0, "雲を抜け風下へ私だけ"),
            _line(1, "だから僕は音楽を辞めた"),
        ]
        # Verify session_start asr_config records decode/preprocess settings (including distilled model initial_prompt auto-disable).
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_model = eval_matching.config.ASR_MODEL_SIZE
            self.addCleanup(setattr, eval_matching.config, "ASR_MODEL_SIZE", orig_model)

            eval_matching.config.ASR_MODEL_SIZE = "turbo"
            rec = MatchTraceRecorder(
                traces_dir=Path(tmpdir), is_enabled_fn=lambda: True
            )
            eng = LyricSyncEngine(trace_recorder=rec, emit_events=False)
            eng.reset_with_lyrics(lyrics, track_meta={"song_name": "CfgTest"})
            tpath = rec.current_trace_path
            eng.close_trace_session("done")
            assert tpath is not None
            loaded_cfg = load_trace(tpath)["session_start"]["asr_config"]
            self.assertIn("beam_size", loaded_cfg)
            self.assertIn("condition_on_previous_text", loaded_cfg)
            self.assertIn("initial_prompt_enabled", loaded_cfg)
            self.assertTrue(loaded_cfg["initial_prompt_enabled"])
            self.assertIn("vocal_bandpass_enabled", loaded_cfg)

            eval_matching.config.ASR_MODEL_SIZE = (
                "kotoba-tech/kotoba-whisper-v2.0-faster"
            )
            eng.reset_with_lyrics(
                lyrics, track_meta={"song_name": "CfgDistilledTest"}
            )
            tpath_distil = rec.current_trace_path
            eng.close_trace_session("done")
            assert tpath_distil is not None
            loaded_distil_cfg = load_trace(tpath_distil)["session_start"][
                "asr_config"
            ]
            self.assertFalse(loaded_distil_cfg["initial_prompt_enabled"])

        post_pr11_trace = {
            "path": "synthetic_post_pr11.jsonl",
            "session_start": {
                "type": "session_start",
                "schema_version": 1,
                "track": {"song_name": "PostPR11", "artist_name": "Tester"},
                "asr_config": {"block_seconds": 4.0, "step_seconds": 2.0},
                "matcher_config": {"alignment_mode": "beam", "phonological_norm": True},
                "initial_state": {"lazy_opened": False, "current_index": -1},
                "lyrics": lyrics,
            },
            "events": [
                {
                    "type": "asr_step",
                    "seq": 1,
                    "elapsed_sec": 2.0,
                    "asr": {
                        "text": "",
                        "raw_text_before_filter": "ごめん",
                        "segment_filtered": True,
                        "segments": [
                            {"text": "ごめん", "avg_logprob": -0.45, "no_speech_prob": 0.08, "compression_ratio": 0.53}
                        ],
                    },
                    "playback": {},
                    "decision": "asr_empty_or_filtered",
                    "matched_index": None,
                },
                {
                    "type": "asr_step",
                    "seq": 2,
                    "elapsed_sec": 4.0,
                    "asr": {
                        "text": "雲を抜け風下へ私だけ",
                        "raw_text_before_filter": "雲を抜け風下へ私だけ",
                        "segment_filtered": True,
                        "segments": [
                            {"text": "雲を抜け風下へ私だけ", "avg_logprob": -0.65, "no_speech_prob": 0.04, "compression_ratio": 0.80}
                        ],
                    },
                    "playback": {},
                    "decision": "matched_phase1",
                    "matched_index": 0,
                },
            ],
        }

        run_base = eval_matching.replay_trace(
            post_pr11_trace, eval_matching.parse_strategy_spec("baseline"), anchor_mode="eval"
        )
        run_unfiltered = eval_matching.replay_trace(
            post_pr11_trace, eval_matching.parse_strategy_spec("beam_align"), anchor_mode="eval"
        )
        run_strict = eval_matching.replay_trace(
            post_pr11_trace,
            eval_matching.parse_strategy_spec("beam_asr_opt:min_logprob=-0.50"),
            anchor_mode="eval",
        )

        self.assertEqual(run_base["summary"]["live_parity_rate"], 1.0)
        self.assertEqual(run_unfiltered["timeline"][0]["buffer_after_trim"], "ごめん")
        self.assertEqual(run_strict["timeline"][0]["decision"], "asr_empty_or_filtered")
        self.assertEqual(run_strict["timeline"][1]["decision"], "asr_empty_or_filtered")
        self.assertEqual(run_strict["summary"]["total_matches"], 0)


class TestSpotifyPositionPriorAndSeekDetection(unittest.TestCase):
    @staticmethod
    def _build_multi_chorus_lyrics() -> list[dict]:
        texts = [
            "つめたいよるのしずくがまどをたたいた",       # 0: Verse 1
            "あおいライトがゆれてとけいをみた",         # 1
            "しずかなまちでひとりあるきだした",         # 2
            "しろいページにゆめをかいていた",           # 3
            "とおいかねのねいろがきこえてくる",         # 4
            "きみとすごしたあのひのことをおもう",       # 5
            "ひかりのかけらをてのひらにあつめて",       # 6: Chorus 1
            "きえないおもいをそらにとばした",           # 7
            "はるかなみらいへこえをひびかせて",         # 8
            "みしらぬえきでとまったでんしゃ",           # 9: Verse 2
            "かばんのなかにはふるいしゃしん",           # 10
            "ゆめのつづきはまだおわらない",             # 11
            "ごごのかふぇでほんをひらいて",             # 12
            "あめあがりのみちにみずたまり",             # 13
            "ゆうぐれのこうえんでたちどまる",           # 14
            "よるがふけてほしがみえてきた",             # 15: Bridge
            "ながいよるをこえてあさがくるまで",         # 16
            "なみだをふいてつよくまえをむいて",         # 17
            "ひかりのかけらをてのひらにあつめてよ",     # 18: Final Chorus (extra particle よ)
            "きえないおもいをそらにとばしたよ",         # 19
            "いつかまたあえるそのひまで",               # 20: Outro
        ]
        return [_line(i, t) for i, t in enumerate(texts)]

    @staticmethod
    def _build_long_multi_chorus_lyrics() -> list[dict]:
        texts = [
            "あさやけのほどうをあるきだした",           # 0: Verse 1
            "しろいいきがそらにきえてゆく",             # 1
            "とおいでんしゃのおとがきこえた",           # 2
            "きみのなまえをなんどもよんだ",             # 3: Chorus 1
            "わすられないひびをおいかけて",             # 4
            "かぜがやさしくほおをなでた",               # 5
            "ごごのカフェでほんをひらいて",             # 6: Verse 2
            "とおいむかしのことをおもいだす",           # 7
            "あめあがりのみちにみずたまり",             # 8
            "ゆうぐれのこうえんでたちどまる",           # 9
            "きみのなまえをなんどもよんだ",             # 10: Chorus 2
            "わすられないひびをおいかけて",             # 11
            "かぜがやさしくほおをなでた",               # 12
            "よるがふけてほしがみえてきた",             # 13: Bridge
            "もういちどだけあいたいとねがう",           # 14
            "とまったとけいがうごきだす",               # 15
            "ちいさなてがみをポケットにいれて",         # 16
            "あしたのひかりをしんじてあるく",           # 17
            "さよならのかわりにえがおをみせて",         # 18
            "とおいほしぞらにてをのばした",             # 19
            "ながれるくもをじっとみつめてた",           # 20
            "きみのなまえをなんどもよんだ",             # 21: Final Chorus
            "わすられないひびをおいかけて",             # 22
            "かぜがやさしくほおをなでた",               # 23
            "いつかまたあえるそのひまで",               # 24: Outro
        ]
        return [_line(i, t) for i, t in enumerate(texts)]

    def test_timeline_character_ratio_helpers(self) -> None:
        empty = ContinuousLyricsTimeline([])
        self.assertEqual(empty.total_chars, 0)
        self.assertEqual(empty.char_ratio(0), 0.0)
        self.assertEqual(empty.line_center_ratio(0), 0.0)
        self.assertEqual(empty.line_for_ratio(0.5), 0)

        tl = ContinuousLyricsTimeline(["aaaa", "bbbbbbbb", "cccc"])
        self.assertEqual(tl.total_chars, 16)
        self.assertAlmostEqual(tl.char_ratio(0), 0.0)
        self.assertAlmostEqual(tl.char_ratio(-5), 0.0)
        self.assertAlmostEqual(tl.char_ratio(8), 0.5)
        self.assertAlmostEqual(tl.char_ratio(99), 1.0)
        self.assertAlmostEqual(tl.line_center_ratio(0), 2.0 / 16.0)
        self.assertAlmostEqual(tl.line_center_ratio(1), 8.0 / 16.0)
        self.assertAlmostEqual(tl.line_center_ratio(2), 14.0 / 16.0)
        self.assertEqual(tl.line_center_ratio(-1), 0.0)
        self.assertEqual(tl.line_center_ratio(99), 0.0)
        self.assertEqual(tl.line_for_ratio(0.0), 0)
        self.assertEqual(tl.line_for_ratio(0.5), 1)
        self.assertEqual(tl.line_for_ratio(0.95), 2)
        self.assertEqual(tl.line_for_ratio(1.5), 2)

        # _current_playback_ratio_locked rejects bools, NaN, inf, and non-positive durations
        ratio_fn = LyricSyncEngine._current_playback_ratio_locked
        self.assertIsNone(ratio_fn({"spotify_progress_ms": True, "duration_ms": 1000}))
        self.assertIsNone(ratio_fn({"spotify_progress_ms": 500, "duration_ms": True}))
        self.assertIsNone(ratio_fn({"spotify_progress_ms": float("nan"), "duration_ms": 1000}))
        self.assertIsNone(ratio_fn({"spotify_progress_ms": 500, "duration_ms": float("inf")}))
        self.assertIsNone(ratio_fn({"spotify_progress_ms": 500, "duration_ms": 0}))
        self.assertIsNone(ratio_fn({"spotify_progress_ms": 500, "duration_ms": -100}))
        self.assertAlmostEqual(
            ratio_fn({"estimated_progress_ms": 60000, "duration_ms": 240000}),
            0.25,
        )

    def test_soft_position_prior_prevents_false_chorus_jump_in_beam_and_line_modes(
        self,
    ) -> None:
        lyrics = self._build_multi_chorus_lyrics()
        # Spoken/sung Chorus 1 line with slight trailing particle noise ('よ') that would
        # score slightly higher on Final Chorus (L18) without a position prior.
        chorus_asr_with_noise = "ひかりのかけらをてのひらにあつめてよ"

        for align_mode in ("beam", "line"):
            # 1. Early/mid playback (~31% into 240s track): position_prior keeps match at Chorus 1 (L6)
            #    while position_prior_enabled=False falsely jumps to Final Chorus (L18).
            for prior_enabled, expected_idx in ((False, 18), (True, 6)):
                clock = _FakeClock(0.0)
                eng = LyricSyncEngine(
                    clock=clock,
                    emit_events=False,
                    alignment_mode=align_mode,
                    phonological_norm=True,
                    fuzzy_lookahead_lines=4,
                    recovery_lookahead_lines=20,
                    position_prior_enabled=prior_enabled,
                    position_prior_tolerance=0.15,
                    position_prior_weight=0.45,
                    position_prior_floor=0.60,
                    seek_detection_enabled=True,
                )
                eng.reset_with_lyrics(lyrics, track_meta={"song_id": "sp:chorus"})
                clock.now = 5.0
                self.assertIsNone(
                    eng.update_playback_progress(
                        15000, 240000, song_id="sp:chorus", is_playing=True
                    )
                )
                m0, _ = eng.evaluate_recognized_text("つめたいよるのしずくがまどをたたいた")
                self.assertIsNotNone(m0)
                assert m0 is not None
                self.assertEqual(m0["matched_index"], 0)

                # Advance 60s monotonically (no seek jump) -> Phase 2 opens across L6..L18
                clock.now = 65.0
                self.assertIsNone(
                    eng.update_playback_progress(
                        75000, 240000, song_id="sp:chorus", is_playing=True
                    )
                )
                match_payload, step_trace = eng.evaluate_recognized_text(
                    chorus_asr_with_noise
                )
                self.assertIsNotNone(match_payload)
                assert match_payload is not None
                self.assertEqual(
                    match_payload["matched_index"],
                    expected_idx,
                    f"mode={align_mode}, prior={prior_enabled}",
                )
                if prior_enabled:
                    by_idx = {
                        c["index"]: c for c in step_trace.get("candidates", [])
                    }
                    self.assertIn(6, by_idx)
                    self.assertIn(18, by_idx)
                    self.assertGreater(
                        by_idx[6]["position_prior"], by_idx[18]["position_prior"]
                    )

            # 2. Late playback (~87.5% into 240s track) near Bridge -> matches Final Chorus (L18)
            clock_late = _FakeClock(0.0)
            eng_late = LyricSyncEngine(
                clock=clock_late,
                emit_events=False,
                alignment_mode=align_mode,
                phonological_norm=True,
                fuzzy_lookahead_lines=4,
                recovery_lookahead_lines=8,
                position_prior_enabled=True,
            )
            eng_late.reset_with_lyrics(lyrics, track_meta={"song_id": "sp:chorus"})
            eng_late.apply_manual_jump(16)
            clock_late.now = 2.0
            eng_late.update_playback_progress(
                210000, 240000, song_id="sp:chorus", is_playing=True
            )
            clock_late.now = 4.0
            late_match, late_trace = eng_late.evaluate_recognized_text(
                chorus_asr_with_noise
            )
            self.assertIsNotNone(late_match)
            assert late_match is not None
            self.assertEqual(late_match["matched_index"], 18)
            by_idx_late = {c["index"]: c for c in late_trace.get("candidates", [])}
            if 18 in by_idx_late:
                self.assertAlmostEqual(by_idx_late[18]["position_prior"], 1.0)

            # 3. System Media mode (progress_ms=None, duration_ms=None): position prior stays neutral 1.0
            clock_sm = _FakeClock(0.0)
            eng_sm = LyricSyncEngine(
                clock=clock_sm,
                emit_events=False,
                alignment_mode=align_mode,
                phonological_norm=True,
                fuzzy_lookahead_lines=4,
                recovery_lookahead_lines=20,
                position_prior_enabled=True,
            )
            eng_sm.reset_with_lyrics(lyrics, track_meta={"song_id": "smtc:chorus"})
            clock_sm.now = 5.0
            eng_sm.update_playback_progress(None, None, song_id="smtc:chorus")
            m0_sm, _ = eng_sm.evaluate_recognized_text("つめたいよるのしずくがまどをたたいた")
            self.assertIsNotNone(m0_sm)
            clock_sm.now = 65.0
            eng_sm.update_playback_progress(None, None, song_id="smtc:chorus")
            sm_match, sm_trace = eng_sm.evaluate_recognized_text(chorus_asr_with_noise)
            self.assertIsNotNone(sm_match)
            assert sm_match is not None
            self.assertEqual(sm_match["matched_index"], 18)
            for cand in sm_trace.get("candidates", []):
                self.assertAlmostEqual(cand["position_prior"], 1.0)

    def test_spotify_rewind_and_fast_forward_seek_detection_reanchors_engine(
        self,
    ) -> None:
        lyrics = self._build_long_multi_chorus_lyrics()

        for align_mode in ("beam", "line"):
            clock = _FakeClock(0.0)
            eng = LyricSyncEngine(
                clock=clock,
                emit_events=False,
                alignment_mode=align_mode,
                phonological_norm=True,
                position_prior_enabled=True,
                seek_detection_enabled=True,
                seek_rewind_threshold_ms=3500,
                seek_forward_threshold_ms=12000,
                seek_window_ratio_radius=0.35,
            )
            eng.reset_with_lyrics(lyrics, track_meta={"song_id": "sp:seek1"})

            # Advance late into the song (Bridge L19, progress 185s / 240s)
            eng.apply_manual_jump(19)
            clock.now = 10.0
            self.assertIsNone(
                eng.update_playback_progress(
                    185000, 240000, song_id="sp:seek1", is_playing=True
                )
            )
            clock.now = 12.0
            m_late, _ = eng.evaluate_recognized_text("ながれるくもをじっとみつめてた")
            self.assertIsNotNone(m_late)
            assert m_late is not None
            self.assertEqual(m_late["matched_index"], 20)

            # 1. Rewind back to Chorus 1 region (40s / 240s -> ratio 0.167, near L3/L4)
            clock.now = 15.0
            rewind_ev = eng.update_playback_progress(
                40000, 240000, song_id="sp:seek1", is_playing=True
            )
            self.assertIsNotNone(rewind_ev)
            assert rewind_ev is not None
            self.assertEqual(rewind_ev["type"], "rewind")
            self.assertEqual(rewind_ev["from_ms"], 185000)
            self.assertEqual(rewind_ev["to_ms"], 40000)

            prompt_after_rewind = eng.get_upcoming_prompt()
            self.assertTrue(prompt_after_rewind)
            self.assertNotIn("いつかまたあえる", prompt_after_rewind)

            clock.now = 17.0
            rewound_match, rewound_trace = eng.evaluate_recognized_text(
                "きみのなまえをなんどもよんだ"
            )
            self.assertIsNotNone(rewound_match)
            assert rewound_match is not None
            self.assertLess(rewound_match["matched_index"], 10)
            self.assertEqual(rewound_match["matched_index"], 3)
            self.assertTrue(rewound_match["seek_reanchored"])
            self.assertTrue(rewound_trace["seek_reanchored"])
            self.assertEqual(rewound_trace["seek_detected"]["type"], "rewind")

            # 2. Large fast-forward beyond normal lookahead (42s -> 205s / 240s, skipping 15+ lines)
            clock.now = 19.0
            ff_ev = eng.update_playback_progress(
                205000, 240000, song_id="sp:seek1", is_playing=True
            )
            self.assertIsNotNone(ff_ev)
            assert ff_ev is not None
            self.assertEqual(ff_ev["type"], "fast_forward")

            clock.now = 21.0
            ff_match, ff_trace = eng.evaluate_recognized_text(
                "きみのなまえをなんどもよんだ"
            )
            self.assertIsNotNone(ff_match)
            assert ff_match is not None
            self.assertEqual(ff_match["matched_index"], 21)
            self.assertTrue(ff_match["seek_reanchored"])
            self.assertEqual(ff_trace["seek_detected"]["type"], "fast_forward")

            # 3. Rewind recovery even after reaching the final line (end_of_lyrics)
            eng.apply_manual_jump(len(lyrics) - 1)
            clock.now = 25.0
            _, eol_trace = eng.evaluate_recognized_text("てすとてすと")
            self.assertEqual(eol_trace["decision"], "end_of_lyrics")

            clock.now = 27.0
            eol_rewind = eng.update_playback_progress(
                38000, 240000, song_id="sp:seek1", is_playing=True
            )
            self.assertIsNotNone(eol_rewind)
            assert eol_rewind is not None
            self.assertEqual(eol_rewind["type"], "rewind")

            clock.now = 29.0
            recovered_after_eol, _ = eng.evaluate_recognized_text(
                "きみのなまえをなんどもよんだ"
            )
            self.assertIsNotNone(recovered_after_eol)
            assert recovered_after_eol is not None
            self.assertEqual(recovered_after_eol["matched_index"], 3)
            self.assertTrue(recovered_after_eol["seek_reanchored"])

            # 4. Track change guards: different duration_ms OR different song_id must NOT trigger rewind
            clock.now = 32.0
            self.assertIsNone(
                eng.update_playback_progress(
                    2000, 195000, song_id="sp:seek1", is_playing=True
                )
            )
            clock.now = 34.0
            self.assertIsNone(
                eng.update_playback_progress(
                    2000, 240000, song_id="sp:seek1", is_playing=True
                )
            )
            clock.now = 36.0
            eng.update_playback_progress(
                180000, 240000, song_id="sp:seek1", is_playing=True
            )
            clock.now = 38.0
            self.assertIsNone(
                eng.update_playback_progress(
                    1500, 240000, song_id="sp:other_song", is_playing=True
                )
            )

        # 5. Pause -> seek while paused & pause -> >=12s resume without seek
        p_clock = _FakeClock(0.0)
        p_eng = LyricSyncEngine(
            clock=p_clock,
            emit_events=False,
            alignment_mode="beam",
            phonological_norm=True,
        )
        p_eng.reset_with_lyrics(lyrics, track_meta={"song_id": "sp:pause"})
        p_clock.now = 5.0
        p_eng.update_playback_progress(150000, 240000, song_id="sp:pause", is_playing=True)
        # Pausing with track metadata preserves progress history
        p_clock.now = 8.0
        self.assertIsNone(
            p_eng.update_playback_progress(153000, 240000, song_id="sp:pause", is_playing=False)
        )
        # Waiting 15s while paused and resuming with same progress must NOT false-trigger fast_forward
        p_clock.now = 23.0
        self.assertIsNone(
            p_eng.update_playback_progress(153000, 240000, song_id="sp:pause", is_playing=True)
        )
        # Re-pause, then wait 15s and resume 14s ahead (< wall_dt_ms=15s) -> still NOT fast_forward
        p_clock.now = 25.0
        p_eng.update_playback_progress(155000, 240000, song_id="sp:pause", is_playing=False)
        p_clock.now = 40.0
        self.assertIsNone(
            p_eng.update_playback_progress(169000, 240000, song_id="sp:pause", is_playing=True)
        )
        # Pause then drag backwards by 90s while paused -> detects rewind immediately!
        p_clock.now = 42.0
        p_eng.update_playback_progress(171000, 240000, song_id="sp:pause", is_playing=False)
        p_clock.now = 44.0
        pause_drag_ev = p_eng.update_playback_progress(
            40000, 240000, song_id="sp:pause", is_playing=False
        )
        self.assertIsNotNone(pause_drag_ev)
        assert pause_drag_ev is not None
        self.assertEqual(pause_drag_ev["type"], "rewind")

        # 6. Short rewind (5s back between 5s Spotify polls) triggers consistently on live & replay
        sr_clock = _FakeClock(0.0)
        sr_eng = LyricSyncEngine(
            clock=sr_clock,
            emit_events=False,
            alignment_mode="beam",
            phonological_norm=True,
        )
        sr_eng.reset_with_lyrics(lyrics, track_meta={"song_id": "sp:short_rw"})
        sr_clock.now = 5.0
        self.assertIsNone(
            sr_eng.update_playback_progress(100000, 240000, song_id="sp:short_rw", is_playing=True)
        )
        sr_clock.now = 10.0
        sr_ev = sr_eng.update_playback_progress(
            99500, 240000, song_id="sp:short_rw", is_playing=True
        )
        self.assertIsNotNone(sr_ev)
        assert sr_ev is not None
        self.assertEqual(sr_ev["type"], "rewind")

        # 7. Lazy-opened trace during active seek preserves seek_reanchor_active state
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_enabled = False
            lz_clock = _FakeClock(0.0)
            lz_rec = MatchTraceRecorder(
                traces_dir=Path(tmpdir),
                is_enabled_fn=lambda: trace_enabled,
                clock=lz_clock,
            )
            lz_eng = LyricSyncEngine(
                trace_recorder=lz_rec,
                clock=lz_clock,
                emit_events=False,
                alignment_mode="beam",
                phonological_norm=True,
            )
            try:
                lz_eng.reset_with_lyrics(lyrics, track_meta={"song_id": "sp:lazy_seek"})
                lz_eng.apply_manual_jump(19)
                lz_clock.now = 10.0
                lz_eng.update_playback_progress(
                    185000, 240000, song_id="sp:lazy_seek", is_playing=True
                )
                lz_clock.now = 14.0
                lz_ev = lz_eng.update_playback_progress(
                    40000, 240000, song_id="sp:lazy_seek", is_playing=True
                )
                self.assertIsNotNone(lz_ev)
                # Enable trace recording while seek re-anchor is still active before matching
                trace_enabled = True
                lz_clock.now = 16.0
                m_lz, _ = lz_eng.evaluate_recognized_text("きみのなまえをなんどもよんだ")
                self.assertIsNotNone(m_lz)
                assert m_lz is not None
                self.assertEqual(m_lz["matched_index"], 3)
                self.assertTrue(m_lz["seek_reanchored"])
                lz_path = lz_rec.current_trace_path
            finally:
                lz_eng.close_trace_session("done", clear_pending=True)

            assert lz_path is not None
            lz_trace = load_trace(lz_path)
            init_state = lz_trace["session_start"]["initial_state"]
            self.assertTrue(init_state.get("seek_reanchor_active"))
            self.assertEqual(init_state.get("seek_type"), "rewind")
            lz_replay = eval_matching.replay_trace(
                lz_trace, eval_matching.parse_strategy_spec("baseline"), anchor_mode="eval"
            )
            self.assertEqual(lz_replay["summary"]["live_parity_rate"], 1.0)
            self.assertEqual(lz_replay["timeline"][0]["matched_index"], 3)
            self.assertTrue(lz_replay["timeline"][0]["seek_reanchored"])

    def test_eval_matching_beam_pos_prior_reduces_chorus_jumps_and_handles_rewind_trace(
        self,
    ) -> None:
        spec = eval_matching.parse_strategy_spec("beam_pos_prior")
        self.assertEqual(spec.name, "beam_pos_prior")
        preset_defaults = eval_matching.STRATEGIES["beam_pos_prior"]._DEFAULT_OVERRIDES
        self.assertTrue(preset_defaults["position_prior_enabled"])
        self.assertTrue(preset_defaults["seek_detection_enabled"])

        custom = eval_matching.parse_strategy_spec(
            "baseline:align_mode=beam,pos_prior=true,pos_tol=0.12,pos_weight=0.5,"
            "pos_floor=0.55,seek_detect=true,seek_rewind_ms=4000,seek_forward_ms=10000,"
            "seek_radius=0.3"
        )
        self.assertEqual(custom.params["alignment_mode"], "beam")
        self.assertTrue(custom.params["position_prior_enabled"])
        self.assertAlmostEqual(custom.params["position_prior_tolerance"], 0.12)
        self.assertAlmostEqual(custom.params["position_prior_weight"], 0.5)
        self.assertAlmostEqual(custom.params["position_prior_floor"], 0.55)
        self.assertTrue(custom.params["seek_detection_enabled"])
        self.assertEqual(custom.params["seek_rewind_threshold_ms"], 4000)
        self.assertEqual(custom.params["seek_forward_threshold_ms"], 10000)
        self.assertAlmostEqual(custom.params["seek_window_ratio_radius"], 0.3)

        lyrics = self._build_multi_chorus_lyrics()
        trace_data = {
            "path": "synthetic_pos_prior_seek.jsonl",
            "session_start": {
                "type": "session_start",
                "schema_version": 1,
                "track": {
                    "song_name": "ChorusSeekTest",
                    "artist_name": "Tester",
                    "song_id": "sp:trace_seek",
                },
                "asr_config": {"block_seconds": 4.0, "step_seconds": 2.0},
                "matcher_config": {
                    "alignment_mode": "beam",
                    "fuzzy_lookahead_lines": 4,
                    "recovery_lookahead_lines": 20,
                    "position_prior_enabled": True,
                    "seek_detection_enabled": True,
                },
                "initial_state": {"lazy_opened": False, "current_index": -1},
                "lyrics": lyrics,
            },
            "events": [
                {
                    "type": "asr_step",
                    "seq": 1,
                    "elapsed_sec": 5.0,
                    "asr": {"text": "つめたいよるのしずくがまどをたたいた"},
                    "playback": {
                        "spotify_progress_ms": 15000,
                        "estimated_progress_ms": 15000,
                        "duration_ms": 240000,
                        "is_playing": True,
                    },
                    "decision": "matched_phase1",
                    "matched_index": 0,
                },
                {
                    "type": "asr_step",
                    "seq": 2,
                    "elapsed_sec": 65.0,
                    # Ambiguous Chorus 1 ASR with trailing particle + monotonic playback (Phase 2 opens)
                    "asr": {"text": "ひかりのかけらをてのひらにあつめてよ"},
                    "playback": {
                        "spotify_progress_ms": 75000,
                        "estimated_progress_ms": 75000,
                        "duration_ms": 240000,
                        "is_playing": True,
                    },
                    "decision": "matched_phase1",
                    "matched_index": 6,
                },
                {
                    "type": "asr_step",
                    "seq": 3,
                    "elapsed_sec": 69.0,
                    "asr": {"text": "きえないおもいをそらにとばした"},
                    "playback": {
                        "spotify_progress_ms": 79000,
                        "estimated_progress_ms": 79000,
                        "duration_ms": 240000,
                        "is_playing": True,
                    },
                    "decision": "matched_phase1",
                    "matched_index": 7,
                },
                {
                    "type": "asr_step",
                    "seq": 4,
                    "elapsed_sec": 73.0,
                    "asr": {"text": "はるかなみらいへこえをひびかせて"},
                    "playback": {
                        "spotify_progress_ms": 83000,
                        "estimated_progress_ms": 83000,
                        "duration_ms": 240000,
                        "is_playing": True,
                    },
                    "decision": "matched_phase1",
                    "matched_index": 8,
                },
                {
                    "type": "asr_step",
                    "seq": 5,
                    "elapsed_sec": 77.0,
                    # Rewind back to Verse 1 (~15s) after singing Chorus 1
                    "asr": {"text": "つめたいよるのしずくがまどをたたいた"},
                    "playback": {
                        "spotify_progress_ms": 15000,
                        "estimated_progress_ms": 15000,
                        "duration_ms": 240000,
                        "is_playing": True,
                    },
                    "decision": "matched_phase1",
                    "matched_index": 0,
                },
            ],
        }

        opt_spec = eval_matching.parse_strategy_spec("beam_asr_opt:recovery_lookahead=20")
        pos_spec = eval_matching.parse_strategy_spec("beam_pos_prior:recovery_lookahead=20")
        run_opt = eval_matching.replay_trace(trace_data, opt_spec, anchor_mode="eval")
        run_pos = eval_matching.replay_trace(trace_data, pos_spec, anchor_mode="eval")

        # Without pos prior, seq 2 falsely jumps to Final Chorus (L18) and loses the rest of Chorus 1 + rewind;
        # with pos prior, seq 2 stays on L6, follows sequentially L6->L7->L8, and rewind re-anchors back to L0.
        self.assertEqual(run_opt["timeline"][1]["matched_index"], 18)
        self.assertGreater(run_opt["summary"]["max_jump"], run_pos["summary"]["max_jump"])
        self.assertGreater(run_pos["summary"]["line_coverage"], run_opt["summary"]["line_coverage"])
        self.assertGreater(run_pos["summary"]["sequential_rate"], run_opt["summary"]["sequential_rate"])

        self.assertEqual(run_pos["timeline"][1]["matched_index"], 6)
        self.assertEqual(run_pos["timeline"][2]["matched_index"], 7)
        self.assertEqual(run_pos["timeline"][3]["matched_index"], 8)
        self.assertEqual(run_pos["timeline"][4]["matched_index"], 0)
        self.assertGreaterEqual(run_pos["summary"]["seek_reanchors"], 1)
        self.assertGreaterEqual(run_pos["summary"]["rewind_advances"], 1)

        tl_text = eval_matching.format_timeline(run_pos)
        self.assertIn("[SEEK:rewind", tl_text)

        # Explicit empty playback dict clears engine playback state in BaselineStrategy replay
        strat = eval_matching.BaselineStrategy(eval_matching.parse_strategy_spec("beam_pos_prior"))
        vclock = eval_matching.VirtualClock(0.0)
        strat.reset(lyrics, trace_data["session_start"], vclock)
        strat.update_playback_progress(
            {
                "spotify_progress_ms": 80000,
                "estimated_progress_ms": 80000,
                "duration_ms": 240000,
                "is_playing": True,
            }
        )
        self.assertEqual(strat._engine.get_playback_progress()["spotify_progress_ms"], 80000)
        strat.update_playback_progress(
            {
                "spotify_progress_ms": None,
                "estimated_progress_ms": None,
                "duration_ms": None,
                "is_playing": False,
            }
        )
        cleared = strat._engine.get_playback_progress()
        self.assertIsNone(cleared["spotify_progress_ms"])
        self.assertIsNone(cleared["duration_ms"])
        self.assertFalse(cleared["is_playing"])

        # Comment 4653186528 regression: poll-to-ASR offsets must cancel out and preserve est_ms
        # Scenario: poll 1 at t=5s (sp=100_000), ASR step 1 at t=9s (est=104_000),
        # rewind 5.5s at t=10s (sp=99_500), ASR step 2 at t=11s (est=100_500).
        offset_trace = {
            "path": "synthetic_poll_offset_short_rewind.jsonl",
            "session_start": {
                "type": "session_start",
                "schema_version": 1,
                "track": {
                    "song_name": "OffsetRewindTest",
                    "artist_name": "Tester",
                    "song_id": "sp:offset_rw",
                },
                "asr_config": {"block_seconds": 4.0, "step_seconds": 2.0},
                "matcher_config": {
                    "alignment_mode": "beam",
                    "position_prior_enabled": True,
                    "seek_detection_enabled": True,
                },
                "initial_state": {"lazy_opened": True, "current_index": 12},
                "lyrics": lyrics,
            },
            "events": [
                {
                    "type": "asr_step",
                    "seq": 1,
                    "elapsed_sec": 9.0,
                    "asr": {"text": "あめあがりのみちにみずたまり"},
                    "playback": {
                        "spotify_progress_ms": 100000,
                        "estimated_progress_ms": 104000,
                        "duration_ms": 240000,
                        "is_playing": True,
                    },
                    "decision": "matched_phase1",
                    "matched_index": 13,
                },
                {
                    "type": "asr_step",
                    "seq": 2,
                    "elapsed_sec": 11.0,
                    "asr": {"text": "きえないおもいをそらにとばした"},
                    "playback": {
                        "spotify_progress_ms": 99500,
                        "estimated_progress_ms": 100500,
                        "duration_ms": 240000,
                        "is_playing": True,
                    },
                    "decision": "matched_phase1",
                    "matched_index": 7,
                },
            ],
        }
        offset_run = eval_matching.replay_trace(
            offset_trace, eval_matching.parse_strategy_spec("beam_pos_prior"), anchor_mode="eval"
        )
        self.assertEqual(offset_run["summary"]["seek_reanchors"], 1)
        self.assertEqual(offset_run["summary"]["rewind_advances"], 1)
        self.assertEqual(offset_run["timeline"][1]["matched_index"], 7)
        self.assertIsNotNone(offset_run["timeline"][1]["seek_detected"])
        self.assertEqual(offset_run["timeline"][1]["seek_detected"]["type"], "rewind")

        # Verify BaselineStrategy preserves engine estimated_progress_ms without sp-lag
        strat2 = eval_matching.BaselineStrategy(eval_matching.parse_strategy_spec("beam_pos_prior"))
        vclock2 = eval_matching.VirtualClock(9.0)
        strat2.reset(lyrics, offset_trace["session_start"], vclock2)
        strat2.update_playback_progress(offset_trace["events"][0]["playback"])
        self.assertEqual(strat2._engine.get_playback_progress()["estimated_progress_ms"], 104000)
        vclock2.now = 11.0
        strat2.update_playback_progress(offset_trace["events"][1]["playback"])
        self.assertEqual(strat2._engine.get_playback_progress()["estimated_progress_ms"], 100500)


if __name__ == "__main__":
    unittest.main()
