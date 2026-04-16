CLIENT_ID = ""
CLIENT_SECRET = ""
REDIRECT_URI = "http://127.0.0.1:8888/callback"

SCOPE = "user-read-currently-playing"

PLAYWRIGHT_SCROLL_PIXELS = 120
PLAYWRIGHT_NAV_TIMEOUT_MS = 15000

FUZZY_LOOKAHEAD_LINES = 4
FUZZY_NEXT_LINE_THRESHOLD = 0.60
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


def set_debug(enabled: bool) -> None:
    global DEBUG
    DEBUG = bool(enabled)


def set_asr_enabled(enabled: bool) -> None:
    global ASR_ENABLED
    ASR_ENABLED = bool(enabled)


def set_asr_model_size(size: str) -> None:
    global ASR_MODEL_SIZE
    ASR_MODEL_SIZE = size.strip()


def set_asr_device(device: str) -> None:
    global ASR_DEVICE
    ASR_DEVICE = device.strip()


def set_audio_source_mode(mode: str) -> None:
    global AUDIO_SOURCE_MODE
    normalized = (mode or "").strip().lower()
    if normalized not in {"spotify", "system_media"}:
        raise ValueError("AUDIO_SOURCE_MODE 必須是 'spotify' 或 'system_media'")
    AUDIO_SOURCE_MODE = normalized


def set_spotify_credentials(
    client_id: str, client_secret: str, redirect_uri: str
) -> None:
    global CLIENT_ID, CLIENT_SECRET, REDIRECT_URI
    CLIENT_ID = (client_id or "").strip()
    CLIENT_SECRET = (client_secret or "").strip()
    REDIRECT_URI = (redirect_uri or "").strip()
