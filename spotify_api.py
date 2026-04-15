import os
import time
from typing import Any

import spotipy
from spotipy.oauth2 import SpotifyOAuth

import config

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE_DIR_CANDIDATES = [
    os.path.join(_MODULE_DIR, ".cache"),
    os.path.join(_MODULE_DIR, "_cache"),
]
SPOTIFY_REQUEST_TIMEOUT_SECONDS = 5


def _resolve_cache_dir():
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


def _spotify_cache_path():
    return os.path.join(_resolve_cache_dir(), "spotify_token_cache")


def _last_callback_url_path():
    return os.path.join(_resolve_cache_dir(), "spotify_last_callback_url.txt")


def _build_oauth_manager():
    return SpotifyOAuth(
        client_id=config.CLIENT_ID,
        client_secret=config.CLIENT_SECRET,
        redirect_uri=config.REDIRECT_URI,
        scope=config.SCOPE,
        open_browser=False,
        cache_path=_spotify_cache_path(),
    )


def get_spotify_authorize_url():
    oauth = _build_oauth_manager()
    return oauth.get_authorize_url()


def exchange_callback_url_for_token(callback_url: str) -> dict[str, Any]:
    oauth = _build_oauth_manager()
    code = oauth.parse_response_code(callback_url)
    if not code:
        raise ValueError("無法從 callback URL 取得授權 code")
    token_info = oauth.get_access_token(code, as_dict=True)
    if not isinstance(token_info, dict):
        raise RuntimeError("Spotify token 回傳格式異常")
    return token_info


def save_last_callback_url(callback_url: str):
    with open(_last_callback_url_path(), "w", encoding="utf-8") as f:
        f.write((callback_url or "").strip())


def load_last_callback_url():
    callback_path = _last_callback_url_path()
    if not os.path.exists(callback_path):
        return ""
    with open(callback_path, "r", encoding="utf-8") as f:
        return f.read().strip()


def get_cached_token_info():
    oauth = _build_oauth_manager()
    return oauth.get_cached_token()


def has_valid_cached_token(min_valid_seconds: int = 60):
    token_info = get_cached_token_info()
    if not token_info:
        return False

    expires_at = token_info.get("expires_at")
    if not expires_at:
        return False

    return float(expires_at) > (time.time() + min_valid_seconds)


def get_spotify_client():
    return spotipy.Spotify(
        auth_manager=_build_oauth_manager(),
        language="ja",
        requests_timeout=SPOTIFY_REQUEST_TIMEOUT_SECONDS,
        retries=0,
    )
