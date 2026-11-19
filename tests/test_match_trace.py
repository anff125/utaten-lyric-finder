"""Unit tests for match trace recording, engine diagnostics, and offline replay."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = str(_REPO_ROOT / "src")
_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import eval_matching  # noqa: E402
from utaten_finder.audio import asr_worker  # noqa: E402
from utaten_finder.core import config  # noqa: E402
from utaten_finder.lyrics.matching import (  # noqa: E402
    LyricSyncEngine,
    calculate_similarity_normalized,
    score_similarity_detailed,
)
from utaten_finder.lyrics.trace import (  # noqa: E402
    TRACE_SCHEMA_VERSION,
    MatchTraceRecorder,
    load_trace,
    sanitize_trace_slug,
)
from utaten_finder.ui.webview_app import AppController  # noqa: E402


class _FakeClock:
    def __init__(self, initial: float = 100.0) -> None:
        self.now = float(initial)

    def __call__(self) -> float:
        return self.now


def _sample_lyrics() -> list[dict]:
    lines = [
        ("優しい嘘をついてくれよ", "やさしいうそをついてくれよ", "<ruby>優<rt>やさ</rt></ruby>しい<ruby>嘘<rt>うそ</rt></ruby>をついてくれよ"),
        ("現実は残酷だもの", "げんじつはざんこくだもの", "<ruby>現実<rt>げんじつ</rt></ruby>は<ruby>残酷<rt>ざんこく</rt></ruby>だもの"),
        ("夢の中で君を探してる", "ゆめのなかできみをさがしてる", "<ruby>夢<rt>ゆめ</rt></ruby>の<ruby>中<rt>なか</rt></ruby>で<ruby>君<rt>きみ</rt></ruby>を<ruby>探<rt>さが</rt></ruby>してる"),
        ("夜が明けるまで踊り明かそう", "よるがあけるまでおどりあかそう", "<ruby>夜<rt>よる</rt></ruby>が<ruby>明<rt>あ</rt></ruby>けるまで<ruby>踊<rt>おど</rt></ruby>り<ruby>明<rt>あ</rt></ruby>かそう"),
        ("光がさす方へ歩き出す", "ひかりがさすほうへあるきだす", "<ruby>光<rt>ひかり</rt></ruby>がさす<ruby>方<rt>ほう</rt></ruby>へ<ruby>歩<rt>ある</rt></ruby>き<ruby>出<rt>だ</rt></ruby>す"),
        ("青い空を見上げながら", "あおいそらをみあげながら", "<ruby>青<rt>あお</rt></ruby>い<ruby>空<rt>そら</rt></ruby>を<ruby>見上<rt>みあ</rt></ruby>げながら"),
        ("忘れられない言葉を抱いて", "わすれられないことばをだいて", "<ruby>忘<rt>わす</rt></ruby>れられない<ruby>言葉<rt>ことば</rt></ruby>を<ruby>抱<rt>だ</rt></ruby>いて"),
    ]
    return [
        {
            "index": idx,
            "original": orig,
            "normalized": norm,
            "ruby_html": ruby,
            "stanza_break": idx == 3,
        }
        for idx, (orig, norm, ruby) in enumerate(lines)
    ]


class TestDetailedSimilarityParity(unittest.TestCase):
    def test_detailed_score_matches_normalized_similarity(self) -> None:
        cases = [
            ("", ""),
            ("", "やさしいうそ"),
            ("やさしいうそ", ""),
            ("やさしいうそをついてくれよ", "やさしいうそをついてくれよ"),
            ("やさ", "やさしいうそをついてくれよ"),
            ("やさしいうそ", "やさしいうそをついてくれよ"),
            ("やさしいうそをついてくれな", "やさしいうそをついてくれよ"),
        ]
        for na, nb in cases:
            detailed = score_similarity_detailed(na, nb, min_coverage_ratio=0.4)
            legacy = calculate_similarity_normalized(na, nb, min_coverage_ratio=0.4)
            self.assertEqual(detailed["final_score"], legacy)
            self.assertTrue(0.0 <= detailed["coverage"] <= 1.0)
            self.assertTrue(0.0 <= detailed["partial_score"] <= 1.0)
            self.assertTrue(0.0 <= detailed["ratio_score"] <= 1.0)


class TestMatchTraceRecorder(unittest.TestCase):
    def test_sanitize_slug_and_collision_avoidance(self) -> None:
        self.assertEqual(
            sanitize_trace_slug('King Gnu: <Sorrows>? / *"Test"\x01'),
            "King_Gnu_Sorrows_Test",
        )
        self.assertEqual(sanitize_trace_slug("   ...   "), "unknown_track")

        with tempfile.TemporaryDirectory() as tmpdir:
            clock = _FakeClock(10.0)
            recorder = MatchTraceRecorder(
                traces_dir=Path(tmpdir),
                is_enabled_fn=lambda: True,
                clock=clock,
            )
            lyrics = _sample_lyrics()[:2]
            p1 = recorder.start_session({"song_name": "Sorrows", "artist_name": "King Gnu"}, lyrics)
            p2 = recorder.start_session({"song_name": "Sorrows", "artist_name": "King Gnu"}, lyrics)
            recorder.close_session("done")

            self.assertIsNotNone(p1)
            self.assertIsNotNone(p2)
            assert p1 is not None and p2 is not None
            self.assertNotEqual(p1, p2)
            self.assertTrue(p1.exists())
            self.assertTrue(p2.exists())

    def test_lazy_open_and_mid_session_disable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            enabled = False
            clock = _FakeClock(50.0)
            recorder = MatchTraceRecorder(
                traces_dir=Path(tmpdir),
                is_enabled_fn=lambda: enabled,
                clock=clock,
            )
            lyrics = _sample_lyrics()

            # Start while disabled -> no file opened yet, session staged.
            self.assertIsNone(recorder.start_session({"song_name": "Test"}, lyrics))
            self.assertFalse(recorder.is_active)
            self.assertFalse(recorder.record_asr_step({"decision": "no_match"}))

            # Enable mid-song -> lazily opens file & writes session_start first.
            enabled = True
            clock.now = 52.5
            self.assertTrue(recorder.record_asr_step({"decision": "matched_phase1"}))
            trace_path = recorder.current_trace_path
            self.assertIsNotNone(trace_path)
            assert trace_path is not None

            # Manual jump while active.
            clock.now = 55.0
            self.assertTrue(recorder.record_manual_jump({"from_index": 0, "to_index": 2}))

            # Disable mid-song -> closes file with reason='disabled'.
            enabled = False
            clock.now = 56.0
            self.assertFalse(recorder.record_asr_step({"decision": "no_match"}))
            self.assertFalse(recorder.is_active)

            loaded = load_trace(trace_path)
            self.assertEqual(loaded["session_start"]["schema_version"], TRACE_SCHEMA_VERSION)
            self.assertEqual(len(loaded["session_start"]["lyrics"]), len(lyrics))
            self.assertIn("<ruby>", loaded["session_start"]["lyrics"][0]["ruby_html"])
            self.assertEqual([e["type"] for e in loaded["events"]], ["asr_step", "manual_jump"])
            self.assertIsNotNone(loaded["session_end"])
            self.assertEqual(loaded["session_end"]["reason"], "disabled")
            self.assertEqual(
                loaded["session_end"]["stats"],
                {
                    "asr_steps": 1,
                    "matches_phase1": 1,
                    "matches_phase2": 0,
                    "manual_jumps": 1,
                },
            )

    def test_load_trace_requires_session_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_path = Path(tmpdir) / "bad.jsonl"
            bad_path.write_text(json.dumps({"type": "asr_step"}) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_trace(bad_path)


class TestEngineDiagnosticsAndProgress(unittest.TestCase):
    def test_engine_records_all_decision_paths_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = _FakeClock(1000.0)
            recorder = MatchTraceRecorder(
                traces_dir=Path(tmpdir),
                is_enabled_fn=lambda: True,
                clock=clock,
            )
            engine = LyricSyncEngine(trace_recorder=recorder, clock=clock, emit_events=False)
            lyrics = _sample_lyrics()

            try:
                engine.update_playback_progress(12000, 180000, song_id="sp-1", is_playing=True)
                engine.reset_with_lyrics(
                    lyrics,
                    track_meta={"song_id": "sp-1", "song_name": "Sorrows", "artist_name": "King Gnu"},
                )

                # 1. Filtered/empty ASR window with asr_meta -> asr_empty_or_filtered
                clock.now = 1002.0
                payload, trace_ev = engine.evaluate_recognized_text(
                    "",
                    asr_meta={"raw_text": "", "raw_text_before_filter": "ご視聴ありがとうございました", "infer_ms": 120.0},
                )
                self.assertIsNone(payload)
                self.assertEqual(trace_ev["decision"], "asr_empty_or_filtered")
                self.assertEqual(trace_ev["playback"]["estimated_progress_ms"], 14000)

                # 2. Phase 1 match on line 0
                clock.now = 1004.0
                m1, t1 = engine.evaluate_recognized_text(
                    "優しい嘘をついてくれよ",
                    asr_meta={"raw_text": "優しい嘘をついてくれよ", "infer_ms": 210.5},
                )
                self.assertIsNotNone(m1)
                assert m1 is not None
                self.assertEqual(m1["matched_index"], 0)
                self.assertEqual(t1["decision"], "matched_phase1")
                self.assertTrue(len(t1["candidates"]) >= 1)
                self.assertIn("coverage", t1["candidates"][0])
                self.assertEqual(t1["playback"]["estimated_progress_ms"], 16000)

                # 3. Duplicate within TTL -> skipped_duplicate_ttl
                clock.now = 1005.0
                m2, t2 = engine.evaluate_recognized_text("優しい嘘をついてくれよ")
                self.assertIsNone(m2)
                self.assertEqual(t2["decision"], "skipped_duplicate_ttl")

                # 4. Unmatched text -> no_match
                clock.now = 1006.0
                m3, t3 = engine.evaluate_recognized_text("ぜんぜんちがうことば")
                self.assertIsNone(m3)
                self.assertEqual(t3["decision"], "no_match")

                # 5. Reset to line 0 via manual jump, establish buffer_start_at, then advance
                # past max lookahead (Phase 1 covers [1, 5]; line 6 falls in Phase 2 recovery).
                jump_info = engine.apply_manual_jump(0)
                self.assertIsNotNone(jump_info)

                clock.now = 1020.0
                engine.evaluate_recognized_text("あ")
                clock.now = 1030.0
                m4, t4 = engine.evaluate_recognized_text("忘れられない言葉を抱いて")
                self.assertIsNotNone(m4)
                assert m4 is not None
                self.assertEqual(t4["decision"], "matched_phase2_recovery")
                self.assertEqual(m4["matched_index"], 6)

                first_trace_path = recorder.current_trace_path
                self.assertIsNotNone(first_trace_path)

                # 6. Switching to a different song_id resets playback progress and opens new trace
                engine.reset_with_lyrics(lyrics, track_meta={"song_id": "sp-2"})
                _, t5 = engine.evaluate_recognized_text("優しい嘘をついてくれよ")
                self.assertIsNone(t5["playback"]["spotify_progress_ms"])

                second_trace_path = recorder.current_trace_path
                self.assertIsNotNone(second_trace_path)
                self.assertNotEqual(first_trace_path, second_trace_path)
            finally:
                engine.close_trace_session("test_done")

            assert first_trace_path is not None
            loaded = load_trace(first_trace_path)
            decisions = [e["decision"] for e in loaded["events"] if e["type"] == "asr_step"]
            self.assertIn("asr_empty_or_filtered", decisions)
            self.assertIn("matched_phase1", decisions)
            self.assertIn("skipped_duplicate_ttl", decisions)
            self.assertIn("no_match", decisions)
            self.assertIn("matched_phase2_recovery", decisions)


class TestAsrCallbackAndSettingsIntegration(unittest.TestCase):
    def test_invoke_asr_callback_supports_single_and_two_arg_signatures(self) -> None:
        calls_one: list[str] = []
        calls_two: list[tuple[str, dict]] = []

        def cb_one(text: str) -> None:
            calls_one.append(text)

        def cb_two(text: str, meta: dict) -> None:
            calls_two.append((text, meta))

        self.assertFalse(asr_worker._callback_accepts_meta(cb_one))
        self.assertTrue(asr_worker._callback_accepts_meta(cb_two))
        self.assertTrue(asr_worker._callback_accepts_meta(lambda *args: None))

        asr_worker._invoke_asr_callback(cb_one, "hello", {"infer_ms": 10})
        asr_worker._invoke_asr_callback(cb_two, "hello", {"infer_ms": 10})
        self.assertEqual(calls_one, ["hello"])
        self.assertEqual(calls_two, [("hello", {"infer_ms": 10})])

    @patch("utaten_finder.ui.webview_app.has_valid_cached_token", return_value=False)
    def test_config_and_app_controller_persist_match_trace_setting(self, _mock_token) -> None:
        orig_file = config.RUNTIME_SETTINGS_FILE
        orig_enabled = config.MATCH_TRACE_ENABLED
        self.addCleanup(setattr, config, "RUNTIME_SETTINGS_FILE", orig_file)
        self.addCleanup(setattr, config, "MATCH_TRACE_ENABLED", orig_enabled)

        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = Path(tmpdir) / "runtime_settings.json"
            config.RUNTIME_SETTINGS_FILE = settings_path
            config.set_match_trace_enabled(False, persist=False)

            recorder = MatchTraceRecorder(traces_dir=Path(tmpdir) / "traces")
            controller = AppController(trace_recorder=recorder)
            self.addCleanup(controller.shutdown)

            initial = controller.get_initial_state()
            self.assertFalse(initial["settings"]["match_trace_enabled"])

            res_on = controller.save_settings({"match_trace_enabled": True})
            self.assertTrue(res_on["ok"])
            self.assertTrue(config.MATCH_TRACE_ENABLED)
            saved_data = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertTrue(saved_data["MATCH_TRACE_ENABLED"])

            # Start a trace session, then disable via save_settings -> closes session.
            controller._sync_engine.reset_with_lyrics(
                _sample_lyrics(),
                track_meta={"song_name": "Sorrows", "artist_name": "King Gnu"},
            )
            self.assertTrue(recorder.is_active)

            res_off = controller.save_settings({"match_trace_enabled": False})
            self.assertTrue(res_off["ok"])
            self.assertFalse(config.MATCH_TRACE_ENABLED)
            self.assertFalse(recorder.is_active)


class TestEvalMatchingReplay(unittest.TestCase):
    def _record_synthetic_trace(self, tmpdir: Path) -> Path:
        clock = _FakeClock(0.0)
        recorder = MatchTraceRecorder(
            traces_dir=tmpdir,
            is_enabled_fn=lambda: True,
            clock=clock,
        )
        engine = LyricSyncEngine(trace_recorder=recorder, clock=clock, emit_events=False)
        lyrics = _sample_lyrics()

        engine.update_playback_progress(5000, 180000, song_id="sp-eval", is_playing=True)
        engine.reset_with_lyrics(
            lyrics,
            track_meta={
                "song_id": "sp-eval",
                "song_name": "EvalSong",
                "artist_name": "EvalArtist",
                "title": "EvalSong",
                "artist": "EvalArtist",
            },
        )

        clock.now = 2.0
        engine.process_recognized_text(
            "優しい嘘をついてくれよ",
            asr_meta={"raw_text": "優しい嘘をついてくれよ", "infer_ms": 250.0, "no_speech_prob_max": 0.05, "avg_logprob_mean": -0.2},
        )
        clock.now = 4.0
        engine.process_recognized_text(
            "げんじつわさんこくだもの",
            asr_meta={"raw_text": "げんじつわさんこくだもの", "infer_ms": 300.0, "no_speech_prob_max": 0.04, "avg_logprob_mean": -0.25},
        )
        clock.now = 6.0
        engine.apply_manual_jump(2)
        clock.now = 8.0
        engine.process_recognized_text(
            "夜が明けるまで踊り明かそう",
            asr_meta={"raw_text": "夜が明けるまで踊り明かそう", "infer_ms": 2200.0, "no_speech_prob_max": 0.03, "avg_logprob_mean": -0.18},
        )

        trace_path = recorder.current_trace_path
        assert trace_path is not None
        engine.close_trace_session("done")
        return trace_path

    def test_baseline_apply_mode_achieves_full_live_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_path = self._record_synthetic_trace(Path(tmpdir))
            loaded = load_trace(trace_path)

            run_apply = eval_matching.replay_trace(
                loaded,
                eval_matching.parse_strategy_spec("baseline"),
                anchor_mode="apply",
            )
            summary = run_apply["summary"]
            self.assertEqual(summary["live_parity_rate"], 1.0)
            self.assertEqual(summary["asr_steps"], 3)
            self.assertEqual(summary["total_matches"], 3)
            self.assertEqual(summary["anchors_total"], 1)
            self.assertEqual(summary["anchor_exact"], 1)
            self.assertEqual(summary["infer_over_budget_count"], 1)

    def test_strategy_comparison_diff_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            trace_path = self._record_synthetic_trace(tmp)
            md_out = tmp / "report.md"
            json_out = tmp / "report.json"

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = eval_matching.main(
                    [
                        str(trace_path),
                        "--strategy",
                        "baseline",
                        "--strategy",
                        "baseline:threshold=0.95",
                        "--anchor-mode",
                        "eval",
                        "--timeline",
                        "--diff",
                        "--export-md",
                        str(md_out),
                        "--export-json",
                        str(json_out),
                    ]
                )
            self.assertEqual(rc, 0)
            stdout_text = buf.getvalue()
            self.assertIn("Strategy Diffs", stdout_text)
            self.assertIn("Timeline:", stdout_text)
            self.assertTrue(md_out.is_file())
            self.assertTrue(json_out.is_file())

            md_text = md_out.read_text(encoding="utf-8")
            self.assertIn("`baseline`", md_text)
            self.assertIn("`baseline:threshold=0.95`", md_text)

            json_data = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(len(json_data["overall"]), 2)
            self.assertGreater(
                json_data["overall"][0]["total_matches"],
                json_data["overall"][1]["total_matches"],
            )


if __name__ == "__main__":
    unittest.main()
