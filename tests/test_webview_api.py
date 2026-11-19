"""Unit tests for JsApi, AppController, and EventBus console capture."""

import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

_SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from utaten_finder.core import events  # noqa: E402
from utaten_finder.ui.webview_app import AppController, JsApi  # noqa: E402


class TestJsApiAndController(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = AppController()
        self.api = JsApi(self.controller)
        self.addCleanup(self.controller.shutdown)

    def test_js_api_exposes_only_expected_public_methods(self) -> None:
        public_attrs = [name for name in dir(self.api) if not name.startswith("_")]
        expected = {
            "clear_logs",
            "exchange_callback_token",
            "get_initial_state",
            "jump_to_line",
            "open_external_url",
            "research_lyrics",
            "save_settings",
            "start_listening",
            "start_spotify_auth",
            "stop_listening",
            "update_manual_url",
        }
        self.assertEqual(set(public_attrs), expected)
        for name in public_attrs:
            self.assertTrue(callable(getattr(self.api, name)))

    @patch("utaten_finder.ui.webview_app.has_valid_cached_token", return_value=False)
    def test_get_initial_state_is_json_serializable(self, _mock_token) -> None:
        state = self.api.get_initial_state()
        encoded = json.dumps(state)
        self.assertIn("settings", state)
        self.assertIn("asr_models", state)
        self.assertIn("logs", state)
        self.assertTrue(len(encoded) > 10)

    def test_jump_to_line_updates_engine_and_emits_highlight(self) -> None:
        self.controller._sync_engine.reset_with_lyrics(
            [
                {"index": 0, "original": "いちぎょうめ", "normalized": "いちぎょうめ"},
                {"index": 1, "original": "にぎょうめ", "normalized": "にぎょうめ"},
            ]
        )
        received: list[dict] = []
        events.bus.subscribe("lyric_highlighted", received.append)
        self.addCleanup(events.bus.unsubscribe, "lyric_highlighted", received.append)

        res = self.api.jump_to_line(1)
        self.assertTrue(res["ok"])
        self.assertEqual(self.controller._sync_engine.get_current_index(), 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["index"], 1)
        self.assertTrue(received[0]["manual"])

    @patch("utaten_finder.ui.webview_app.webbrowser.open")
    def test_open_external_url_validates_scheme(self, mock_open) -> None:
        bad_file = self.api.open_external_url("file:///C:/Windows/System32/calc.exe")
        self.assertFalse(bad_file["ok"])
        bad_js = self.api.open_external_url("javascript:alert(1)")
        self.assertFalse(bad_js["ok"])
        mock_open.assert_not_called()

        ok_res = self.api.open_external_url("https://utaten.com/lyric/ac19030854/")
        self.assertTrue(ok_res["ok"])
        mock_open.assert_called_once_with("https://utaten.com/lyric/ac19030854/")

    @patch("utaten_finder.ui.webview_app.load_lyrics_into_engine")
    @patch("utaten_finder.ui.webview_app.set_cached_data")
    def test_update_manual_url_validates_and_dispatches_loader(
        self, mock_cache, mock_loader
    ) -> None:
        invalid = self.api.update_manual_url("not-a-url")
        self.assertFalse(invalid["ok"])
        mock_cache.assert_not_called()

        done = threading.Event()
        mock_loader.side_effect = lambda *args, **kwargs: done.set()

        self.controller._current_track = {
            "song_id": "test-song-1",
            "song_name": "Sorrows",
            "artist_name": "King Gnu",
            "source": "Spotify",
        }
        res = self.api.update_manual_url("https://www.utaten.com/lyric/ac19030854/")
        self.assertTrue(res["ok"])
        self.assertEqual(res["url"], "https://utaten.com/lyric/ac19030854/")
        mock_cache.assert_called_once_with(
            "test-song-1",
            "https://utaten.com/lyric/ac19030854/",
            "perfect",
            "King Gnu",
            "Sorrows",
        )
        self.assertTrue(done.wait(timeout=2.0))


class TestConsoleLogCapture(unittest.TestCase):
    def test_install_console_log_capture_records_and_skips_progress(self) -> None:
        orig_stdout = sys.stdout
        orig_stderr = sys.stderr
        orig_flag = events._capture_installed
        events.clear_recent_logs()
        events._capture_installed = False

        captured_events: list[dict] = []
        events.bus.subscribe("log_message", captured_events.append)

        try:
            events.install_console_log_capture()
            sys.stdout.write("\r  [████░░░░░░░░░░░░░░░░]  20% | loading...")
            print("⚠️ test warning log")
            print("❌ test error log")

            messages = [entry["message"] for entry in events.get_recent_logs()]
            self.assertIn("⚠️ test warning log", messages)
            self.assertIn("❌ test error log", messages)
            self.assertFalse(any("loading..." in m for m in messages))

            levels = {entry["message"]: entry["level"] for entry in captured_events}
            self.assertEqual(levels.get("⚠️ test warning log"), "warn")
            self.assertEqual(levels.get("❌ test error log"), "error")
        finally:
            events.bus.unsubscribe("log_message", captured_events.append)
            if not orig_flag:
                events._uninstall_console_log_capture_handler()
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr
            events._capture_installed = orig_flag
            events.clear_recent_logs()


if __name__ == "__main__":
    unittest.main()
