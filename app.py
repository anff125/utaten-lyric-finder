"""Single entry point for UtaTen Lyric Finder."""

import argparse
from pathlib import Path
import sys


def _bootstrap_src_path() -> None:
    src_dir = str(Path(__file__).resolve().parent / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)


def main(argv: list[str] | None = None) -> int:
    _bootstrap_src_path()

    parser = argparse.ArgumentParser(description="UtaTen Lyric Finder")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Run in console/headless orchestrator mode without opening the GUI.",
    )
    args = parser.parse_args(argv)

    if args.cli:
        from utaten_finder.core.orchestrator import main as run_cli

        try:
            run_cli()
        except KeyboardInterrupt:
            print("\n🛑 收到終止訊號，正在關閉程式...")
        return 0

    from utaten_finder.core import config

    config.STARTUP_PROGRESS_ACTIVE = True
    config.report_startup_progress(
        0.20, "正在載入基礎設定與快取模組...", console_only=True
    )
    from utaten_finder.ui.legacy_gui import main as run_gui

    run_gui()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
