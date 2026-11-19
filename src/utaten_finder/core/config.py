import contextlib
import json
from pathlib import Path
import shutil
import sys
import threading
import time
from typing import Callable

from utaten_finder.core.events import bus

CLIENT_ID = ""
CLIENT_SECRET = ""
REDIRECT_URI = "http://127.0.0.1:8888/callback"

SCOPE = "user-read-currently-playing"

FUZZY_LOOKAHEAD_LINES = 4
FUZZY_NEXT_LINE_THRESHOLD = 0.75
# 迷失恢復機制 (Phase 2 Lookahead)
RECOVERY_LOOKAHEAD_LINES = 8
RECOVERY_THRESHOLD = 0.60

ASR_ENABLED = True
ASR_MODEL_SIZE = "turbo"

# ASR 模型推薦設定與硬體說明
ASR_MODEL_MAP = {
    "turbo (推薦首選 | VRAM >= 3GB | 速度最快)": "turbo",
    "kotoba-whisper-v2.0 (日語專用強化 | VRAM >= 3GB)": "kotoba-tech/kotoba-whisper-v2.0-faster",
    "large-v3 (高精準度 | VRAM >= 6GB)": "large-v3",
    "medium (中等均衡 | VRAM >= 2GB / 效能 CPU)": "medium",
    "small (CPU 推薦首選 | 輕量免顯卡)": "small",
    "base (極速輕量 | 舊 CPU / 低資源)": "base",
}


def get_model_display_name(model_id: str) -> str:
    """根據實際 model_id 取得 UI 顯示名稱；若找不到則回傳原字串。"""
    for display, mid in ASR_MODEL_MAP.items():
        if mid == model_id or display == model_id:
            return display
    return model_id


def get_model_id_from_display(display_name: str) -> str:
    """根據 UI 顯示名稱取得實際 model_id；若非預設選項則回傳原字串（支援自訂輸入）。"""
    return ASR_MODEL_MAP.get(display_name, display_name)


ASR_DEVICE = "auto"
ASR_COMPUTE_TYPE = "default"
ASR_LANGUAGE = "ja"
ASR_SAMPLE_RATE = 16000
ASR_BLOCK_SECONDS = 4

SPOTIFY_POLL_SECONDS = 5
MAIN_LOOP_SLEEP_SECONDS = 0.5

DEBUG_POLLING = False
DEBUG_RECOGNITION = False
DEBUG = False
# 支援: spotify / system_media
AUDIO_SOURCE_MODE = "spotify"
# 當音源為 spotify 時，是否只錄製 Spotify 程序的獨立音訊 (Windows 11 Process Loopback)
CAPTURE_SPOTIFY_PROCESS_ONLY = True

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"
ASSETS_DIR = PROJECT_ROOT / "assets"
RUNTIME_SETTINGS_FILE = DATA_DIR / "runtime_settings.json"
SONG_CACHE_FILE = DATA_DIR / "song_cache.json"
SPOTIFY_TOKEN_CACHE_FILE = DATA_DIR / "spotify_token_cache"
SPOTIFY_LAST_CALLBACK_FILE = DATA_DIR / "spotify_last_callback_url.txt"

_PERSISTED_KEYS = {
    "CLIENT_ID",
    "CLIENT_SECRET",
    "REDIRECT_URI",
    "DEBUG_POLLING",
    "DEBUG_RECOGNITION",
    "DEBUG",
    "AUDIO_SOURCE_MODE",
    "CAPTURE_SPOTIFY_PROCESS_ONLY",
    "ASR_ENABLED",
    "ASR_MODEL_SIZE",
    "ASR_DEVICE",
}


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _migrate_legacy_data_files() -> None:
    """Copy legacy root/.cache/_cache runtime files into data/ if not yet migrated."""
    _ensure_data_dir()
    legacy_mappings = (
        (
            RUNTIME_SETTINGS_FILE,
            (
                PROJECT_ROOT / ".cache" / "runtime_settings.json",
                PROJECT_ROOT / "_cache" / "runtime_settings.json",
            ),
        ),
        (
            SPOTIFY_TOKEN_CACHE_FILE,
            (
                PROJECT_ROOT / ".cache" / "spotify_token_cache",
                PROJECT_ROOT / "_cache" / "spotify_token_cache",
            ),
        ),
        (
            SPOTIFY_LAST_CALLBACK_FILE,
            (
                PROJECT_ROOT / ".cache" / "spotify_last_callback_url.txt",
                PROJECT_ROOT / "_cache" / "spotify_last_callback_url.txt",
            ),
        ),
        (
            SONG_CACHE_FILE,
            (PROJECT_ROOT / "song_cache.json",),
        ),
    )

    for target_path, candidates in legacy_mappings:
        if target_path.exists():
            continue
        for source_path in candidates:
            if source_path.is_file():
                try:
                    shutil.copy2(source_path, target_path)
                except Exception:
                    pass
                break


