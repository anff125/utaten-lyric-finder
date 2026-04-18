import json
import os

CLIENT_ID = ""
CLIENT_SECRET = ""
REDIRECT_URI = "http://127.0.0.1:8888/callback"

SCOPE = "user-read-currently-playing"

PLAYWRIGHT_SCROLL_PIXELS = 120
PLAYWRIGHT_NAV_TIMEOUT_MS = 15000

FUZZY_LOOKAHEAD_LINES = 4
FUZZY_NEXT_LINE_THRESHOLD = 0.75
# 【新增】迷失恢復機制 (Phase 2 Lookahead)
RECOVERY_LOOKAHEAD_LINES = 8
RECOVERY_THRESHOLD = 0.60

ASR_ENABLED = True
ASR_MODEL_SIZE = "large-v3"  # tiny, base, small, medium, large-v3
ASR_DEVICE = "auto"
ASR_COMPUTE_TYPE = "default"
ASR_LANGUAGE = "ja"
ASR_SAMPLE_RATE = 16000
ASR_BLOCK_SECONDS = 4

SPOTIFY_POLL_SECONDS = 5
MAIN_LOOP_SLEEP_SECONDS = 0.5

DEBUG = False
# 支援: spotify / system_media
AUDIO_SOURCE_MODE = "spotify"


_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE_DIR = os.path.join(_MODULE_DIR, ".cache")
_RUNTIME_SETTINGS_FILE = os.path.join(_CACHE_DIR, "runtime_settings.json")
_PERSISTED_KEYS = {
    "CLIENT_ID",
    "CLIENT_SECRET",
    "REDIRECT_URI",
    "DEBUG",
    "AUDIO_SOURCE_MODE",
    "ASR_ENABLED",
    "ASR_MODEL_SIZE",
    "ASR_DEVICE",
}


def _ensure_cache_dir() -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)


def _save_runtime_settings() -> None:
    try:
        _ensure_cache_dir()
        data = {key: globals()[key] for key in _PERSISTED_KEYS}
        with open(_RUNTIME_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        # 設定儲存失敗時不應中斷主要流程。
        return


def _load_runtime_settings() -> None:
    if not os.path.exists(_RUNTIME_SETTINGS_FILE):
        return

    try:
        with open(_RUNTIME_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    if not isinstance(data, dict):
        return

    client_id = data.get("CLIENT_ID", CLIENT_ID)
    client_secret = data.get("CLIENT_SECRET", CLIENT_SECRET)
    redirect_uri = data.get("REDIRECT_URI", REDIRECT_URI)
    set_spotify_credentials(client_id, client_secret, redirect_uri, persist=False)

    set_debug(data.get("DEBUG", DEBUG), persist=False)
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


def set_debug(enabled: bool, persist: bool = True) -> None:
    global DEBUG
    DEBUG = bool(enabled)
    if persist:
        _save_runtime_settings()


def set_asr_enabled(enabled: bool, persist: bool = True) -> None:
    global ASR_ENABLED
    ASR_ENABLED = bool(enabled)
    if persist:
        _save_runtime_settings()


def set_asr_model_size(size: str, persist: bool = True) -> None:
    global ASR_MODEL_SIZE
    ASR_MODEL_SIZE = size.strip()
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


def set_spotify_credentials(
    client_id: str, client_secret: str, redirect_uri: str, persist: bool = True
) -> None:
    global CLIENT_ID, CLIENT_SECRET, REDIRECT_URI
    CLIENT_ID = (client_id or "").strip()
    CLIENT_SECRET = (client_secret or "").strip()
    REDIRECT_URI = (redirect_uri or "").strip()
    if persist:
        _save_runtime_settings()


_load_runtime_settings()
