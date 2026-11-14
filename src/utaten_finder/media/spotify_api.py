"""Spotify Web API authentication and client helpers."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import time
from typing import cast

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from utaten_finder.core import config

SPOTIFY_REQUEST_TIMEOUT_SECONDS = 5


class _CallbackHTTPServer(HTTPServer):
    callback_path: str
    callback_host: str
    callback_url: str | None


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = cast(_CallbackHTTPServer, self.server)
        if self.path.startswith(server.callback_path):
            full_url = f"http://{server.callback_host}:{server.server_port}{self.path}"
            server.callback_url = full_url
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><body><h3>Authentication received. You can close this tab.</h3></body></html>".encode(
                    "utf-8"
                )
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def wait_for_oauth_callback(
    callback_host: str,
    callback_port: int,
    callback_path: str,
    timeout: int = 180,
) -> str | None:
    """Listen on a local HTTP endpoint for a single Spotify OAuth redirect."""
    server = _CallbackHTTPServer((callback_host, callback_port), _CallbackHandler)
    server.callback_path = callback_path
    server.callback_host = callback_host
    server.callback_url = None
    server.timeout = timeout
    try:
        server.handle_request()
        return server.callback_url
    finally:
        server.server_close()


def _spotify_cache_path() -> str:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return str(config.SPOTIFY_TOKEN_CACHE_FILE)


def _last_callback_url_path() -> str:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return str(config.SPOTIFY_LAST_CALLBACK_FILE)


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


def exchange_callback_url_for_token(callback_url: str):
    oauth = _build_oauth_manager()
    code = oauth.parse_response_code(callback_url)
    if not code:
        raise ValueError("無法從 callback URL 取得授權 code")
    return oauth.get_access_token(code, as_dict=True)


def save_last_callback_url(callback_url: str):
    with open(_last_callback_url_path(), "w", encoding="utf-8") as f:
        f.write((callback_url or "").strip())


def load_last_callback_url():
    callback_path = _last_callback_url_path()
    try:
        with open(callback_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


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
