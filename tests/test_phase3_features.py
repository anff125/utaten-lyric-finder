"""Unit tests for Phase 3 audio VU meter, logging capture, CLI entry, and portable manifest."""

import ast
import logging
import math
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = str(_REPO_ROOT / "src")
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import app  # noqa: E402
from utaten_finder.audio import asr_worker  # noqa: E402
from utaten_finder.core import config, events  # noqa: E402


class TestAudioLevelComputationAndEmitter(unittest.TestCase):
    def test_compute_audio_level_edge_cases(self) -> None:
        self.assertEqual(asr_worker.compute_audio_level([]), (0.0, 0.0, True))
        self.assertEqual(
            asr_worker.compute_audio_level(np.zeros(256, dtype=np.float32)),
            (0.0, 0.0, True),
        )

        rms, peak, silent = asr_worker.compute_audio_level(
            np.full(128, 0.5, dtype=np.float32)
        )
        self.assertAlmostEqual(rms, 0.5, places=4)
        self.assertAlmostEqual(peak, 0.5, places=4)
        self.assertFalse(silent)

        clipped = np.array([2.5, -3.0, 0.5], dtype=np.float32)
        c_rms, c_peak, c_silent = asr_worker.compute_audio_level(clipped)
        self.assertAlmostEqual(c_peak, 1.0, places=4)
        self.assertTrue(0.0 <= c_rms <= 1.0)
        self.assertFalse(c_silent)

        non_finite = np.array([np.nan, np.inf, -np.inf, 0.0], dtype=np.float32)
        nf_rms, nf_peak, nf_silent = asr_worker.compute_audio_level(non_finite)
        self.assertFalse(math.isnan(nf_rms))
        self.assertFalse(math.isnan(nf_peak))
        self.assertTrue(0.0 <= nf_rms <= 1.0)
        self.assertTrue(0.0 <= nf_peak <= 1.0)
        self.assertFalse(nf_silent)

    @patch("utaten_finder.audio.asr_worker.time.monotonic")
    def test_audio_level_emitter_throttles_and_allows_source_or_force(
        self, mock_monotonic
    ) -> None:
        received: list[dict] = []
        events.bus.subscribe("audio_level", received.append)
        self.addCleanup(events.bus.unsubscribe, "audio_level", received.append)

        emitter = asr_worker.AudioLevelEmitter(min_interval=0.08)
        mock_monotonic.side_effect = [1.00, 1.02, 1.09, 1.10, 1.11]

        self.assertTrue(
            emitter.emit_samples(np.full(32, 0.25, dtype=np.float32), "Spotify.exe")
        )
        # Within min_interval for same source -> throttled.
        self.assertFalse(
            emitter.emit_samples(np.full(32, 0.30, dtype=np.float32), "Spotify.exe")
        )
        # Past min_interval -> emitted.
        self.assertTrue(
            emitter.emit_samples(np.full(32, 0.40, dtype=np.float32), "Spotify.exe")
        )
        # Source change bypasses throttle.
        self.assertTrue(
            emitter.emit_samples(np.zeros(32, dtype=np.float32), "Default Speakers")
        )
        # force=True bypasses throttle.
        self.assertTrue(
            emitter.emit_values(
                0.1, 0.2, False, "Default Speakers", force=True
            )
        )

        self.assertEqual(len(received), 4)
        self.assertEqual(received[0]["source_name"], "Spotify.exe")
        self.assertFalse(received[0]["silent"])
        self.assertEqual(received[2]["source_name"], "Default Speakers")
        self.assertTrue(received[2]["silent"])
        self.assertEqual(received[3], {"rms": 0.1, "peak": 0.2, "silent": False, "source_name": "Default Speakers"})


