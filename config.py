import json
import os


CLIENT_ID = ""
CLIENT_SECRET = ""
REDIRECT_URI = "http://127.0.0.1:8888/callback"

SCOPE = "user-read-currently-playing"

PLAYWRIGHT_SCROLL_PIXELS = 120
PLAYWRIGHT_NAV_TIMEOUT_MS = 15000

FUZZY_LOOKAHEAD_LINES = 4
FUZZY_NEXT_LINE_THRESHOLD = 0.60
# 【新增】迷失恢復機制 (Phase 2 Lookahead)
RECOVERY_LOOKAHEAD_LINES = 6
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
# 支援: spotify / system_media (Windows: SMTC, Linux: MPRIS/playerctl)
AUDIO_SOURCE_MODE = "spotify"
UI_SCALE = 1.2

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE_DIR_CANDIDATES = [
    os.path.join(_MODULE_DIR, ".cache"),
    os.path.join(_MODULE_DIR, "_cache"),
]
_UI_SETTINGS_FILENAME = "ui_settings.json"


def _resolve_cache_dir() -> str:
    for candidate in _CACHE_DIR_CANDIDATES:
        if os.path.isdir(candidate):
            return candidate
        if os.path.exists(candidate) and not os.path.isdir(candidate):
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError("無法建立快取資料夾，請檢查 .cache/_cache 路徑權限與檔案型態")


def _ui_settings_path() -> str:
    return os.path.join(_resolve_cache_dir(), _UI_SETTINGS_FILENAME)


def load_ui_scale(default_scale: float = 1.2) -> float:
    settings_path = _ui_settings_path()
    if not os.path.exists(settings_path):
        return float(default_scale)

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("ui_scale", default_scale))
    except Exception:
        return float(default_scale)


def save_ui_scale(scale: float) -> None:
    parsed = float(scale)
    with open(_ui_settings_path(), "w", encoding="utf-8") as f:
        json.dump({"ui_scale": parsed}, f, ensure_ascii=False, indent=2)


def set_debug(enabled: bool) -> None:
    global DEBUG
    DEBUG = bool(enabled)


def set_asr_enabled(enabled: bool) -> None:
    global ASR_ENABLED
    ASR_ENABLED = bool(enabled)


def set_audio_source_mode(mode: str) -> None:
    global AUDIO_SOURCE_MODE
    normalized = (mode or "").strip().lower()
    if normalized not in {"spotify", "system_media"}:
        raise ValueError("AUDIO_SOURCE_MODE 必須是 'spotify' 或 'system_media'")
    AUDIO_SOURCE_MODE = normalized


def set_ui_scale(scale: float) -> None:
    global UI_SCALE
    parsed = float(scale)
    if parsed < 0.8 or parsed > 2.0:
        raise ValueError("UI_SCALE 必須介於 0.8 到 2.0")
    UI_SCALE = parsed


def set_spotify_credentials(
    client_id: str, client_secret: str, redirect_uri: str
) -> None:
    global CLIENT_ID, CLIENT_SECRET, REDIRECT_URI
    CLIENT_ID = (client_id or "").strip()
    CLIENT_SECRET = (client_secret or "").strip()
    REDIRECT_URI = (redirect_uri or "").strip()