def _save_runtime_settings() -> None:
    try:
        _ensure_data_dir()
        data = {key: globals()[key] for key in _PERSISTED_KEYS}
        with open(RUNTIME_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        # 設定儲存失敗時不應中斷主要流程。
        return


def _load_runtime_settings() -> None:
    if not RUNTIME_SETTINGS_FILE.exists():
        return

    try:
        with open(RUNTIME_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    if not isinstance(data, dict):
        return

    client_id = data.get("CLIENT_ID", CLIENT_ID)
    client_secret = data.get("CLIENT_SECRET", CLIENT_SECRET)
    redirect_uri = data.get("REDIRECT_URI", REDIRECT_URI)
    set_spotify_credentials(client_id, client_secret, redirect_uri, persist=False)

    polling_debug = data.get("DEBUG_POLLING", data.get("DEBUG", DEBUG))
    recognition_debug = data.get("DEBUG_RECOGNITION", data.get("DEBUG", DEBUG))
    set_polling_debug(polling_debug, persist=False)
    set_recognition_debug(recognition_debug, persist=False)
    set_asr_enabled(data.get("ASR_ENABLED", ASR_ENABLED), persist=False)

    model_size = data.get("ASR_MODEL_SIZE", ASR_MODEL_SIZE)
    if isinstance(model_size, str) and model_size.strip():
        set_asr_model_size(model_size, persist=False)

    device = data.get("ASR_DEVICE", ASR_DEVICE)
    if isinstance(device, str) and device.strip():
        set_asr_device(device, persist=False)

    mode = data.get("AUDIO_SOURCE_MODE", AUDIO_SOURCE_MODE)
    if isinstance(mode, str) and mode.strip():
        try:
            set_audio_source_mode(mode, persist=False)
        except ValueError:
            pass

    if "CAPTURE_SPOTIFY_PROCESS_ONLY" in data:
        set_capture_spotify_process_only(
            data.get("CAPTURE_SPOTIFY_PROCESS_ONLY", CAPTURE_SPOTIFY_PROCESS_ONLY),
            persist=False,
        )


def _sync_debug_alias() -> None:
    global DEBUG
    DEBUG = DEBUG_POLLING or DEBUG_RECOGNITION


def set_polling_debug(enabled: bool, persist: bool = True) -> None:
    global DEBUG_POLLING
    DEBUG_POLLING = bool(enabled)
    _sync_debug_alias()
    if persist:
        _save_runtime_settings()


def set_recognition_debug(enabled: bool, persist: bool = True) -> None:
    global DEBUG_RECOGNITION
    DEBUG_RECOGNITION = bool(enabled)
    _sync_debug_alias()
    if persist:
        _save_runtime_settings()


def set_debug(enabled: bool, persist: bool = True) -> None:
    set_polling_debug(enabled, persist=False)
    set_recognition_debug(enabled, persist=False)
    if persist:
        _save_runtime_settings()


def set_asr_enabled(enabled: bool, persist: bool = True) -> None:
    global ASR_ENABLED
    ASR_ENABLED = bool(enabled)
    if persist:
        _save_runtime_settings()


def set_asr_model_size(size: str, persist: bool = True) -> None:
    global ASR_MODEL_SIZE
    actual_id = get_model_id_from_display(size.strip())
    ASR_MODEL_SIZE = actual_id
    if persist:
        _save_runtime_settings()


def set_asr_device(device: str, persist: bool = True) -> None:
    global ASR_DEVICE
    ASR_DEVICE = device.strip()
    if persist:
        _save_runtime_settings()


def set_audio_source_mode(mode: str, persist: bool = True) -> None:
    global AUDIO_SOURCE_MODE
    normalized = (mode or "").strip().lower()
    if normalized not in {"spotify", "system_media"}:
        raise ValueError("AUDIO_SOURCE_MODE 必須是 'spotify' 或 'system_media'")
    AUDIO_SOURCE_MODE = normalized
    if persist:
        _save_runtime_settings()


def set_capture_spotify_process_only(
    enabled: bool, persist: bool = True
) -> None:
    global CAPTURE_SPOTIFY_PROCESS_ONLY
    CAPTURE_SPOTIFY_PROCESS_ONLY = bool(enabled)
    if persist:
        _save_runtime_settings()


def set_spotify_credentials(
    client_id: str, client_secret: str, redirect_uri: str, persist: bool = True
) -> None:
    global CLIENT_ID, CLIENT_SECRET, REDIRECT_URI
    CLIENT_ID = (client_id or "").strip()
    CLIENT_SECRET = (client_secret or "").strip()
    REDIRECT_URI = (redirect_uri or "").strip()
    if persist:
        _save_runtime_settings()


STARTUP_PROGRESS_ACTIVE = False
_startup_progress_callback: Callable[[float, str], None] | None = None
_progress_lock = threading.Lock()

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def set_startup_progress_callback(
    callback: Callable[[float, str], None] | None,
) -> None:
    global _startup_progress_callback
    _startup_progress_callback = callback


def _display_width(text: str) -> int:
    width = 0
    for ch in text:
        code = ord(ch)
        if code in (0x2588, 0x2591):
            width += 1
        elif code > 127:
            width += 2
        else:
            width += 1
    return width


def format_console_progress_bar(
    fraction: float, message: str, width: int = 20
) -> str:
    clamped = max(0.0, min(1.0, float(fraction)))
    filled = int(round(clamped * width))
    bar = "█" * filled + "░" * (width - filled)
    percent = int(round(clamped * 100))
    return f"  [{bar}] {percent:3d}% | {message}"


def report_startup_progress(
    fraction: float,
    message: str,
    *,
    newline: bool = False,
    console_only: bool = False,
) -> None:
    clamped = max(0.0, min(1.0, float(fraction)))
    if STARTUP_PROGRESS_ACTIVE:
        with _progress_lock:
            line = format_console_progress_bar(clamped, message)
            pad = max(0, 74 - _display_width(line))
            end_char = "\n" if (newline or clamped >= 1.0) else ""
            try:
                sys.stdout.write(f"\r{line}{' ' * pad}{end_char}")
                sys.stdout.flush()
            except Exception:
                pass

    if not console_only:
        bus.emit("startup_progress", {"fraction": clamped, "message": message})
        if _startup_progress_callback is not None:
            try:
                _startup_progress_callback(clamped, message)
            except Exception:
                pass


@contextlib.contextmanager
def startup_progress_stage(
    start_fraction: float,
    max_fraction: float,
    message: str,
    *,
    console_only: bool = False,
):
    has_bus_listeners = not console_only and bus.has_subscribers("startup_progress")
    if (
        not STARTUP_PROGRESS_ACTIVE
        and not has_bus_listeners
        and (console_only or _startup_progress_callback is None)
    ):
        yield
        return

    stop_ticker = threading.Event()
    t0 = time.perf_counter()
    report_startup_progress(start_fraction, message, console_only=console_only)

    def _ticker():
        current = start_fraction
        while not stop_ticker.wait(0.25):
            elapsed = time.perf_counter() - t0
            step = (max_fraction - current) * 0.18
            current = min(max_fraction, current + max(0.005, step))
            report_startup_progress(
                current, f"{message} ({elapsed:.1f}s)", console_only=console_only
            )

    ticker_thread = threading.Thread(target=_ticker, daemon=True)
    ticker_thread.start()
    try:
        yield
    finally:
        stop_ticker.set()
        ticker_thread.join(timeout=0.5)


_migrate_legacy_data_files()
_load_runtime_settings()