class TestEventBusAndLoggingCapture(unittest.TestCase):
    def test_has_subscribers_and_startup_progress_stage_under_bus_listener(self) -> None:
        received: list[dict] = []
        self.assertFalse(events.bus.has_subscribers("startup_progress"))

        events.bus.subscribe("startup_progress", received.append)
        try:
            self.assertTrue(events.bus.has_subscribers("startup_progress"))
            orig_active = config.STARTUP_PROGRESS_ACTIVE
            orig_cb = config._startup_progress_callback
            config.STARTUP_PROGRESS_ACTIVE = False
            config._startup_progress_callback = None
            try:
                with config.startup_progress_stage(0.1, 0.5, "測試階段"):
                    pass
            finally:
                config.STARTUP_PROGRESS_ACTIVE = orig_active
                config._startup_progress_callback = orig_cb
        finally:
            events.bus.unsubscribe("startup_progress", received.append)

        self.assertFalse(events.bus.has_subscribers("startup_progress"))
        self.assertGreaterEqual(len(received), 1)
        self.assertEqual(received[0]["message"], "測試階段")
        self.assertAlmostEqual(received[0]["fraction"], 0.1, places=4)

    def test_logging_capture_and_pythonw_none_streams(self) -> None:
        orig_stdout = sys.stdout
        orig_stderr = sys.stderr
        orig_flag = events._capture_installed
        test_logger = logging.getLogger("utaten_finder.test_phase3")
        orig_level = test_logger.level

        events.clear_recent_logs()
        events._capture_installed = False

        try:
            # Simulate pythonw.exe where stdout/stderr start as None.
            sys.stdout = None  # type: ignore[assignment]
            sys.stderr = None  # type: ignore[assignment]
            events.install_console_log_capture()

            test_logger.setLevel(logging.DEBUG)
            test_logger.debug("debug line")
            test_logger.info("info line")
            test_logger.warning("warn line")
            test_logger.error("error line")
            print("stdout line under pythonw")

            entries = events.get_recent_logs()
            matched = {
                entry["message"]: entry["level"]
                for entry in entries
                if "line" in entry["message"]
            }
            self.assertEqual(
                matched,
                {
                    "[utaten_finder.test_phase3] debug line": "debug",
                    "[utaten_finder.test_phase3] info line": "info",
                    "[utaten_finder.test_phase3] warn line": "warn",
                    "[utaten_finder.test_phase3] error line": "error",
                    "stdout line under pythonw": "info",
                },
            )
            # Ensure no duplicate emission from logging -> stderr -> EventBus.
            self.assertEqual(
                sum(1 for e in entries if "info line" in e["message"]),
                1,
            )
        finally:
            test_logger.setLevel(orig_level)
            if not orig_flag:
                events._uninstall_console_log_capture_handler()
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr
            events._capture_installed = orig_flag
            events.clear_recent_logs()


class TestAppEntryAndPortableManifest(unittest.TestCase):
    def test_app_cli_routing(self) -> None:
        with patch("utaten_finder.core.orchestrator.main") as mock_orch:
            rc = app.main(["--cli"])
            self.assertEqual(rc, 0)
            mock_orch.assert_called_once_with(open_external_browser=True)

        with (
            patch("utaten_finder.core.events.install_console_log_capture") as mock_cap,
            patch("utaten_finder.ui.webview_app.main") as mock_webview,
        ):
            rc_default = app.main([])
            self.assertEqual(rc_default, 0)
            mock_cap.assert_called_once()
            mock_webview.assert_called_once_with(debug=False)

        with (
            patch("utaten_finder.core.events.install_console_log_capture"),
            patch("utaten_finder.ui.webview_app.main") as mock_webview,
        ):
            rc_debug = app.main(["--debug"])
            self.assertEqual(rc_debug, 0)
            mock_webview.assert_called_once_with(debug=True)

        with self.assertRaises(SystemExit) as ctx:
            app.main(["--legacy-ui"])
        self.assertEqual(ctx.exception.code, 2)

    def test_portable_update_manifest_matches_repo_sources(self) -> None:
        ps1_text = (_REPO_ROOT / "scripts" / "build_portable.ps1").read_text(
            encoding="utf-8-sig"
        )
        list_lines = re.findall(
            r'>> "%TEMP_PY%" echo\s+(.*)',
            ps1_text,
        )
        py_snippet = "\n".join(line.rstrip() for line in list_lines)
        match = re.search(r"files\s*=\s*(\[.*?\])", py_snippet, flags=re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None
        manifest_files = set(ast.literal_eval(match.group(1)))

        expected_files = {"app.py"}
        for py_path in (_REPO_ROOT / "src" / "utaten_finder").rglob("*.py"):
            if "__pycache__" not in py_path.parts:
                expected_files.add(py_path.relative_to(_REPO_ROOT).as_posix())
        for web_path in (_REPO_ROOT / "assets" / "web").iterdir():
            if web_path.is_file():
                expected_files.add(web_path.relative_to(_REPO_ROOT).as_posix())

        self.assertEqual(manifest_files, expected_files)
        self.assertNotIn("src/utaten_finder/ui/legacy_gui.py", manifest_files)
        self.assertNotIn("src/utaten_finder/lyrics/browser_sync.py", manifest_files)


if __name__ == "__main__":
    unittest.main()
